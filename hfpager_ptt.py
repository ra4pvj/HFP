"""PTT / transmitter keying for HFPager app.

Methods
  none         - no keying (VOX / external)
  RTS, DTR, RTS+DTR - level on a serial-port control line ("1" on the COM port)
  ICOM         - CI-V  FE FE <radio> E0 1C 00 <01|00> FD
  YAESU        - new CAT      "TX1;" / "TX0;"      (FT-991/A, FTDX-101/10/3000/5000, FT-710, FT-891, FT-450 ...)
  YAESU-OLD    - 5-byte CAT   00 00 00 00 08 / 88  (FT-817/818, FT-857, FT-897, FT-100, FT-450 old fw ...)
  KENWOOD      - "TX;" / "RX;"                     (TS-590, TS-480, TS-2000, TS-890, TS-870, TS-570, TS-50 ...)
                 variants: TX; | TX0; | TX1; (TX1 = data/ACC2 audio input)
  ELECRAFT     - same as KENWOOD "TX;" / "RX;"      (K3, KX3, KX2, K4)

The command sets are written from the manufacturers' public CAT manuals and have NOT been tested
against real radios by the author; use the "Test PTT" button with a dummy load first.
"""
import threading
import time

try:
    import serial
    import serial.tools.list_ports as _lp
    SERIAL_ERROR = None
except Exception as _ex:            # pyserial missing
    serial = None
    _lp = None
    SERIAL_ERROR = str(_ex)

METHODS = ['none', 'RTS', 'DTR', 'RTS+DTR', 'ICOM', 'YAESU', 'YAESU-OLD', 'KENWOOD', 'ELECRAFT']
LINE_METHODS = ('RTS', 'DTR', 'RTS+DTR')
DEFAULT_BAUD = {'ICOM': 19200, 'YAESU': 38400, 'YAESU-OLD': 9600, 'KENWOOD': 9600, 'ELECRAFT': 38400,
                'RTS': 9600, 'DTR': 9600, 'RTS+DTR': 9600, 'none': 9600}
BAUDS = [1200, 4800, 9600, 19200, 38400, 57600, 115200]

# default CI-V addresses (hex); the address can be changed in the radio's menu, so a custom field exists
ICOM_MODELS = {
    'IC-7300': 0x94, 'IC-7300MK2': 0xB6, 'IC-705': 0xA4, 'IC-7610': 0x98, 'IC-9700': 0xA2, 'IC-7100': 0x88,
    'IC-7851/7800': 0x8E, 'IC-7700': 0x74, 'IC-7600': 0x7A, 'IC-7410': 0x80, 'IC-7200': 0x76,
    'IC-7000': 0x70, 'IC-756PRO3': 0x6E, 'IC-756PRO2': 0x64, 'IC-756PRO': 0x5C, 'IC-746PRO': 0x66,
    'IC-746': 0x56, 'IC-706MKIIG': 0x58, 'IC-718': 0x5E, 'IC-703': 0x68, 'IC-707': 0x3E,
    'IC-775': 0x46, 'IC-781': 0x26, 'IC-R8600 (no TX)': 0x96, 'Custom': None,
}
KENWOOD_VARIANTS = ['TX;', 'TX0;', 'TX1;']


def list_ports():
    """[(device, description)]"""
    if _lp is None:
        return []
    try:
        return [(p.device, '%s - %s' % (p.device, p.description)) for p in sorted(_lp.comports())]
    except Exception:
        return []


def parse_hex_addr(text):
    t = str(text).strip().lower().replace('0x', '').replace('h', '')
    v = int(t, 16)
    if not (0 < v < 0xFE):
        raise ValueError('CI-V address must be 01..FD')
    return v


def civ_ptt_frame(addr, on):
    return bytes([0xFE, 0xFE, addr & 0xFF, 0xE0, 0x1C, 0x00, 0x01 if on else 0x00, 0xFD])


