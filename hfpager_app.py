#!/usr/bin/env python3
"""
HFPager-compatible messenger (PS-18, 5.86 and 23.44 Bd) – experimental.  Windows / macOS / Linux.

Input and output go through the computer's sound card (connect it to the transceiver's audio
interface, SSB).  Requires: Python 3.10+, numpy, sounddevice (tkinter is part of Python).

    pip install numpy sounddevice
    python hfpager_app.py
"""
import os
import sys
import json
import time
import queue
import threading
import datetime

import numpy as np

import hfpager_modem as M
import hfpager_rx as R

try:
    import sounddevice as sd
    SD_ERROR = None
except Exception as _ex:           # PortAudio missing etc.
    sd = None
    SD_ERROR = _ex

def _app_dir():
    if sys.platform == 'darwin':
        return os.path.join(os.path.expanduser('~'), 'Library', 'Application Support', 'HFPagerApp')
    if sys.platform.startswith('win'):
        return os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'HFPagerApp')
    return os.path.join(os.environ.get('XDG_CONFIG_HOME', os.path.join(os.path.expanduser('~'), '.config')),
                        'HFPagerApp')


APP_DIR = _app_dir()
MONO_FONT = 'Menlo' if sys.platform == 'darwin' else ('Consolas' if sys.platform.startswith('win') else 'DejaVu Sans Mono')
SETTINGS_FILE = os.path.join(APP_DIR, 'settings.json')
LOG_FILE = os.path.join(APP_DIR, 'messages.log')

DEFAULTS = dict(my_id=1, to_id=1, in_dev='', out_dev='', speed='5.86', f0=1611.4, tx_level=0.5,
                ptt_delay=0.5, afc=True, ack=False, counter=0, sens_db=4.0, auto_ack=False)


