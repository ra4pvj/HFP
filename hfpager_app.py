#!/usr/bin/env python3
"""
HFPager-compatible messenger (PS-18, 5.86 and 23.44 Bd) – experimental.  Windows / macOS / Linux.

Input and output go through the computer's sound card (connect it to the transceiver's audio
interface, SSB).  Requires: Python 3.10+, numpy, sounddevice (tkinter is part of Python).

    pip install numpy sounddevice pyserial
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
import hfpager_ptt as P
import hfpager_rig as G
import copy

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
def _log_dir():
    docs = os.path.join(os.path.expanduser('~'), 'Documents')
    return os.path.join(docs if os.path.isdir(docs) else os.path.expanduser('~'), 'HFPagerApp')


OUTBOX_DEFAULT = os.path.join(_log_dir(), 'outbox.txt')
LOG_FILE = os.path.join(_log_dir(), 'messages.txt')      # human-readable journal of ALL messages

DEFAULTS = dict(my_id=1, to_id=1, in_dev='', out_dev='', speed='5.86', f0=1611.4, tx_level=0.5,
                ptt_delay=0.5, afc=True, ack=False, counter=0, sens_db=4.0, auto_ack=False, only_mine=False,
                ptt_method='none', ptt_port='', ptt_baud=9600, ptt_invert=False,
                icom_model='IC-7300', icom_addr='94', kenwood_variant='TX;',
                rig_proto='ICOM', rig_port='', rig_baud=19200, rig_scen_enable=False, scenarios=None,
                outbox_enable=False, outbox_reply_none=False, outbox_ack_timeout=60, outbox_trigger='QRU?', outbox_file='')


# ============================================================================== non-GUI core
class Core:
    """Settings, audio streams, receiver engine, transmitter.  Talks to the GUI via a queue."""

    def __init__(self, ui_queue, sd_module=None):
        self.q = ui_queue
        self.sd = sd_module if sd_module is not None else sd
        self.s = dict(DEFAULTS)
        self.load()
        if not isinstance(self.s.get('scenarios'), list) or not self.s['scenarios']:
            self.s['scenarios'] = copy.deepcopy(G.DEFAULT_SCENARIOS)
        self.engine = None
        self.stream = None
        self.fs = 48000
        self.tx_busy = False
        self._seen = {}
        self._tx_thread = None
        self.pending = {}          # (counter, header crc) -> (dest, text, time) awaiting an ACK
        self.ptt = P.PttController()
        self.last_tx_error = None
        self.last_key = None
        self._deferred = {}           # (counter, header crc) -> Event set when the ACK arrives
        self.outbox = []              # deferred messages: dict(to, text, added)
        self._outbox_mtime = None
        self._outbox_lock = threading.RLock()
        self._qru_busy = set()
        self.rig = G.RigController(self.ptt, ptt_cfg=self.ptt_cfg, ptt_open=self.ptt_apply)

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
            os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
            with open(LOG_FILE, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except Exception:
            pass

    def open_log(self):
        """open the journal in the default text editor; returns error text or None"""
        try:
            if not os.path.exists(LOG_FILE):
                self.log('%s  Журнал создан' % datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            if sys.platform.startswith('win'):
                os.startfile(LOG_FILE)
            elif sys.platform == 'darwin':
                import subprocess
                subprocess.Popen(['open', LOG_FILE])
            else:
                import subprocess
                subprocess.Popen(['xdg-open', LOG_FILE])
            return None
        except Exception as ex:
            return 'Не удалось открыть %s: %s' % (LOG_FILE, ex)

    @staticmethod
    def _stamp():
        return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

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
            ev = self._deferred.get((fr['orig_counter'], fr['orig_crc']))
            if ev is not None and fr['dest'] == mine and fr['kind'] == 'ack':
                ev.set()
            self.q.put(('ack', fr, info, hit))
            self.log('%s  %s       для ID %d, исходное сообщение №%d%s' % (
                self._stamp(), 'ПОДТВЕРЖДЕНИЕ' if fr['kind'] == 'ack' else 'ОТКАЗ', fr['dest'],
                fr['orig_counter'], ' (доставлено: «%s»)' % hit[1] if hit else ''))
            return
        key = (fr['sender'], fr['dest'], fr['counter'], fr['text'])
        dup = key in self._seen and now - self._seen[key] < 120
        self._seen[key] = now
        for k in [k for k, t in self._seen.items() if now - t > 600]:
            del self._seen[k]
        self.q.put(('rx', fr, info, dup))
        self.log('%s  %s  %d → %d  №%d  (%s Бод)%s: %s' % (
            self._stamp(), 'МАЯК  ' if fr.get('b0') == 0x06 else 'ПРИЁМ  ', fr['sender'], fr['dest'],
            fr['counter'], fr['speed'], ' [повтор]' if dup else '', fr['text'].decode('cp1251', 'replace')))
        if (self.s.get('rig_scen_enable') and fr.get('kind') == 'msg' and fr['dest'] == int(self.s['my_id'])
                and not dup):
            threading.Thread(target=self.run_scenario,
                             args=(fr['text'].decode('cp1251', 'replace'), fr['sender']), daemon=True).start()
        if (self.s.get('outbox_enable') and fr.get('kind') == 'msg' and fr['dest'] == int(self.s['my_id'])
                and not dup and ' '.join(fr['text'].decode('cp1251', 'replace').upper().split())
                == ' '.join(str(self.s.get('outbox_trigger', 'QRU?')).upper().split())):
            threading.Thread(target=self.on_qru, args=(fr['sender'],), daemon=True).start()
        if (self.s.get('auto_ack') and fr.get('kind') == 'msg' and (fr['b1'] & 0x02)
                and fr['dest'] == int(self.s['my_id']) and not dup):
            threading.Timer(1.5, self.send_ack, args=(fr,)).start()

    # ---- deferred messages (sent when the addressee asks "QRU?") -----------------------------------
    OUTBOX_HEADER = ('# HFPager: отложенные сообщения. Одна строка = одно сообщение:  ID получателя|текст\n'
                     '# Строки с # и пустые игнорируются. Кодировка UTF-8. Файл можно править вручную или из другой программы:\n'
                     '# приложение перечитывает его при изменении и при запросе QRU?. Отправленные строки удаляются\n'
                     '# отсюда и дописываются в outbox_sent.txt в формате  дата время|ID|текст\n')

    def outbox_path(self):
        return self.s.get('outbox_file') or OUTBOX_DEFAULT

    def outbox_sent_path(self):
        return os.path.join(os.path.dirname(self.outbox_path()), 'outbox_sent.txt')

    @staticmethod
    def parse_outbox(text):
        items = []
        for line in text.replace('\ufeff', '').splitlines():
            line = line.strip('\r\n')
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            sep = '|' if '|' in line else ('\t' if '\t' in line else None)
            if sep is None:
                continue
            a, b = line.split(sep, 1)
            a, b = a.strip(), b.strip()
            if a.isdigit() and 0 <= int(a) <= 65535 and b:
                items.append(dict(to=int(a), text=b))
        return items

    def outbox_load(self):
        """read the file (missing file = empty list); returns error text or None"""
        with self._outbox_lock:
            path = self.outbox_path()
            try:
                if not os.path.exists(path):
                    self.outbox, self._outbox_mtime = [], None
                    return None
                with open(path, 'r', encoding='utf-8-sig', errors='replace') as f:
                    self.outbox = self.parse_outbox(f.read())
                self._outbox_mtime = os.path.getmtime(path)
                return None
            except Exception as ex:
                return 'Не удалось прочитать %s: %s' % (path, ex)

    def outbox_save(self):
        with self._outbox_lock:
            path = self.outbox_path()
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = path + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    f.write(self.OUTBOX_HEADER)
                    for it in self.outbox:
                        f.write('%d|%s\n' % (it['to'], it['text'].replace('\n', ' ').replace('\r', ' ')))
                os.replace(tmp, path)
                self._outbox_mtime = os.path.getmtime(path)
                return None
            except Exception as ex:
                return 'Не удалось записать %s: %s' % (path, ex)

    def outbox_changed_on_disk(self):
        try:
            m = os.path.getmtime(self.outbox_path()) if os.path.exists(self.outbox_path()) else None
        except Exception:
            return False
        return m != self._outbox_mtime

    def outbox_mark_sent(self, item):
        """remove the item from the file (re-reading it first, so edits made by other programs survive)
        and append it to outbox_sent.txt"""
        with self._outbox_lock:
            self.outbox_load()
            for i, it in enumerate(self.outbox):
                if it['to'] == item['to'] and it['text'] == item['text']:
                    del self.outbox[i]
                    break
            self.outbox_save()
            try:
                with open(self.outbox_sent_path(), 'a', encoding='utf-8') as f:
                    f.write('%s|%d|%s\n' % (self._stamp(), item['to'], item['text']))
            except Exception:
                pass
        self.q.put(('outbox_changed',))

    def outbox_send(self, items, first_delay=2.0, gap=2.0):
        """transmit the given deferred messages one after another (blocking; run in a thread)"""
        time.sleep(first_delay)
        sent = 0
        for it in items:
            t0 = time.time()
            while self.tx_busy and time.time() - t0 < 120:
                time.sleep(0.3)
            need_ack = not it.get('noack')
            err = self.send(it['to'], it['text'], force_ack=need_ack)
            key = self.last_key
            ev = threading.Event()
            if need_ack and key:
                self._deferred[key] = ev
            if err:
                self.sys('Отложенное сообщение для %d не отправлено: %s' % (it['to'], err))
                break
            while self.tx_busy:                 # wait until the transmission has really finished
                time.sleep(0.3)
            if self.last_tx_error:              # PTT / sound failure: keep the message in the queue
                self.sys('Отложенное сообщение для %d НЕ отправлено (останется в очереди): %s' % (
                    it['to'], self.last_tx_error))
                break
            if need_ack:
                tmo = float(self.s.get('outbox_ack_timeout', 60))
                self.sys('Отложенное сообщение для %d передано, жду подтверждение до %.0f с…' % (it['to'], tmo))
                got = ev.wait(tmo)
                self._deferred.pop(key, None)
                if not got:
                    self.sys('Подтверждения от ID %d нет – сообщение остаётся в очереди '
                             '(будет отправлено снова при следующем запросе).' % it['to'])
                    self.log('%s  ОТЛОЖЕННОЕ БЕЗ ПОДТВЕРЖДЕНИЯ  → %d: %s' % (self._stamp(), it['to'], it['text']))
                    break
                self.outbox_mark_sent(it)
                self.sys('Подтверждение от ID %d получено – сообщение удалено из очереди.' % it['to'])
            sent += 1
            self.log('%s  ОТЛОЖЕННОЕ ДОСТАВЛЕНО  → %d: %s' % (self._stamp(), it['to'], it['text']))
            time.sleep(gap)
        return sent

    def on_qru(self, sender):
        """the station `sender` asked "QRU?" - send everything waiting for it (max 5 messages)"""
        if sender in self._qru_busy:
            return
        self._qru_busy.add(sender)
        try:
            err = self.outbox_load()
            if err:
                self.sys(err)
            items = [dict(it) for it in self.outbox if it['to'] == sender][:5]
            self.log('%s  ЗАПРОС %s от ID %d: отложенных сообщений %d' % (
                self._stamp(), self.s.get('outbox_trigger', 'QRU?'), sender, len(items)))
            if items:
                self.sys('Запрос %s от ID %d – отправляю отложенные сообщения: %d' % (
                    self.s.get('outbox_trigger', 'QRU?'), sender, len(items)))
                self.outbox_send(items)
            elif self.s.get('outbox_reply_none'):
                self.sys('Запрос %s от ID %d – отложенных нет, отвечаю QRU' % (
                    self.s.get('outbox_trigger', 'QRU?'), sender))
                self.outbox_send([dict(to=sender, text='QRU', noack=True)])
            else:
                self.sys('Запрос %s от ID %d – отложенных сообщений для него нет' % (
                    self.s.get('outbox_trigger', 'QRU?'), sender))
        finally:
            self._qru_busy.discard(sender)

    # ---- transceiver CAT control / scenarios
    def rig_cfg(self):
        try:
            addr = P.parse_hex_addr(self.s.get('icom_addr', '94'))
        except Exception:
            addr = 0x94
        return dict(proto=self.s.get('rig_proto', 'ICOM'), port=self.s.get('rig_port', ''),
                    baud=int(self.s.get('rig_baud') or 9600), icom_addr=addr)

    def run_actions_logged(self, actions, why):
        """waits for the end of own transmission, then executes; returns True if all succeeded"""
        t0 = time.time()
        while self.tx_busy and time.time() - t0 < 30:
            time.sleep(0.2)
        if self.tx_busy:
            self.sys('Сценарий пропущен: идёт передача.')
            return False
        ok = True
        for a, good, msg in self.rig.run_actions(self.rig_cfg(), actions):
            ok = ok and good
            line = '%s: %s – %s' % (why, a, ('OK, ' + msg) if good else 'ОШИБКА: ' + msg)
            self.sys('Трансивер  ' + line)
            self.log('%s  ТРАНСИВЕР  %s' % (self._stamp(), line))
        return ok

    def run_scenario(self, text, sender, manual=False):
        """find the first matching scenario for a message and execute it; returns a status string"""
        try:
            hit = G.match_rows(self.s['scenarios'], text, None if manual else sender)
        except G.RigError as ex:
            self.sys('Сценарий: %s' % ex)
            return str(ex)
        if not hit:
            return None
        idx, actions = hit
        self.run_actions_logged(actions, 'сценарий %d «%s»' % (idx + 1, text))
        return actions

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
        self.log('%s  ПОДТВЕРЖДЕНИЕ ОТПРАВЛЕНО  для ID %d, сообщение №%d' % (self._stamp(), fr['sender'], fr['counter']))
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

    def send(self, to_id, text, force_ack=False):
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
        want_ack = bool(self.s['ack']) or bool(force_ack)
        audio = self.build_audio(to, my, counter, data, speed, f0, float(self.s['tx_level']),
                                 float(self.s['ptt_delay']), want_ack)
        self.last_key = None
        if want_ack:
            crc = M.header_crc(to, my, counter, data, b1=0xA2)
            self.pending[(counter, crc)] = (to, text, time.time())
            self.last_key = (counter, crc)
        self.s['counter'] = (counter + 1) & 0xFF
        self.s['to_id'] = to
        self.save()
        self.last_tx_error = None
        self.tx_busy = True
        self.q.put(('tx_start', my, to, counter, text, speed, len(audio) / self.fs))
        self._tx_thread = threading.Thread(target=self._tx_run, args=(audio, out), daemon=True)
        self._tx_thread.start()
        self.log('%s  ПЕРЕДАЧА  %d → %d  №%d  (%s Бод)%s: %s' % (
            self._stamp(), my, to, counter, speed, ' [запрос подтверждения]' if want_ack else '', text))
        return None

    def ptt_cfg(self):
        try:
            addr = P.parse_hex_addr(self.s.get('icom_addr', '94'))
        except Exception:
            addr = 0x94
        return dict(method=self.s.get('ptt_method', 'none'), port=self.s.get('ptt_port', ''),
                    baud=int(self.s.get('ptt_baud') or 9600), invert=bool(self.s.get('ptt_invert')),
                    icom_addr=addr, kenwood_variant=self.s.get('kenwood_variant', 'TX;'))

    def ptt_apply(self):
        """open / reconfigure the PTT port; returns error text or None"""
        return self.ptt.configure(self.ptt_cfg())

    def ptt_test(self, seconds=1.0):
        """key the transmitter for a short time (no audio!) – blocking, call from a worker thread"""
        if self.tx_busy:
            return 'Передача уже идёт.'
        self.tx_busy = True
        try:
            err = self.ptt_apply() or self.ptt.key(True)
            if err:
                return err
            time.sleep(seconds)
            return None
        finally:
            self.ptt.unkey_safe()
            self.tx_busy = False

    def _tx_run(self, audio, out):
        if self.engine is not None:
            self.engine.pause(True)
        err = None
        keyed = False
        try:
            if self.ptt_cfg()['method'] != 'none':
                err = self.ptt_apply() or self.ptt.key(True)
                if err:
                    err = 'PTT: %s – передача отменена' % err
                    return
                keyed = True
            self.sd.play(audio, self.fs, device=out)
            self.sd.wait()
        except Exception as ex:
            err = str(ex)
        finally:
            if keyed or self.ptt.keyed:
                time.sleep(0.15)
                uerr = self.ptt.unkey_safe()
                if uerr:
                    err = ((err + '; ') if err else '') + 'НЕ УДАЛОСЬ СНЯТЬ PTT: %s – выключите передачу вручную!' % uerr
            time.sleep(0.4)
            if self.engine is not None:
                self.engine.pause(False)
            self.last_tx_error = err
            self.tx_busy = False
            self.q.put(('tx_done', err))

    def abort_tx(self):
        try:
            self.sd.stop()
        except Exception:
            pass
        # _tx_run releases PTT right after the stream stops; extra safety if it is stuck
        if self.ptt.keyed and not self.tx_busy:
            self.ptt.unkey_safe()


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
        only_mine=tk.BooleanVar(value=core.s['only_mine']),
        sens_db=tk.StringVar(value=str(core.s['sens_db'])),
        ptt_method=tk.StringVar(value=core.s['ptt_method']),
        ptt_port=tk.StringVar(value=core.s['ptt_port']),
        ptt_baud=tk.StringVar(value=str(core.s['ptt_baud'])),
        ptt_invert=tk.BooleanVar(value=core.s['ptt_invert']),
        icom_model=tk.StringVar(value=core.s['icom_model']),
        icom_addr=tk.StringVar(value=str(core.s['icom_addr'])),
        kenwood_variant=tk.StringVar(value=core.s['kenwood_variant']),
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
    ttk.Checkbutton(opt, text='Запрос подтверждения', variable=var['ack']).pack(side='left', padx=4)
    ttk.Checkbutton(opt, text='Автоподтверждение', variable=var['auto_ack']).pack(side='left', padx=4)
    ttk.Checkbutton(opt, text='Фильтр ID', variable=var['only_mine']).pack(
        side='left', padx=(12, 4))

    # ---------------------------------------------------------------- PTT / CAT panel
    pf = ttk.LabelFrame(root, text='Управление передачей (PTT / CAT)')
    pf.pack(fill='x', padx=8, pady=(0, 4))
    pr1 = ttk.Frame(pf)
    pr1.pack(fill='x', pady=2)
    ttk.Label(pr1, text='Метод:').pack(side='left', padx=4)
    cb_pm = ttk.Combobox(pr1, textvariable=var['ptt_method'], values=P.METHODS, width=11, state='readonly')
    cb_pm.pack(side='left')
    ttk.Label(pr1, text='COM-порт:').pack(side='left', padx=(10, 4))
    cb_pp = ttk.Combobox(pr1, textvariable=var['ptt_port'], width=34)
    cb_pp.pack(side='left')
    ttk.Button(pr1, text='⟳', width=3, command=lambda: refresh_ports()).pack(side='left', padx=2)
    ttk.Label(pr1, text='Скорость:').pack(side='left', padx=(10, 4))
    ttk.Combobox(pr1, textvariable=var['ptt_baud'], values=[str(b) for b in P.BAUDS], width=7).pack(side='left')
    ttk.Checkbutton(pr1, text='Инверсия (линия)', variable=var['ptt_invert']).pack(side='left', padx=8)
    pr2 = ttk.Frame(pf)
    pr2.pack(fill='x', pady=2)
    ttk.Label(pr2, text='ICOM модель:').pack(side='left', padx=4)
    cb_im = ttk.Combobox(pr2, textvariable=var['icom_model'], values=list(P.ICOM_MODELS), width=16,
                         state='readonly')
    cb_im.pack(side='left')
    ttk.Label(pr2, text='CI-V адрес (hex):').pack(side='left', padx=(8, 4))
    ttk.Entry(pr2, textvariable=var['icom_addr'], width=5).pack(side='left')
    ttk.Label(pr2, text='KENWOOD команда:').pack(side='left', padx=(12, 4))
    ttk.Combobox(pr2, textvariable=var['kenwood_variant'], values=P.KENWOOD_VARIANTS, width=6,
                 state='readonly').pack(side='left')
    btn_ptt_test = ttk.Button(pr2, text='Тест PTT (1 с, без звука)')
    btn_ptt_test.pack(side='left', padx=14)
    ttk.Button(pr2, text='Трансивер / сценарии…', command=lambda: open_rig_window()).pack(side='left', padx=4)
    ttk.Button(pr2, text='Отложенные сообщения…', command=lambda: open_outbox_window()).pack(side='left', padx=4)
    ttk.Label(pf, text='RTS/DTR: «1» на линии COM-порта на время передачи. YAESU: TX1;/TX0;  '
                       'YAESU-OLD: 5-байтовый CAT (FT-817/857/897)  KENWOOD/ELECRAFT: TX;/RX;  '
                       'ICOM: CI-V 1C 00. Сначала проверяйте на эквиваленте нагрузки.',
              foreground='#777777', wraplength=900).pack(anchor='w', padx=4)

    # ---------------------------------------------------------------- chat
    mid = ttk.LabelFrame(root, text='Общий обмен сообщениями')
    mid.pack(fill='both', expand=True, padx=8, pady=4)
    CF = 13 if sys.platform == 'darwin' else 12            # chat font size
    GREEN, GREEN_DIM, RED, YELLOW = '#33ff33', '#1f9e1f', '#ff4a4a', '#ffee33'
    CYAN, CYAN_DIM = '#4fd8ff', '#2b8099'
    chat = tk.Text(mid, wrap='word', state='disabled', font=(MONO_FONT, CF), background='#000000',
                   foreground=GREEN, insertbackground=GREEN, selectbackground='#145214', padx=6, pady=4)
    sb = ttk.Scrollbar(mid, command=chat.yview)
    chat.configure(yscrollcommand=sb.set)
    sb.pack(side='right', fill='y')
    chat.pack(fill='both', expand=True)
    chat.tag_configure('tome', foreground=GREEN, font=(MONO_FONT, CF, 'bold'))     # received, addressed to me
    chat.tag_configure('rx', foreground=CYAN)                                       # received, addressed to others
    chat.tag_configure('beacon', foreground=CYAN)
    chat.tag_configure('dup_o', foreground=CYAN_DIM)
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
        core.s['only_mine'] = bool(var['only_mine'].get())
        core.s['ptt_method'] = var['ptt_method'].get()
        core.s['ptt_port'] = var['ptt_port'].get().split(' - ')[0].strip()
        try:
            core.s['ptt_baud'] = int(var['ptt_baud'].get())
        except ValueError:
            pass
        core.s['ptt_invert'] = bool(var['ptt_invert'].get())
        core.s['icom_model'] = var['icom_model'].get()
        core.s['icom_addr'] = var['icom_addr'].get().strip() or '94'
        core.s['kenwood_variant'] = var['kenwood_variant'].get()

    def refresh_ports():
        ports = P.list_ports()
        cb_pp['values'] = [lbl for _, lbl in ports]
        cur = var['ptt_port'].get().split(' - ')[0].strip()
        if not cur and ports:
            var['ptt_port'].set(ports[0][1])

    def on_ptt_method(*_):
        m = var['ptt_method'].get()
        var['ptt_baud'].set(str(P.DEFAULT_BAUD.get(m, 9600)))

    def on_icom_model(*_):
        a = P.ICOM_MODELS.get(var['icom_model'].get())
        if a is not None:
            var['icom_addr'].set('%02X' % a)

    def ptt_test():
        pull_settings()
        core.save()
        if core.s['ptt_method'] == 'none':
            messagebox.showinfo('PTT', 'Метод PTT = none: управлять нечем.')
            return
        if not messagebox.askokcancel('Тест PTT', 'Трансивер включится на передачу примерно на 1 секунду '
                                      '(звука не будет). Нагрузка подключена, диапазон свободен?'):
            return
        btn_ptt_test.state(['disabled'])

        def work():
            err = core.ptt_test(1.0)
            q.put(('ptt_test', err))
        threading.Thread(target=work, daemon=True).start()

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

    # ---------------------------------------------------------------- transceiver panel + scenarios
    rig_ui = {}

    def open_rig_window():
        w0 = rig_ui.get('win')
        if w0 is not None:
            try:
                w0.deiconify()
                w0.lift()
                return
            except Exception:
                rig_ui.clear()
        w = tk.Toplevel(root)
        w.title('Трансивер (CAT) и сценарии')
        rig_ui['win'] = w
        rv = dict(
            proto=tk.StringVar(value=core.s['rig_proto']), port=tk.StringVar(value=core.s['rig_port']),
            baud=tk.StringVar(value=str(core.s['rig_baud'])), enable=tk.BooleanVar(value=core.s['rig_scen_enable']),
            freq=tk.StringVar(value='7.100'), mode=tk.StringVar(value='USB'), cur=tk.StringVar(value='—'),
            on=tk.BooleanVar(value=True), trig=tk.StringVar(), frm=tk.StringVar(), act=tk.StringVar(),
            test=tk.StringVar(), status=tk.StringVar(value=''))
        rig_ui['cur'] = rv['cur']
        rig_ui['status'] = rv['status']

        def sync(save=True):
            pull_settings()
            core.s['rig_proto'] = rv['proto'].get()
            core.s['rig_port'] = rv['port'].get().split(' - ')[0].strip()
            try:
                core.s['rig_baud'] = int(rv['baud'].get())
            except ValueError:
                pass
            core.s['rig_scen_enable'] = bool(rv['enable'].get())
            if save:
                core.save()

        def bg(fn):
            sync()
            threading.Thread(target=fn, daemon=True).start()

        def run_text(text):
            bg(lambda: core.run_actions_logged(text, 'панель'))

        # --- connection
        cf = ttk.LabelFrame(w, text='Связь с трансивером (CAT)')
        cf.pack(fill='x', padx=8, pady=6)
        ttk.Label(cf, text='Протокол:').pack(side='left', padx=4)
        ttk.Combobox(cf, textvariable=rv['proto'], values=G.RIG_PROTOS, width=11, state='readonly').pack(side='left')
        ttk.Label(cf, text='COM-порт:').pack(side='left', padx=(10, 4))
        cb_rp = ttk.Combobox(cf, textvariable=rv['port'], values=[l for _, l in P.list_ports()], width=30)
        cb_rp.pack(side='left')
        ttk.Label(cf, text='Скорость:').pack(side='left', padx=(10, 4))
        ttk.Combobox(cf, textvariable=rv['baud'], values=[str(b) for b in P.BAUDS], width=7).pack(side='left')

        def same_as_ptt():
            pull_settings()
            m = core.s['ptt_method']
            if m in G.RIG_PROTOS:
                rv['proto'].set(m)
            rv['port'].set(core.s['ptt_port'])
            rv['baud'].set(str(core.s['ptt_baud']))
        ttk.Button(cf, text='Как в PTT', command=same_as_ptt).pack(side='left', padx=8)
        ttk.Label(cf, text='(CI-V адрес ICOM — в панели PTT)', foreground='#777777').pack(side='left')

        # --- panel
        pnl = ttk.LabelFrame(w, text='Управление')
        pnl.pack(fill='x', padx=8, pady=4)
        r1 = ttk.Frame(pnl)
        r1.pack(fill='x', pady=3)
        ttk.Label(r1, text='Частота, МГц:').pack(side='left', padx=4)
        ttk.Entry(r1, textvariable=rv['freq'], width=11).pack(side='left')
        ttk.Label(r1, text='Режим:').pack(side='left', padx=(10, 4))
        ttk.Combobox(r1, textvariable=rv['mode'], values=G.MODES, width=7, state='readonly').pack(side='left')
        ttk.Button(r1, text='Установить', command=lambda: run_text('FRQ %s %s' % (
            rv['freq'].get().strip() or '0', rv['mode'].get()))).pack(side='left', padx=6)
        ttk.Button(r1, text='Только режим', command=lambda: run_text('MODE %s' % rv['mode'].get())).pack(
            side='left')

        def read_now():
            def work():
                try:
                    hz = core.rig.read_freq(core.rig_cfg())
                    q.put(('rig_freq', '%s МГц' % G.fmt_mhz(hz)))
                except G.RigError as ex:
                    q.put(('rig_freq', 'ошибка: %s' % ex))
            bg(work)
        ttk.Button(r1, text='Прочитать частоту', command=read_now).pack(side='left', padx=10)
        ttk.Label(r1, textvariable=rv['cur'], font=(MONO_FONT, CF, 'bold')).pack(side='left', padx=4)
        r2 = ttk.Frame(pnl)
        r2.pack(fill='x', pady=3)
        ttk.Label(r2, text='Диапазон, м:').pack(side='left', padx=4)
        for b in G.BANDS:
            ttk.Button(r2, text=str(b), width=4, command=lambda b=b: run_text('BND %d' % b)).pack(side='left')
        r3 = ttk.Frame(pnl)
        r3.pack(fill='x', pady=3)
        ttk.Label(r3, text='Шаг, кГц:').pack(side='left', padx=4)
        for lbl, act in (('−10', 'DOWN 10'), ('−1', 'DOWN 1'), ('−0.1', 'DOWN 0.1'),
                         ('+0.1', 'UP 0.1'), ('+1', 'UP 1'), ('+10', 'UP 10')):
            ttk.Button(r3, text=lbl, width=5, command=lambda a=act: run_text(a)).pack(side='left')

        # --- scenarios
        sf = ttk.LabelFrame(w, text='Сценарии: действия при получении сообщения')
        sf.pack(fill='both', expand=True, padx=8, pady=4)
        ttk.Checkbutton(sf, text='Выполнять сценарии при получении сообщений, адресованных моему ID',
                        variable=rv['enable'], command=lambda: sync()).pack(anchor='w', padx=4, pady=2)
        tv = ttk.Treeview(sf, columns=('n', 'on', 'trigger', 'from', 'action'), show='headings', height=9)
        for c, t, wd in (('n', '№', 32), ('on', 'Вкл', 40), ('trigger', 'Текст сообщения (* = любое слово)', 230),
                         ('from', 'От ID (пусто = любой)', 130), ('action', 'Действие', 330)):
            tv.heading(c, text=t)
            tv.column(c, width=wd, anchor='w')
        tv.pack(fill='both', expand=True, padx=4, pady=2)

        def fill_table(sel=None):
            for i in tv.get_children():
                tv.delete(i)
            for n, r in enumerate(core.s['scenarios']):
                tv.insert('', 'end', iid=str(n), values=(n + 1, '✔' if r.get('on', True) else '',
                                                         r.get('trigger', ''), r.get('from', ''),
                                                         r.get('action', '')))
            if sel is not None and str(sel) in tv.get_children():
                tv.selection_set(str(sel))

        def selected():
            sel = tv.selection()
            return int(sel[0]) if sel else None

        def on_select(*_):
            n = selected()
            if n is None or n >= len(core.s['scenarios']):
                return
            r = core.s['scenarios'][n]
            rv['on'].set(bool(r.get('on', True)))
            rv['trig'].set(r.get('trigger', ''))
            rv['frm'].set(str(r.get('from', '')))
            rv['act'].set(r.get('action', ''))

        def row_from_form():
            return {'on': bool(rv['on'].get()), 'trigger': rv['trig'].get().strip().upper(),
                    'from': rv['frm'].get().strip(), 'action': rv['act'].get().strip()}

        def valid(row):
            if not row['trigger'] or not row['action']:
                messagebox.showwarning('Сценарий', 'Заполните текст сообщения и действие.')
                return False
            return True

        def add_row():
            r = row_from_form()
            if valid(r):
                core.s['scenarios'].append(r)
                core.save()
                fill_table(len(core.s['scenarios']) - 1)

        def replace_row():
            n, r = selected(), row_from_form()
            if n is None:
                messagebox.showinfo('Сценарий', 'Выберите строку таблицы.')
            elif valid(r):
                core.s['scenarios'][n] = r
                core.save()
                fill_table(n)

        def delete_row():
            n = selected()
            if n is not None:
                del core.s['scenarios'][n]
                core.save()
                fill_table()

        def move_row(d):
            n = selected()
            if n is None or not (0 <= n + d < len(core.s['scenarios'])):
                return
            sc = core.s['scenarios']
            sc[n], sc[n + d] = sc[n + d], sc[n]
            core.save()
            fill_table(n + d)

        def defaults_rows():
            if messagebox.askokcancel('Сценарии', 'Заменить таблицу стандартным набором сценариев?'):
                core.s['scenarios'] = copy.deepcopy(G.DEFAULT_SCENARIOS)
                core.save()
                fill_table()

        tv.bind('<<TreeviewSelect>>', on_select)
        ef = ttk.Frame(sf)
        ef.pack(fill='x', pady=2)
        ttk.Checkbutton(ef, text='Вкл', variable=rv['on']).pack(side='left', padx=4)
        ttk.Label(ef, text='Сообщение:').pack(side='left')
        ttk.Entry(ef, textvariable=rv['trig'], width=24).pack(side='left', padx=2)
        ttk.Label(ef, text='От ID:').pack(side='left', padx=(6, 0))
        ttk.Entry(ef, textvariable=rv['frm'], width=10).pack(side='left', padx=2)
        ttk.Label(ef, text='Действие:').pack(side='left', padx=(6, 0))
        ttk.Entry(ef, textvariable=rv['act'], width=34).pack(side='left', padx=2)
        bf = ttk.Frame(sf)
        bf.pack(fill='x', pady=2)
        for t, c in (('Добавить', add_row), ('Заменить', replace_row), ('Удалить', delete_row),
                     ('▲', lambda: move_row(-1)), ('▼', lambda: move_row(1)), ('Стандартные', defaults_rows)):
            ttk.Button(bf, text=t, command=c, width=11 if len(t) > 2 else 4).pack(side='left', padx=3)
        tf = ttk.Frame(sf)
        tf.pack(fill='x', pady=3)
        ttk.Label(tf, text='Проверка (имитация сообщения):').pack(side='left', padx=4)
        ttk.Entry(tf, textvariable=rv['test'], width=28).pack(side='left')

        def test_msg():
            text = rv['test'].get().strip()

            def work():
                res = core.run_scenario(text, None, manual=True)
                q.put(('rig_status', 'нет подходящего сценария' if res is None else 'выполнено: ' + res))
            bg(work)
        ttk.Button(tf, text='Выполнить', command=test_msg).pack(side='left', padx=6)
        ttk.Label(tf, textvariable=rv['status'], foreground='#777777').pack(side='left', padx=4)
        ttk.Label(sf, text='Действия (через «;»): BND <м> · FRQ <МГц> [режим] · MODE <режим> · UP/DOWN <кГц> · '
                           'WAIT <с>.  Параметры из сообщения: {1}, {2}…  Режимы: ' + ' '.join(G.MODES) +
                           '.  Передатчик сценарии не включают. Выполняется только первый подходящий сценарий '
                           '(порядок сверху вниз); для защиты укажите «От ID».',
                  foreground='#777777', wraplength=820).pack(anchor='w', padx=4, pady=2)
        fill_table()

        def close_win():
            sync()
            rig_ui.clear()
            w.destroy()
        w.protocol('WM_DELETE_WINDOW', close_win)

    # ---------------------------------------------------------------- deferred messages window
    ob_ui = {}

    def open_outbox_window():
        w0 = ob_ui.get('win')
        if w0 is not None:
            try:
                w0.deiconify()
                w0.lift()
                return
            except Exception:
                ob_ui.clear()
        w = tk.Toplevel(root)
        w.title('Отложенные сообщения')
        try:
            w.transient(root)
        except Exception:
            pass
        ob_ui['win'] = w
        ov = dict(enable=tk.BooleanVar(value=core.s['outbox_enable']),
                  reply=tk.BooleanVar(value=core.s['outbox_reply_none']),
                  trig=tk.StringVar(value=core.s['outbox_trigger']),
                  tmo=tk.StringVar(value=str(core.s['outbox_ack_timeout'])),
                  file=tk.StringVar(value=core.s['outbox_file'] or core.outbox_path()),
                  to=tk.StringVar(value=''), text=tk.StringVar(value=''), info=tk.StringVar(value=''))

        def sync():
            core.s['outbox_enable'] = bool(ov['enable'].get())
            core.s['outbox_reply_none'] = bool(ov['reply'].get())
            core.s['outbox_trigger'] = ov['trig'].get().strip() or 'QRU?'
            try:
                core.s['outbox_ack_timeout'] = max(10.0, min(600.0, float(ov['tmo'].get().replace(',', '.'))))
            except ValueError:
                pass
            path = ov['file'].get().strip()
            changed = path != core.outbox_path()
            core.s['outbox_file'] = '' if path == OUTBOX_DEFAULT else path
            core.save()
            return changed

        top2 = ttk.LabelFrame(w, text='Режим «QRU?»')
        top2.pack(fill='x', padx=8, pady=6)
        ttk.Checkbutton(top2, text='При получении запроса от ID отправлять ему его отложенные сообщения',
                        variable=ov['enable'], command=sync).pack(anchor='w', padx=4)
        r1 = ttk.Frame(top2)
        r1.pack(fill='x', pady=2)
        ttk.Label(r1, text='Текст запроса:').pack(side='left', padx=4)
        ttk.Entry(r1, textvariable=ov['trig'], width=10).pack(side='left')
        ttk.Label(r1, text='Ждать подтверждение, с:').pack(side='left', padx=(12, 4))
        ttk.Entry(r1, textvariable=ov['tmo'], width=5).pack(side='left')
        ttk.Checkbutton(r1, text='Если для него ничего нет — отвечать «QRU»', variable=ov['reply'],
                        command=sync).pack(side='left', padx=12)
        r2 = ttk.Frame(top2)
        r2.pack(fill='x', pady=2)
        ttk.Label(r2, text='TXT-файл:').pack(side='left', padx=4)
        ttk.Entry(r2, textvariable=ov['file'], width=62).pack(side='left')

        tv = ttk.Treeview(w, columns=('n', 'to', 'text'), show='headings', height=10)
        for c, t, wd in (('n', '№', 36), ('to', 'Кому (ID)', 90), ('text', 'Текст сообщения', 620)):
            tv.heading(c, text=t)
            tv.column(c, width=wd, anchor='w')
        tv.pack(fill='both', expand=True, padx=8, pady=4)

        def fill(sel=None):
            for i in tv.get_children():
                tv.delete(i)
            for n, it in enumerate(core.outbox):
                tv.insert('', 'end', iid=str(n), values=(n + 1, it['to'], it['text']))
            if sel is not None and str(sel) in tv.get_children():
                tv.selection_set(str(sel))
            ov['info'].set('Сообщений в очереди: %d' % len(core.outbox))

        def selected():
            sel = tv.selection()
            return int(sel[0]) if sel else None

        def on_select(*_):
            n = selected()
            if n is not None and n < len(core.outbox):
                ov['to'].set(str(core.outbox[n]['to']))
                ov['text'].set(core.outbox[n]['text'])

        def read_form():
            to, text = ov['to'].get().strip(), ov['text'].get().strip()
            if not (to.isdigit() and 0 <= int(to) <= 65535):
                messagebox.showwarning('Отложенные', 'ID получателя: число 0…65535.')
                return None
            n = len(text.encode('cp1251', 'replace'))
            if not (1 <= n <= 252):
                messagebox.showwarning('Отложенные', 'Длина текста: 1…252 байта (сейчас %d).' % n)
                return None
            return dict(to=int(to), text=text.replace('\n', ' '))

        def save_back():
            err = core.outbox_save()
            if err:
                messagebox.showwarning('Отложенные', err)

        def add_item():
            it = read_form()
            if it:
                core.outbox_load()
                core.outbox.append(it)
                save_back()
                fill(len(core.outbox) - 1)

        def replace_item():
            n, it = selected(), read_form()
            if n is None:
                messagebox.showinfo('Отложенные', 'Выберите строку.')
            elif it:
                core.outbox[n] = it
                save_back()
                fill(n)

        def delete_item():
            n = selected()
            if n is not None:
                del core.outbox[n]
                save_back()
                fill()

        def send_now():
            n = selected()
            if n is None:
                messagebox.showinfo('Отложенные', 'Выберите строку.')
                return
            it = dict(core.outbox[n])
            threading.Thread(target=lambda: core.outbox_send([it], first_delay=0.0), daemon=True).start()

        def reload_file():
            sync()
            err = core.outbox_load()
            fill()
            if err:
                messagebox.showwarning('Отложенные', err)

        def open_file():
            sync()
            if not os.path.exists(core.outbox_path()):
                save_back()
            try:
                if sys.platform.startswith('win'):
                    os.startfile(core.outbox_path())
                else:
                    import subprocess
                    subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', core.outbox_path()])
            except Exception as ex:
                messagebox.showwarning('Отложенные', 'Не удалось открыть файл: %s' % ex)

        tv.bind('<<TreeviewSelect>>', on_select)
        ef = ttk.Frame(w)
        ef.pack(fill='x', padx=8, pady=2)
        ttk.Label(ef, text='Кому (ID):').pack(side='left')
        e_to = ttk.Entry(ef, textvariable=ov['to'], width=8)
        e_to.pack(side='left', padx=3)
        ttk.Label(ef, text='Текст:').pack(side='left', padx=(8, 0))
        e_text = ttk.Entry(ef, textvariable=ov['text'], width=60)
        e_text.pack(side='left', padx=3)
        for e_ in (e_to, e_text):       # click always gives the field the keyboard focus (Windows quirk)
            e_.bind('<Button-1>', lambda ev: (ev.widget.focus_force(), None)[1])
        bf = ttk.Frame(w)
        bf.pack(fill='x', padx=8, pady=2)
        for t, c in (('Добавить', add_item), ('Заменить', replace_item), ('Удалить', delete_item),
                     ('Отправить сейчас', send_now), ('Перечитать файл', reload_file), ('Открыть TXT', open_file)):
            ttk.Button(bf, text=t, command=c).pack(side='left', padx=3)
        ttk.Label(w, textvariable=ov['info'], foreground='#777777').pack(anchor='w', padx=10)
        ttk.Label(w, text='Формат файла (UTF-8): одна строка = одно сообщение,  ID|текст  — например  10048|Привет, жду на 7.185.  '
                          'Сообщение удаляется из очереди только после получения подтверждения доставки (отправляется с запросом подтверждения); без подтверждения остаётся. Строки с # игнорируются. Приложение само перечитывает файл при изменении, так что '
                          'сообщения можно добавлять из другого менеджера. Отправленные строки удаляются из файла '
                          'и дописываются в outbox_sent.txt (дата время|ID|текст).',
                  foreground='#777777', wraplength=780).pack(anchor='w', padx=10, pady=(0, 6))

        core.outbox_load()
        fill()
        ob_ui['fill'] = fill

        def watch():
            if ob_ui.get('win') is None:
                return
            try:
                if core.outbox_changed_on_disk():
                    core.outbox_load()
                    fill()
                root.after(3000, watch)
            except Exception:
                pass
        root.after(3000, watch)

        def close():
            sync()
            ob_ui.clear()
            w.destroy()
        w.protocol('WM_DELETE_WINDOW', close)

    btn.configure(command=on_send)
    btn_ptt_test.configure(command=ptt_test)
    cb_pm.bind('<<ComboboxSelected>>', on_ptt_method)
    cb_im.bind('<<ComboboxSelected>>', on_icom_model)
    btn_stop.configure(command=core.abort_tx)
    ent.bind('<Return>', on_send)
    var['msg'].trace_add('write', update_counter)
    var['speed'].trace_add('write', update_counter)
    for k in ('in_dev',):
        cb_in.bind('<<ComboboxSelected>>', restart_rx)
    ttk.Button(top, text='Журнал (TXT)', command=lambda: (lambda e: e and messagebox.showwarning(
        'Журнал', e))(core.open_log())).grid(row=2, column=4, padx=6)
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
            hide = bool(core.s.get('only_mine')) and (fr['b0'] == 0x06 or fr['dest'] != mine)
            if hide:
                pass
            elif fr['b0'] == 0x06:
                add('[%s] ★ Маяк от %d: %s  (%s Бод)' % (now(), fr['sender'], text, fr['speed']), 'beacon')
            else:
                tome = fr['dest'] == mine
                tag = ('dup' if tome else 'dup_o') if dup else ('tome' if tome else 'rx')
                line = '[%s] %d → %d  №%d  (%s Бод, АПЧ %.0f Гц): %s%s' % (
                    now(), fr['sender'], fr['dest'], fr['counter'], fr['speed'],
                    info.get('afc_hz', 0.0), text, '   (повтор)' if dup else '')
                add(line, tag)
                if (fr['b1'] & 0x02) and fr['dest'] == mine and not dup:
                    add('      запрошено подтверждение доставки%s' % (
                        ' – отправляю' if core.s.get('auto_ack') else
                        ' (автоподтверждение выключено)'), 'sys')
            var['info'].set('Последний кадр: центр %.0f Гц, диапазон АПЧ %.1f Гц' %
                            (info.get('center_hz', 0), info.get('afc_hz', 0)))
            rx_state['flash_until'] = time.time() + 1.5
            update_lamps()
        elif kind == 'ack':
            _, fr, info, hit = item
            who = 'ПОДТВЕРЖДЕНИЕ' if fr['kind'] == 'ack' else 'ОТКАЗ'
            if core.s.get('only_mine') and not hit and fr['dest'] != int(core.s['my_id']):
                pass
            elif hit:
                add('[%s] ✔ %s: сообщение №%d для %d доставлено («%s»)' % (
                    now(), who, fr['orig_counter'], hit[0], hit[1]), 'note')
            else:
                add('[%s] ✔ %s для ID %d (исходное сообщение №%d, CRC %04X)' % (
                    now(), who, fr['dest'], fr['orig_counter'], fr['orig_crc']), 'note')
        elif kind == 'outbox_changed':
            if ob_ui.get('fill') is not None:
                core.outbox_load()
                ob_ui['fill']()
        elif kind == 'rig_freq':
            if rig_ui.get('cur') is not None:
                rig_ui['cur'].set(item[1])
        elif kind == 'rig_status':
            if rig_ui.get('status') is not None:
                rig_ui['status'].set(item[1])
        elif kind == 'ptt_test':
            btn_ptt_test.state(['!disabled'])
            add('[%s] • Тест PTT: %s' % (now(), item[1] or 'выполнен (включено и выключено)'), 'sys')
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
                try:
                    handle(q.get_nowait())
                except queue.Empty:
                    raise
                except Exception as ex:             # a failing handler must never stop the UI loop
                    import traceback
                    core.log('%s  ОШИБКА ИНТЕРФЕЙСА: %s' % (core._stamp(), traceback.format_exc().replace('\n', ' | ')))
                    add('[%s] • Ошибка интерфейса: %s' % (now(), ex), 'sys')
        except queue.Empty:
            pass
        try:
            update_lamps()
        except Exception:
            pass
        root.after(100, poll)

    def on_close():
        try:
            pull_settings()
            core.save()
            core.stop_rx()
            core.ptt.close()
            core.rig.close()
        finally:
            root.destroy()

    root.protocol('WM_DELETE_WINDOW', on_close)
    refresh_devices()
    refresh_ports()
    update_counter()
    if sd is None:
        add('• Библиотека sounddevice/PortAudio недоступна: %s' % SD_ERROR, 'sys')
        add('• Установите: pip install sounddevice numpy', 'sys')
    else:
        add('• Передача и приём идут через выбранные звуковые устройства.', 'sys')
        restart_rx()
    poll()
    root.mainloop()


if __name__ == '__main__':
    run_gui()