def command_bytes(method, on, icom_addr=0x94, kenwood_variant='TX;'):
    """bytes sent for a CAT method, or None for line methods"""
    if method == 'ICOM':
        return civ_ptt_frame(icom_addr, on)
    if method == 'YAESU':
        return b'TX1;' if on else b'TX0;'
    if method == 'YAESU-OLD':
        return bytes([0, 0, 0, 0, 0x08 if on else 0x88])
    if method in ('KENWOOD', 'ELECRAFT'):
        if not on:
            return b'RX;'
        v = kenwood_variant if kenwood_variant in KENWOOD_VARIANTS else 'TX;'
        return v.encode('ascii')
    return None


class PttController:
    """Thread-safe PTT. cfg keys: method, port, baud, invert, icom_addr (int), kenwood_variant."""

    def __init__(self, serial_module=None):
        self._ser_mod = serial_module if serial_module is not None else serial
        self.ser = None
        self.cfg = {}
        self.keyed = False
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ helpers
    @property
    def method(self):
        return self.cfg.get('method', 'none')

    def active(self):
        return self.method != 'none'

    def _idle_levels(self):
        inv = bool(self.cfg.get('invert'))
        idle = inv          # not keyed: line level = False, or True when inverted
        m = self.method
        return (idle if m in ('RTS', 'RTS+DTR') else False,
                idle if m in ('DTR', 'RTS+DTR') else False)

    def configure(self, cfg):
        """(re)open the port if settings changed; returns error text or None"""
        with self._lock:
            if self.keyed:
                self._send(False)
            new = dict(cfg)
            if self.ser is not None and self.cfg == new:
                return None
            self.close()
            self.cfg = new
            if new.get('method', 'none') == 'none':
                return None
            return self._open()

    def _open(self):
        if self._ser_mod is None:
            return 'pyserial не установлен (pip install pyserial): %s' % SERIAL_ERROR
        port = self.cfg.get('port')
        if not port:
            return 'Не выбран COM-порт для PTT/CAT.'
        try:
            ser = self._ser_mod.Serial()
            ser.port = port
            ser.baudrate = int(self.cfg.get('baud') or 9600)
            ser.timeout = 0.2
            ser.write_timeout = 1.0
            rts, dtr = self._idle_levels()
            ser.rts = rts           # set before open() so the lines never glitch into TX
            ser.dtr = dtr
            ser.open()
            self.ser = ser
            return None
        except Exception as ex:
            self.ser = None
            return 'Не удалось открыть порт %s: %s' % (port, ex)

    def close(self):
        with self._lock:
            if self.ser is not None:
                try:
                    if self.keyed:
                        self._send(False)
                except Exception:
                    pass
                try:
                    self.ser.close()
                except Exception:
                    pass
            self.ser = None
            self.keyed = False

    # ------------------------------------------------------------------ keying
    def _send(self, on):
        m = self.method
        ser = self.ser
        if m == 'none':
            return None
        if ser is None:
            return 'Порт PTT не открыт.'
        if m in LINE_METHODS:
            level = bool(on) != bool(self.cfg.get('invert'))
            if m in ('RTS', 'RTS+DTR'):
                ser.rts = level
            if m in ('DTR', 'RTS+DTR'):
                ser.dtr = level
        else:
            data = command_bytes(m, on, int(self.cfg.get('icom_addr') or 0x94),
                                 self.cfg.get('kenwood_variant', 'TX;'))
            try:
                ser.reset_input_buffer()
            except Exception:
                pass
            ser.write(data)
            try:
                ser.flush()
            except Exception:
                pass
            if m == 'ICOM':
                time.sleep(0.05)    # radio echoes the frame and answers FB; just drain it
                try:
                    ser.reset_input_buffer()
                except Exception:
                    pass
        self.keyed = bool(on)
        return None

    def key(self, on):
        """returns error text or None"""
        with self._lock:
            try:
                if on and self.method != 'none' and self.ser is None:
                    err = self._open()
                    if err:
                        return err
                err = self._send(on)
                if err:
                    return err
                if on and self.method in ('YAESU', 'YAESU-OLD', 'KENWOOD', 'ELECRAFT', 'ICOM'):
                    time.sleep(0.05)
                return None
            except Exception as ex:
                if not on:
                    self.keyed = False
                return 'Ошибка PTT (%s): %s' % (self.method, ex)

    def unkey_safe(self):
        """best effort, retries once, never raises"""
        for _ in range(2):
            err = self.key(False)
            if not err:
                return None
            self.close()
        return err