# ============================================================================== non-GUI core
class Core:
    """Settings, audio streams, receiver engine, transmitter.  Talks to the GUI via a queue."""

    def __init__(self, ui_queue, sd_module=None):
        self.q = ui_queue
        self.sd = sd_module if sd_module is not None else sd
        self.s = dict(DEFAULTS)
        self.load()
        self.engine = None
        self.stream = None
        self.fs = 48000
        self.tx_busy = False
        self._seen = {}
        self._tx_thread = None
        self.pending = {}          # (counter, header crc) -> (dest, text, time) awaiting an ACK

    # ---- settings
    def load(self):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                self.s.update(json.load(f))
        except Exception:
            pass

    def save(self):
        try:
            os.makedirs(APP_DIR, exist_ok=True)
            with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.s, f, ensure_ascii=False, indent=1)
        except Exception as ex:
            self.sys('Не удалось сохранить настройки: %s' % ex)

    def log(self, line):
        try:
            os.makedirs(APP_DIR, exist_ok=True)
            with open(LOG_FILE, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except Exception:
            pass

    def sys(self, text):
        self.q.put(('sys', text))

    # ---- devices
    def devices(self):
        """returns (inputs, outputs): lists of (index, label)"""
        ins, outs = [], []
        if self.sd is None:
            return ins, outs
        try:
            apis = self.sd.query_hostapis()
            for i, d in enumerate(self.sd.query_devices()):
                api = apis[d['hostapi']]['name'] if d.get('hostapi') is not None else ''
                label = '%d: %s [%s]' % (i, d['name'], api)
                if d['max_input_channels'] > 0:
                    ins.append((i, label))
                if d['max_output_channels'] > 0:
                    outs.append((i, label))
        except Exception as ex:
            self.sys('Ошибка опроса звуковых устройств: %s' % ex)
        return ins, outs

    @staticmethod
    def _idx(label):
        try:
            return int(str(label).split(':')[0])
        except Exception:
            return None

    # ---- receiving
    def start_rx(self):
        self.stop_rx()
        if self.sd is None:
            self.sys('Звуковая библиотека недоступна: %s' % SD_ERROR)
            return False
        dev = self._idx(self.s['in_dev'])
        fs = None
        for cand in (48000, 44100):
            try:
                self.sd.check_input_settings(device=dev, channels=1, samplerate=cand)
                fs = cand
                break
            except Exception:
                continue
        if fs is None:
            try:
                info = self.sd.query_devices(dev, 'input')
                fs = int(info['default_samplerate'])
            except Exception:
                fs = 48000
        self.fs = fs
        self.engine = R.RxEngine(fs, self._on_frame, self._on_event, afc=bool(self.s['afc']))
        self.engine.pk_margin = float(self.s.get('sens_db', 4.0))
        self.engine.start()

        def cb(indata, frames, time_info, status):
            self.engine.feed(indata[:, 0])
        try:
            self.stream = self.sd.InputStream(samplerate=fs, device=dev, channels=1, dtype='float32',
                                              blocksize=int(0.05 * fs), callback=cb)
            self.stream.start()
        except Exception as ex:
            self.sys('Не удалось открыть вход звуковой карты: %s' % ex)
            self.stream = None
            return False
        self.sys('Приём запущен (%d Гц). Распознаются 5.86 и 23.44 Бод, АПЧ %s.' %
                 (fs, 'включена' if self.s['afc'] else 'выключена'))
        return True

    def stop_rx(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        if self.engine is not None:
            self.engine.stop()
            self.engine = None

    def apply_live_settings(self):
        if self.engine is not None:
            self.engine.afc = bool(self.s['afc'])
            self.engine.pk_margin = float(self.s.get('sens_db', 4.0))

    def _on_event(self, kind, info):
        self.q.put(('event', kind, info))

    def _on_frame(self, fr, info):
        now = time.time()
        stamp = datetime.datetime.now().isoformat(timespec='seconds')
        if fr.get('kind') in ('ack', 'nak'):
            mine = int(self.s['my_id'])
            hit = self.pending.pop((fr['orig_counter'], fr['orig_crc']), None) if fr['dest'] == mine else None
            self.q.put(('ack', fr, info, hit))
            self.log('%s RX %s to=%d orig#%d crc=%04x delivered=%s' % (stamp, fr['kind'].upper(), fr['dest'],
                     fr['orig_counter'], fr['orig_crc'], bool(hit)))
            return
        key = (fr['sender'], fr['dest'], fr['counter'], fr['text'])
        dup = key in self._seen and now - self._seen[key] < 120
        self._seen[key] = now
        for k in [k for k, t in self._seen.items() if now - t > 600]:
            del self._seen[k]
        self.q.put(('rx', fr, info, dup))
        self.log('%s RX %s Bd %d>%d #%d %r' % (stamp, fr['speed'], fr['sender'], fr['dest'], fr['counter'],
                 fr['text'].decode('cp1251', 'replace')))
        if (self.s.get('auto_ack') and fr.get('kind') == 'msg' and (fr['b1'] & 0x02)
                and fr['dest'] == int(self.s['my_id']) and not dup):
            threading.Timer(1.5, self.send_ack, args=(fr,)).start()

    def send_ack(self, fr):
        """experimental: confirm delivery of a received message"""
        if self.tx_busy or self.sd is None:
            return
        own = int(self.s['counter']) & 0xFF
        with R.MODEM_LOCK:
            M.set_speed(fr['speed'])
            nib = M.tx_nibbles(M.ack_frame_nibbles(fr['sender'], fr['counter'], fr['hdr_crc'],
                                                   orig_len=fr['length'], own_counter=own))
            audio = M.synth(M.nibbles_to_tones(nib), fs=self.fs, f0=float(self.s['f0']),
                            amp=float(self.s['tx_level']), lead=float(self.s['ptt_delay']), tail=0.3)
        self.s['counter'] = (own + 1) & 0xFF
        self.save()
        self.tx_busy = True
        self.q.put(('tx_ack', fr['sender'], fr['counter'], fr['speed'], len(audio) / self.fs))
        self._tx_thread = threading.Thread(target=self._tx_run,
                                           args=(audio.astype(np.float32), self._idx(self.s['out_dev'])),
                                           daemon=True)
        self._tx_thread.start()

    # ---- transmitting
    @staticmethod
    def air_time(speed, nbytes):
        sp = R.M.SPEEDS[speed]
        nsb = 1 + (nbytes + 11) // 12
        return (sp['pre'] + 1 + 60 * nsb + 2) / sp['baud']

    def build_audio(self, dest, sender, counter, text_bytes, speed, f0, level, ptt_delay, ack):
        with R.MODEM_LOCK:
            M.set_speed(speed)
            nib = M.tx_nibbles(M.frame_nibbles(dest, sender, counter, text_bytes,
                                               b1=0xA2 if ack else 0xA0))
            tones = M.nibbles_to_tones(nib)
            audio = M.synth(tones, fs=self.fs, f0=f0, amp=level, lead=ptt_delay, tail=0.3)
        return audio.astype(np.float32)

    def send(self, to_id, text):
        """validate and start transmission; returns error text or None"""
        if self.tx_busy:
            return 'Передача уже идёт.'
        if self.sd is None:
            return 'Звуковая библиотека недоступна: %s' % SD_ERROR
        try:
            my = int(self.s['my_id'])
            to = int(to_id)
        except Exception:
            return 'ID должны быть числами.'
        if not (0 <= my <= 65535) or not (0 <= to <= 65535):
            return 'ID должен быть в диапазоне 0…65535.'
        data = text.encode('cp1251', 'replace')
        if not (1 <= len(data) <= 252):
            return 'Длина текста: от 1 до 252 символов (сейчас %d).' % len(data)
        speed = self.s['speed']
        f0 = float(self.s['f0'])
        if not (300 <= f0 <= 2000):
            return 'Частота нулевого тона должна быть в диапазоне 300…2000 Гц.'
        counter = int(self.s['counter']) & 0xFF
        out = self._idx(self.s['out_dev'])
        audio = self.build_audio(to, my, counter, data, speed, f0, float(self.s['tx_level']),
                                 float(self.s['ptt_delay']), bool(self.s['ack']))
        if self.s['ack']:
            crc = M.header_crc(to, my, counter, data, b1=0xA2)
            self.pending[(counter, crc)] = (to, text, time.time())
        self.s['counter'] = (counter + 1) & 0xFF
        self.s['to_id'] = to
        self.save()
        self.tx_busy = True
        self.q.put(('tx_start', my, to, counter, text, speed, len(audio) / self.fs))
        self._tx_thread = threading.Thread(target=self._tx_run, args=(audio, out), daemon=True)
        self._tx_thread.start()
        self.log('%s TX %s Bd %d>%d #%d %r' % (datetime.datetime.now().isoformat(timespec='seconds'),
                 speed, my, to, counter, text))
        return None

    def _tx_run(self, audio, out):
        if self.engine is not None:
            self.engine.pause(True)
        err = None
        try:
            self.sd.play(audio, self.fs, device=out)
            self.sd.wait()
        except Exception as ex:
            err = str(ex)
        finally:
            time.sleep(0.4)
            if self.engine is not None:
                self.engine.pause(False)
            self.tx_busy = False
            self.q.put(('tx_done', err))

    def abort_tx(self):
        try:
            self.sd.stop()
        except Exception:
            pass


# ============================================================================== GUI
def run_gui():
    import tkinter as tk
    from tkinter import ttk, messagebox

    import tkinter.font as tkfont
    root = tk.Tk()
    root.title('HFPager-совместимый пейджер (PS-18)')
    root.geometry('1040x860')
    for fname in ('TkDefaultFont', 'TkTextFont', 'TkMenuFont'):
        try:
            tkfont.nametofont(fname).configure(size=13 if sys.platform == 'darwin' else 11)
        except Exception:
            pass
    q = queue.Queue()
    core = Core(q)

    var = dict(
        my_id=tk.StringVar(value=str(core.s['my_id'])),
        to_id=tk.StringVar(value=str(core.s['to_id'])),
        in_dev=tk.StringVar(value=core.s['in_dev']),
        out_dev=tk.StringVar(value=core.s['out_dev']),
        speed=tk.StringVar(value=core.s['speed']),
        f0=tk.StringVar(value=str(core.s['f0'])),
        tx_level=tk.DoubleVar(value=core.s['tx_level']),
        ptt_delay=tk.StringVar(value=str(core.s['ptt_delay'])),
        afc=tk.BooleanVar(value=core.s['afc']),
        ack=tk.BooleanVar(value=core.s['ack']),
        auto_ack=tk.BooleanVar(value=core.s['auto_ack']),
        sens_db=tk.StringVar(value=str(core.s['sens_db'])),
        msg=tk.StringVar(),
        status=tk.StringVar(value='Ожидание'),
        info=tk.StringVar(value=''),
        counter=tk.StringVar(),
        level_txt=tk.StringVar(value='— дБ'),
        clip=tk.StringVar(value=''),
    )

    # ---------------------------------------------------------------- settings panel
    top = ttk.LabelFrame(root, text='Настройки')
    top.pack(fill='x', padx=8, pady=6)

    ttk.Label(top, text='Мой ID:').grid(row=0, column=0, sticky='e', padx=4, pady=3)
    ttk.Entry(top, textvariable=var['my_id'], width=8).grid(row=0, column=1, sticky='w')
    ttk.Label(top, text='Вход (приём):').grid(row=0, column=2, sticky='e', padx=4)
    cb_in = ttk.Combobox(top, textvariable=var['in_dev'], width=44, state='readonly')
    cb_in.grid(row=0, column=3, sticky='w')
    ttk.Label(top, text='Выход (передача):').grid(row=1, column=2, sticky='e', padx=4)
    cb_out = ttk.Combobox(top, textvariable=var['out_dev'], width=44, state='readonly')
    cb_out.grid(row=1, column=3, sticky='w')

    ttk.Label(top, text='Скорость передачи, Бод:').grid(row=1, column=0, sticky='e', padx=4, pady=3)
    ttk.Combobox(top, textvariable=var['speed'], values=list(R.M.SPEEDS), width=7,
                 state='readonly').grid(row=1, column=1, sticky='w')

    opt = ttk.Frame(top)
    opt.grid(row=2, column=0, columnspan=4, sticky='w', pady=3)
    ttk.Label(opt, text='Тон 0, Гц:').pack(side='left', padx=4)
    ttk.Entry(opt, textvariable=var['f0'], width=8).pack(side='left')
    ttk.Label(opt, text='Уровень TX:').pack(side='left', padx=(12, 4))
    ttk.Scale(opt, from_=0.05, to=1.0, variable=var['tx_level'], length=110).pack(side='left')
    ttk.Label(opt, text='Задержка PTT/VOX, с:').pack(side='left', padx=(12, 4))
    ttk.Entry(opt, textvariable=var['ptt_delay'], width=5).pack(side='left')
    ttk.Label(opt, text='Порог приёма, дБ:').pack(side='left', padx=(12, 4))
    ttk.Entry(opt, textvariable=var['sens_db'], width=5).pack(side='left')
    ttk.Checkbutton(opt, text='АПЧ', variable=var['afc']).pack(side='left', padx=(12, 4))
    ttk.Checkbutton(opt, text='Запрашивать подтверждение', variable=var['ack']).pack(side='left', padx=4)
    ttk.Checkbutton(opt, text='Автоподтверждение (экспер.)', variable=var['auto_ack']).pack(side='left', padx=4)

    # ---------------------------------------------------------------- chat
    mid = ttk.LabelFrame(root, text='Общий обмен сообщениями')
    mid.pack(fill='both', expand=True, padx=8, pady=4)
    CF = 13 if sys.platform == 'darwin' else 12            # chat font size
    GREEN, GREEN_DIM, RED, YELLOW = '#33ff33', '#1f9e1f', '#ff4a4a', '#ffee33'
    chat = tk.Text(mid, wrap='word', state='disabled', font=(MONO_FONT, CF), background='#000000',
                   foreground=GREEN, insertbackground=GREEN, selectbackground='#145214', padx=6, pady=4)
    sb = ttk.Scrollbar(mid, command=chat.yview)
    chat.configure(yscrollcommand=sb.set)
    sb.pack(side='right', fill='y')
    chat.pack(fill='both', expand=True)
    chat.tag_configure('tome', foreground=GREEN, font=(MONO_FONT, CF, 'bold'))     # received, addressed to me
    chat.tag_configure('rx', foreground=GREEN)                                      # received (others, beacons)
    chat.tag_configure('beacon', foreground=GREEN)
    chat.tag_configure('dup', foreground=GREEN_DIM)                                 # received again
    chat.tag_configure('mine', foreground=RED)                                      # transmitted
    chat.tag_configure('sys', foreground=YELLOW, font=(MONO_FONT, CF - 1, 'italic'))  # everything else
    chat.tag_configure('note', foreground=YELLOW, font=(MONO_FONT, CF, 'bold'))

    def add(text, tag):
        chat.configure(state='normal')
        chat.insert('end', text + '\n', tag)
        chat.configure(state='disabled')
        chat.see('end')

    def now():
        return datetime.datetime.now().strftime('%H:%M:%S')

    # ---------------------------------------------------------------- composer
    bot = ttk.LabelFrame(root, text='Новое сообщение')
    bot.pack(fill='x', padx=8, pady=6)
    ttk.Label(bot, text='Кому (ID):').grid(row=0, column=0, padx=4, pady=4, sticky='e')
    ttk.Entry(bot, textvariable=var['to_id'], width=8).grid(row=0, column=1, sticky='w')
    ttk.Label(bot, text='Текст:').grid(row=0, column=2, padx=4, sticky='e')
    ent = ttk.Entry(bot, textvariable=var['msg'], width=70)
    ent.grid(row=0, column=3, sticky='we', padx=2)
    btn = ttk.Button(bot, text='Отправить')
    btn.grid(row=0, column=4, padx=6)
    btn_stop = ttk.Button(bot, text='Прервать')
    btn_stop.grid(row=0, column=5, padx=2)
    bot.columnconfigure(3, weight=1)
    ttk.Label(bot, textvariable=var['counter']).grid(row=1, column=3, sticky='w')

    # ---------------------------------------------------------------- receive monitor
    mon = ttk.LabelFrame(root, text='Монитор приёма')
    mon.pack(fill='x', padx=8, pady=(0, 6))

    lamp_fr = ttk.Frame(mon)
    lamp_fr.pack(side='left', padx=10, pady=4)
    LAMP = {'rx_on': '#33ff33', 'rx_off': '#0b3a0b', 'rx_dec': '#ffcc00', 'tx_on': '#ff2a2a', 'tx_off': '#4a0b0b'}

    def make_lamp(col, label, off):
        c = tk.Canvas(lamp_fr, width=34, height=34, highlightthickness=0)
        c.grid(row=0, column=col, padx=6)
        oval = c.create_oval(4, 4, 30, 30, fill=off, outline='#222222', width=2)
        ttk.Label(lamp_fr, text=label, font=('Helvetica', 11, 'bold')).grid(row=1, column=col)
        return c, oval

    rx_lamp = make_lamp(0, 'RX', LAMP['rx_off'])
    tx_lamp = make_lamp(1, 'TX', LAMP['tx_off'])
    rx_state = {'state': 'idle', 'flash_until': 0.0}

    def update_lamps():
        t = time.time()
        if rx_state['state'] == 'receiving' or t < rx_state['flash_until']:
            colr = LAMP['rx_on']
        elif rx_state['state'] == 'decoding':
            colr = LAMP['rx_dec']
        else:
            colr = LAMP['rx_off']
        rx_lamp[0].itemconfig(rx_lamp[1], fill=colr)
        tx_lamp[0].itemconfig(tx_lamp[1], fill=LAMP['tx_on'] if core.tx_busy else LAMP['tx_off'])

    meter = ttk.Frame(mon)
    meter.pack(side='left', padx=10)
    ttk.Label(meter, text='Уровень входа, дБ от полной шкалы').pack(anchor='w')
    level = ttk.Progressbar(meter, length=210, maximum=60)
    level.pack(anchor='w', pady=2)
    ttk.Label(meter, textvariable=var['level_txt']).pack(anchor='w')
    ttk.Label(meter, textvariable=var['clip'], foreground='#cc0000').pack(anchor='w')

    # waterfall (spectrogram) 300..3000 Hz, newest line on top
    WF_W, WF_H, ZOOM = 270, 56, 2
    wf = np.full((WF_H, WF_W), -120.0, dtype=np.float32)
    wf_state = {'lo': None, 'img': None}
    _pos = [0.0, 0.25, 0.55, 0.8, 1.0]
    _lut_x = np.linspace(0, 1, 256)
    lut = np.stack([np.interp(_lut_x, _pos, c) for c in
                    ([0, 0, 40, 255, 255], [0, 50, 230, 255, 255], [0, 0, 40, 60, 200])], axis=1).astype(np.uint8)
    wf_box = ttk.Frame(mon)
    wf_box.pack(side='left', padx=10, pady=4)
    wf_canvas = tk.Canvas(wf_box, width=WF_W * ZOOM, height=WF_H * ZOOM, background='black',
                          highlightthickness=1, highlightbackground='#333333')
    wf_canvas.pack()
    wf_item = wf_canvas.create_image(0, 0, anchor='nw')
    axis = tk.Canvas(wf_box, width=WF_W * ZOOM, height=16, highlightthickness=0)
    axis.pack()
    for f_hz in (500, 1000, 1500, 2000, 2500, 3000):
        x_ = (f_hz - 300) / 10 * ZOOM
        axis.create_line(x_, 0, x_, 4)
        axis.create_text(min(x_, WF_W * ZOOM - 14), 11, text=str(f_hz), font=('Helvetica', 8))
    ttk.Label(wf_box, text='Спектр, Гц').pack()

    def draw_waterfall():
        lo = wf_state['lo']
        norm = np.clip((wf - lo) / 45.0, 0.0, 1.0)
        rgb = lut[(norm * 255).astype(np.uint8)]
        ppm = b'P6 %d %d 255\n' % (WF_W, WF_H) + rgb.tobytes()
        img = tk.PhotoImage(data=ppm, format='PPM').zoom(ZOOM, ZOOM)
        wf_state['img'] = img                    # keep a reference
        wf_canvas.itemconfig(wf_item, image=img)

    def on_spectrum(row):
        row = np.asarray(row, dtype=np.float32)[:WF_W]
        if len(row) < WF_W:
            row = np.pad(row, (0, WF_W - len(row)), constant_values=-120.0)
        med = float(np.median(row))
        wf_state['lo'] = med if wf_state['lo'] is None else 0.97 * wf_state['lo'] + 0.03 * med
        wf[1:] = wf[:-1]
        wf[0] = row
        draw_waterfall()

    sbar = ttk.Frame(root)
    sbar.pack(fill='x', padx=8, pady=(0, 6))
    ttk.Label(sbar, textvariable=var['status'], width=26).pack(side='left')
    ttk.Label(sbar, textvariable=var['info']).pack(side='left', padx=8)

    # ---------------------------------------------------------------- logic glue
    def pull_settings():
        try:
            core.s['my_id'] = int(var['my_id'].get())
        except ValueError:
            pass
        core.s['in_dev'] = var['in_dev'].get()
        core.s['out_dev'] = var['out_dev'].get()
        core.s['speed'] = var['speed'].get()
        try:
            core.s['f0'] = float(var['f0'].get().replace(',', '.'))
        except ValueError:
            pass
        try:
            core.s['ptt_delay'] = max(0.0, min(5.0, float(var['ptt_delay'].get().replace(',', '.'))))
        except ValueError:
            pass
        try:
            core.s['sens_db'] = max(1.0, min(20.0, float(var['sens_db'].get().replace(',', '.'))))
        except ValueError:
            pass
        core.s['tx_level'] = float(var['tx_level'].get())
        core.s['afc'] = bool(var['afc'].get())
        core.s['ack'] = bool(var['ack'].get())
        core.s['auto_ack'] = bool(var['auto_ack'].get())

    def refresh_devices():
        ins, outs = core.devices()
        cb_in['values'] = [l for _, l in ins]
        cb_out['values'] = [l for _, l in outs]
        if not var['in_dev'].get() and ins:
            var['in_dev'].set(ins[0][1])
        if not var['out_dev'].get() and outs:
            var['out_dev'].set(outs[0][1])

    def restart_rx(*_):
        pull_settings()
        core.save()
        core.start_rx()

    def on_send(*_):
        pull_settings()
        text = var['msg'].get().strip()
        err = core.send(var['to_id'].get(), text)
        if err:
            messagebox.showwarning('Отправка', err)
        else:
            var['msg'].set('')
        update_counter()

    def update_counter(*_):
        data = var['msg'].get().encode('cp1251', 'replace')
        n = len(data)
        t = Core.air_time(var['speed'].get(), max(n, 1))
        var['counter'].set('%d/252 байт, время в эфире ≈ %.0f с, сообщение № %d' %
                           (n, t, int(core.s['counter']) & 0xFF))

    btn.configure(command=on_send)
    btn_stop.configure(command=core.abort_tx)
    ent.bind('<Return>', on_send)
    var['msg'].trace_add('write', update_counter)
    var['speed'].trace_add('write', update_counter)
    for k in ('in_dev',):
        cb_in.bind('<<ComboboxSelected>>', restart_rx)
    ttk.Button(top, text='Обновить', command=lambda: (refresh_devices(), restart_rx())).grid(
        row=0, column=4, padx=6)
    ttk.Button(top, text='Применить', command=lambda: (pull_settings(), core.save(),
                                                       core.apply_live_settings())).grid(row=1, column=4, padx=6)

    clip_until = [0.0]

    def handle(item):
        kind = item[0]
        if kind == 'sys':
            add('[%s] • %s' % (now(), item[1]), 'sys')
        elif kind == 'rx':
            _, fr, info, dup = item
            mine = int(core.s['my_id'])
            text = fr['text'].decode('cp1251', 'replace')
            if fr['b0'] == 0x06:
                add('[%s] ★ Маяк от %d: %s  (%s Бод)' % (now(), fr['sender'], text, fr['speed']), 'beacon')
            else:
                tag = 'dup' if dup else ('tome' if fr['dest'] == mine else 'rx')
                line = '[%s] %d → %d  №%d  (%s Бод, АПЧ %.0f Гц): %s%s' % (
                    now(), fr['sender'], fr['dest'], fr['counter'], fr['speed'],
                    info.get('afc_hz', 0.0), text, '   (повтор)' if dup else '')
                add(line, tag)
                if (fr['b1'] & 0x02) and fr['dest'] == mine and not dup:
                    add('      запрошено подтверждение доставки%s' % (
                        ' – отправляю (экспериментально)' if core.s.get('auto_ack') else
                        ' (автоподтверждение выключено)'), 'sys')
            var['info'].set('Последний кадр: центр %.0f Гц, диапазон АПЧ %.1f Гц' %
                            (info.get('center_hz', 0), info.get('afc_hz', 0)))
            rx_state['flash_until'] = time.time() + 1.5
            update_lamps()
        elif kind == 'ack':
            _, fr, info, hit = item
            who = 'ПОДТВЕРЖДЕНИЕ' if fr['kind'] == 'ack' else 'ОТКАЗ'
            if hit:
                add('[%s] ✔ %s: сообщение №%d для %d доставлено («%s»)' % (
                    now(), who, fr['orig_counter'], hit[0], hit[1]), 'note')
            else:
                add('[%s] ✔ %s для ID %d (исходное сообщение №%d, CRC %04X)' % (
                    now(), who, fr['dest'], fr['orig_counter'], fr['orig_crc']), 'note')
        elif kind == 'tx_ack':
            _, to, cnt, speed, secs = item
            add('[%s] ✔ → %d  подтверждение сообщения №%d  (%s Бод, ≈ %.0f с)' % (now(), to, cnt, speed, secs), 'mine')
            var['status'].set('Передача…')
            btn.state(['disabled'])
            update_lamps()
        elif kind == 'tx_start':
            _, my, to, cnt, text, speed, secs = item
            add('[%s] %d → %d  №%d  (%s Бод, передача ≈ %.0f с): %s' % (now(), my, to, cnt, speed, secs, text),
                'mine')
            var['status'].set('Передача…')
            btn.state(['disabled'])
            update_lamps()
        elif kind == 'tx_done':
            btn.state(['!disabled'])
            var['status'].set('Ожидание')
            update_lamps()
            if item[1]:
                add('[%s] • Ошибка передачи: %s' % (now(), item[1]), 'sys')
        elif kind == 'event':
            _, ev, info = item
            if ev == 'level':
                db = info.get('rms_db', -60.0)
                level['value'] = max(0.0, min(60.0, db + 60.0))
                var['level_txt'].set('%.0f дБ' % db)
                if info.get('clip'):
                    var['clip'].set('ПЕРЕГРУЗКА ВХОДА — уменьшите уровень!')
                    clip_until[0] = time.time() + 1.5
                elif time.time() > clip_until[0]:
                    var['clip'].set('')
            elif ev == 'spectrum':
                on_spectrum(info['db'])
            elif ev == 'state' and not core.tx_busy:
                rx_state['state'] = info['state']
                var['status'].set({'idle': 'Ожидание', 'receiving': 'Приём сигнала…',
                                   'decoding': 'Декодирование…'}.get(info['state'], ''))
                update_lamps()
            elif ev == 'fail':
                add('[%s] • Принят сигнал (%.0f с), но кадр не удалось декодировать '
                    '(слишком слабый или искажён)' % (now(), info['seconds']), 'sys')
            elif ev == 'error':
                add('[%s] • Внутренняя ошибка: %s' % (now(), info['text']), 'sys')

    def poll():
        try:
            while True:
                handle(q.get_nowait())
        except queue.Empty:
            pass
        update_lamps()
        root.after(100, poll)

    def on_close():
        try:
            pull_settings()
            core.save()
            core.stop_rx()
        finally:
            root.destroy()

    root.protocol('WM_DELETE_WINDOW', on_close)
    refresh_devices()
    update_counter()
    if sd is None:
        add('• Библиотека sounddevice/PortAudio недоступна: %s' % SD_ERROR, 'sys')
        add('• Установите: pip install sounddevice numpy', 'sys')
    else:
        add('• Передача и приём идут через выбранные звуковые устройства. '
            'Работа на передачу в КВ-диапазоне требует лицензии.', 'sys')
        restart_rx()
    poll()
    root.mainloop()


if __name__ == '__main__':
    run_gui()
