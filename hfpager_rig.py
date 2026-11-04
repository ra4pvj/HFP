"""CAT control of the transceiver (frequency / mode / band) and message-triggered scenarios.

Protocols: ICOM (CI-V), YAESU (new "FA...;/MD0x;"), YAESU-OLD (5-byte), KENWOOD, ELECRAFT.
Only frequency / mode commands are generated here - nothing in this module can key the transmitter.

Scenario action language (several actions separated by ';'):
    BND <m>              change band, e.g. BND 40 (default frequency/mode from BANDS)
    FRQ <MHz> [mode]     set frequency (7.185 | 7185 kHz | 7185000 Hz) and optionally the mode
    MODE <mode>          LSB USB CW CW-R AM FM RTTY USB-D LSB-D
    UP <kHz> / DOWN <kHz> shift the current frequency (reads it first)
    WAIT <sec>           pause (max 10 s)
Trigger patterns: words separated by spaces, '*' = any single word, captured as {1}, {2} ... in the action.
"""
import re
import time
import threading

import hfpager_ptt as P

MODES = ['LSB', 'USB', 'CW', 'CW-R', 'AM', 'FM', 'RTTY', 'USB-D', 'LSB-D']
BANDS = {   # metres: (default MHz, mode)
    160: (1.900, 'LSB'), 80: (3.600, 'LSB'), 60: (5.357, 'USB'), 40: (7.100, 'LSB'), 30: (10.130, 'USB'),
    20: (14.200, 'USB'), 17: (18.130, 'USB'), 15: (21.300, 'USB'), 12: (24.950, 'USB'),
    10: (28.400, 'USB'), 6: (50.150, 'USB'),
}
F_MIN_HZ, F_MAX_HZ = 30_000, 470_000_000

_MODE_ALIAS = {'DATA': 'USB-D', 'DIGI': 'USB-D', 'PKT': 'USB-D', 'USBD': 'USB-D', 'USB-D': 'USB-D',
               'USB-DATA': 'USB-D', 'LSBD': 'LSB-D', 'LSB-D': 'LSB-D', 'LSB-DATA': 'LSB-D',
               'CWR': 'CW-R', 'CW-R': 'CW-R', 'FSK': 'RTTY', 'RTTY': 'RTTY'}

ICOM_MODE = {'LSB': 0x00, 'USB': 0x01, 'AM': 0x02, 'CW': 0x03, 'RTTY': 0x04, 'FM': 0x05, 'CW-R': 0x07,
             'USB-D': 0x01, 'LSB-D': 0x00}
YAESU_MODE = {'LSB': '1', 'USB': '2', 'CW': '3', 'FM': '4', 'AM': '5', 'RTTY': '6', 'CW-R': '7',
              'LSB-D': '8', 'USB-D': 'C'}
YAESU_OLD_MODE = {'LSB': 0x00, 'USB': 0x01, 'CW': 0x02, 'CW-R': 0x03, 'AM': 0x04, 'FM': 0x08, 'RTTY': 0x0A,
                  'USB-D': 0x0A, 'LSB-D': 0x0A}          # 0x0A = DIG
KENWOOD_MODE = {'LSB': '1', 'USB': '2', 'CW': '3', 'FM': '4', 'AM': '5', 'RTTY': '6', 'CW-R': '7',
                'USB-D': '2', 'LSB-D': '1'}
ELECRAFT_MODE = {'LSB': '1', 'USB': '2', 'CW': '3', 'FM': '4', 'AM': '5', 'RTTY': '6', 'CW-R': '7',
                 'USB-D': '6', 'LSB-D': '9'}
RIG_PROTOS = ['ICOM', 'YAESU', 'YAESU-OLD', 'KENWOOD', 'ELECRAFT']


class RigError(Exception):
    pass


# ------------------------------------------------------------------ pure helpers
def norm_mode(text):
    t = str(text).strip().upper()
    t = _MODE_ALIAS.get(t, t)
    if t not in MODES:
        raise RigError('неизвестный режим «%s» (доступно: %s)' % (text, ' '.join(MODES)))
    return t


def parse_freq(text):
    """'7.185' / '7,185' (MHz), '7185' (kHz), '7185000' (Hz)  ->  int Hz"""
    try:
        v = float(str(text).strip().lower().replace(',', '.').replace('mhz', '').replace('khz', ''))
    except ValueError:
        raise RigError('не удалось разобрать частоту «%s»' % text)
    if v >= 100_000:
        hz = v
    elif v >= 1000:
        hz = v * 1000
    else:
        hz = v * 1_000_000
    hz = int(round(hz))
    if not (F_MIN_HZ <= hz <= F_MAX_HZ):
        raise RigError('частота %s вне допустимых пределов' % text)
    return hz


def fmt_mhz(hz):
    return '%.5f' % (hz / 1e6)


def bcd_icom(hz):
    s = '%010d' % hz
    return bytes(int(s[i:i + 2], 16) for i in (8, 6, 4, 2, 0))


def icom_frame(addr, payload):
    return bytes([0xFE, 0xFE, addr & 0xFF, 0xE0]) + bytes(payload) + b'\xFD'


def frames_set_freq(proto, hz, icom_addr=0x94):
    if proto == 'ICOM':
        return [icom_frame(icom_addr, b'\x05' + bcd_icom(hz))]
    if proto == 'YAESU':
        return [('FA%09d;' % hz).encode()]
    if proto == 'YAESU-OLD':
        s = '%08d' % (hz // 10)
        return [bytes(int(s[i:i + 2], 16) for i in (0, 2, 4, 6)) + b'\x01']
    if proto in ('KENWOOD', 'ELECRAFT'):
        return [('FA%011d;' % hz).encode()]
    raise RigError('неизвестный протокол %s' % proto)


def frames_set_mode(proto, mode, icom_addr=0x94):
    mode = norm_mode(mode)
    if proto == 'ICOM':
        fr = [icom_frame(icom_addr, bytes([0x06, ICOM_MODE[mode], 0x01]))]
        if mode in ('USB', 'LSB'):
            fr.append(icom_frame(icom_addr, bytes([0x1A, 0x06, 0x00])))              # DATA off
        elif mode in ('USB-D', 'LSB-D'):
            fr.append(icom_frame(icom_addr, bytes([0x1A, 0x06, 0x01, 0x01])))        # DATA on, FIL1
        return fr
    if proto == 'YAESU':
        return [('MD0%s;' % YAESU_MODE[mode]).encode()]
    if proto == 'YAESU-OLD':
        return [bytes([YAESU_OLD_MODE[mode], 0, 0, 0, 0x07])]
    if proto == 'KENWOOD':
        return [('MD%s;' % KENWOOD_MODE[mode]).encode()]
    if proto == 'ELECRAFT':
        return [('MD%s;' % ELECRAFT_MODE[mode]).encode()]
    raise RigError('неизвестный протокол %s' % proto)


def frame_read_freq(proto, icom_addr=0x94):
    if proto == 'ICOM':
        return icom_frame(icom_addr, b'\x03')
    if proto in ('YAESU', 'KENWOOD', 'ELECRAFT'):
        return b'FA;'
    return bytes([0, 0, 0, 0, 0x03])


def parse_freq_reply(proto, data, icom_addr=0x94):
    """-> Hz or None"""
    if proto == 'ICOM':
        key = bytes([0xFE, 0xFE, 0xE0, icom_addr & 0xFF, 0x03])
        i = data.find(key)
        if i < 0 or len(data) < i + 10:
            return None
        return int(''.join('%02X' % b for b in reversed(data[i + 5:i + 10])))
    if proto in ('YAESU', 'KENWOOD', 'ELECRAFT'):
        m = re.search(rb'FA(\d{8,11});', data)
        return int(m.group(1)) if m else None
    if len(data) >= 5:
        return int(''.join('%02X' % b for b in data[:4])) * 10
    return None


# ------------------------------------------------------------------ scenarios
def compile_pattern(p):
    toks = str(p).upper().split()
    return '^' + r'\s+'.join(r'(\S+)' if t == '*' else re.escape(t) for t in toks) + '$'


def substitute(action, groups):
    def f(m):
        n = int(m.group(1))
        if not (1 <= n <= len(groups)):
            raise RigError('в сценарии нет параметра {%d}' % n)
        return groups[n - 1]
    return re.sub(r'\{(\d+)\}', f, action)


def match_rows(rows, text, sender=None):
    """first matching enabled scenario -> (row index, action text with parameters) or None.
    sender=None skips the sender check (manual test)."""
    t = ' '.join(str(text).upper().split())
    for i, r in enumerate(rows):
        if not r.get('on', True) or not str(r.get('trigger', '')).strip():
            continue
        frm = str(r.get('from', '')).strip()
        if frm and sender is not None:
            allowed = {int(x) for x in re.split(r'[,\s]+', frm) if x.isdigit()}
            if sender not in allowed:
                continue
        m = re.match(compile_pattern(r['trigger']), t)
        if m:
            return i, substitute(str(r.get('action', '')), m.groups())
    return None


DEFAULT_SCENARIOS = [
    dict(on=True, trigger='CNG BND *', **{'from': ''}, action='BND {1}'),
    dict(on=True, trigger='SET FRQ * *', **{'from': ''}, action='FRQ {1} {2}'),
    dict(on=True, trigger='SET FRQ *', **{'from': ''}, action='FRQ {1}'),
    dict(on=True, trigger='SET MOD *', **{'from': ''}, action='MODE {1}'),
    dict(on=True, trigger='FRQ UP *', **{'from': ''}, action='UP {1}'),
    dict(on=True, trigger='FRQ DN *', **{'from': ''}, action='DOWN {1}'),
    dict(on=True, trigger='DIGI *', **{'from': ''}, action='BND {1}; MODE USB-D'),
    dict(on=True, trigger='LSB *', **{'from': ''}, action='FRQ {1} LSB'),
    dict(on=True, trigger='USB *', **{'from': ''}, action='FRQ {1} USB'),
    dict(on=True, trigger='CW *', **{'from': ''}, action='FRQ {1} CW'),
    dict(on=True, trigger='WWV', **{'from': ''}, action='FRQ 10.000 AM'),
    dict(on=True, trigger='HOME', **{'from': ''}, action='BND 40; MODE LSB'),
]


# ------------------------------------------------------------------ controller
class RigController:
    """cfg: dict(proto, port, baud, icom_addr).  Shares the serial object of the PTT controller when
    both use the same COM port (typical for CAT + RTS PTT on one USB interface)."""

    def __init__(self, ptt, serial_module=None, ptt_cfg=None, ptt_open=None):
        self.ptt = ptt
        self._mod = serial_module if serial_module is not None else P.serial
        self._ptt_cfg = ptt_cfg
        self._ptt_open = ptt_open
        self.ser = None
        self._own = None
        self._lock = threading.RLock()
        self.exec_lock = threading.Lock()

    def close(self):
        with self._lock:
            if self.ser is not None:
                try:
                    self.ser.close()
                except Exception:
                    pass
            self.ser = None
            self._own = None

    def _with_port(self, cfg, fn):
        port, baud = cfg.get('port'), int(cfg.get('baud') or 9600)
        if not port:
            raise RigError('не выбран COM-порт трансивера')
        pc = self._ptt_cfg() if self._ptt_cfg else {}
        if pc.get('method', 'none') != 'none' and pc.get('port') == port:
            if self._ptt_open:
                err = self._ptt_open()
                if err:
                    raise RigError(err)
            with self.ptt._lock:
                ser = self.ptt.ser
                if ser is None:
                    raise RigError('порт PTT не открыт')
                if getattr(ser, 'baudrate', baud) != baud:
                    ser.baudrate = baud
                return fn(ser)
        if self._mod is None:
            raise RigError('pyserial не установлен (pip install pyserial)')
        with self._lock:
            if self.ser is None or self._own != (port, baud):
                self.close()
                try:
                    s = self._mod.Serial()
                    s.port = port
                    s.baudrate = baud
                    s.timeout = 0.2
                    s.write_timeout = 1.0
                    s.rts = False
                    s.dtr = False
                    s.open()
                except Exception as ex:
                    raise RigError('не удалось открыть порт %s: %s' % (port, ex))
                self.ser, self._own = s, (port, baud)
            return fn(self.ser)

    @staticmethod
    def _drain(ser, wait=0.2):
        try:
            data = ser.read(64)
        except Exception:
            data = b''
        return data

    def _send(self, cfg, frames):
        proto, addr = cfg['proto'], int(cfg.get('icom_addr') or 0x94)

        def fn(ser):
            for fr in frames:
                try:
                    ser.reset_input_buffer()
                except Exception:
                    pass
                ser.write(fr)
                try:
                    ser.flush()
                except Exception:
                    pass
                if proto == 'YAESU-OLD':
                    time.sleep(0.1)
                    self._drain(ser)
                    continue
                rep = self._drain(ser)
                if proto == 'ICOM' and bytes([0xFE, 0xFE, 0xE0, addr & 0xFF, 0xFA, 0xFD]) in rep:
                    raise RigError('радио ответило NG (команда не поддерживается или неверный CI-V адрес)')
                if proto != 'ICOM' and b'?;' in rep:
                    raise RigError('радио ответило «?;» (команда отклонена)')
                time.sleep(0.05)
        self._with_port(cfg, fn)

    def read_freq(self, cfg):
        proto, addr = cfg['proto'], int(cfg.get('icom_addr') or 0x94)

        def fn(ser):
            try:
                ser.reset_input_buffer()
            except Exception:
                pass
            ser.write(frame_read_freq(proto, addr))
            try:
                ser.flush()
            except Exception:
                pass
            data = b''
            for _ in range(4):
                data += self._drain(ser)
                hz = parse_freq_reply(proto, data, addr)
                if hz:
                    return hz
            return None
        hz = self._with_port(cfg, fn)
        if not hz:
            raise RigError('радио не ответило на запрос частоты')
        return hz

    def set_freq(self, cfg, hz):
        self._send(cfg, frames_set_freq(cfg['proto'], hz, int(cfg.get('icom_addr') or 0x94)))

    def set_mode(self, cfg, mode):
        self._send(cfg, frames_set_mode(cfg['proto'], mode, int(cfg.get('icom_addr') or 0x94)))

    # ---- one scenario action
    def run_action(self, cfg, action):
        tok = action.split()
        if not tok:
            return ''
        cmd, args = tok[0].upper(), tok[1:]
        if cmd == 'BND':
            if len(args) != 1 or not args[0].isdigit() or int(args[0]) not in BANDS:
                raise RigError('BND: диапазон из %s' % ' '.join(str(b) for b in BANDS))
            mhz, mode = BANDS[int(args[0])]
            self.set_freq(cfg, parse_freq(mhz))
            self.set_mode(cfg, mode)
            return 'диапазон %s м: %.3f МГц %s' % (args[0], mhz, mode)
        if cmd == 'FRQ':
            if not args or len(args) > 2:
                raise RigError('FRQ <МГц> [режим]')
            hz = parse_freq(args[0])
            if len(args) == 2:
                norm_mode(args[1])
            self.set_freq(cfg, hz)
            if len(args) == 2:
                self.set_mode(cfg, args[1])
            return '%s МГц%s' % (fmt_mhz(hz), (' ' + norm_mode(args[1])) if len(args) == 2 else '')
        if cmd == 'MODE':
            if len(args) != 1:
                raise RigError('MODE <режим>')
            self.set_mode(cfg, args[0])
            return 'режим %s' % norm_mode(args[0])
        if cmd in ('UP', 'DOWN'):
            if len(args) != 1:
                raise RigError('%s <кГц>' % cmd)
            try:
                d = float(args[0].replace(',', '.'))
            except ValueError:
                raise RigError('%s: неверное число' % cmd)
            if not (0 < d <= 1000):
                raise RigError('%s: шаг 0…1000 кГц' % cmd)
            hz = self.read_freq(cfg) + int(round(d * 1000)) * (1 if cmd == 'UP' else -1)
            if not (F_MIN_HZ <= hz <= F_MAX_HZ):
                raise RigError('частота вне пределов')
            self.set_freq(cfg, hz)
            return '%s МГц' % fmt_mhz(hz)
        if cmd == 'WAIT':
            try:
                w = max(0.0, min(10.0, float(args[0].replace(',', '.'))))
            except Exception:
                raise RigError('WAIT <секунды>')
            time.sleep(w)
            return 'пауза %.1f с' % w
        raise RigError('неизвестная команда «%s»' % tok[0])

    def run_actions(self, cfg, text):
        """-> list of (action, ok, message); stops at the first error"""
        out = []
        with self.exec_lock:
            for a in [x.strip() for x in text.split(';') if x.strip()]:
                try:
                    out.append((a, True, self.run_action(cfg, a)))
                except RigError as ex:
                    out.append((a, False, str(ex)))
                    break
                except Exception as ex:
                    out.append((a, False, 'ошибка: %s' % ex))
                    break
        return out
