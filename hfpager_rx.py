"""
HFPager receiver engine: segmentation of a continuous audio stream, preamble-based tone-grid
acquisition and AFC (automatic frequency control), decision-directed tone tracking, RS/CRC decode.

Depends only on numpy and hfpager_modem.py.  Thread-safe: audio callback calls feed(); decoding
runs in worker threads.
"""
import threading, time, queue
from collections import deque
import numpy as np
import hfpager_modem as M

MODEM_LOCK = threading.RLock()      # hfpager_modem keeps the current speed in module globals

SPEED_ORDER = ('5.86', '23.44')


# ----------------------------------------------------------------------------- DSP helpers
def sym_peaks(x, fs, starts, win, fmin, fmax):
    """Per-symbol peak frequency (parabolic interpolation) and 'sharpness' (peak power share)."""
    N = 1 << int(np.ceil(np.log2(win * 4)))
    df = fs / N
    lo = max(int(np.floor(fmin / df)), 1)
    hi = int(np.ceil(fmax / df))
    w = np.hanning(win)
    n = len(starts)
    F = np.zeros(n)
    S = np.zeros(n)
    ar = np.arange(win)[None, :]
    for c in range(0, n, 128):
        st = np.asarray(starts[c:c + 128]).astype(np.int64)
        idx = st[:, None] + ar
        valid = (idx >= 0) & (idx < len(x))
        seg = np.where(valid, x[np.clip(idx, 0, len(x) - 1)], 0.0) * w
        sp = np.abs(np.fft.rfft(seg, N, axis=1))[:, lo:hi + 1]
        k = np.argmax(sp, axis=1)
        rows = np.arange(len(st))
        y1 = sp[rows, k]
        y0 = sp[rows, np.maximum(k - 1, 0)]
        y2 = sp[rows, np.minimum(k + 1, sp.shape[1] - 1)]
        den = y0 - 2 * y1 + y2
        safe = np.where(np.abs(den) > 1e-12, den, 1.0)
        d = np.where(np.abs(den) > 1e-12, 0.5 * (y0 - y2) / safe, 0.0)
        F[c:c + 128] = (lo + k + np.clip(d, -1, 1)) * df
        S[c:c + 128] = y1 ** 2 / (np.sum(sp ** 2, axis=1) + 1e-18)
    return F, S


def estimate_band(x, fs, step, lo=300.0, hi=3000.0):
    """Find where the 18-tone group sits in the audio spectrum (rough centre +- span)."""
    nfft = 4096
    n = (len(x) // nfft)
    if n < 2:
        return lo, hi
    blocks = x[:n * nfft].reshape(n, nfft) * np.hanning(nfft)
    p = np.mean(np.abs(np.fft.rfft(blocks, axis=1)) ** 2, axis=0)
    fr = np.fft.rfftfreq(nfft, 1 / fs)
    sel = (fr >= lo) & (fr <= hi)
    fr, p = fr[sel], p[sel]
    span = 17 * step
    kern = max(int(span / (fr[1] - fr[0])), 3)
    sm = np.convolve(p, np.ones(kern) / kern, 'same')
    c = fr[np.argmax(sm)]
    m = (fr > c - span) & (fr < c + span)
    cen = np.sum(fr[m] * p[m]) / (np.sum(p[m]) + 1e-18)
    half = span / 2 + 3 * step + 30
    return max(cen - half, 150.0), cen + half


def _wrap(v, period=18.0):
    return (v + period / 2) % period - period / 2


def afc_tones(F, a, pre, step, mu, nsym):
    """Decision-directed absolute tone tracking with a first-order AFC loop.
    F: symbol frequencies, a: index of the first preamble symbol, pre: preamble length.
    Returns (tone indices, offset track o_k, fref)."""
    # tone-0 reference from the preamble: tones are (6*(i-a)) mod 18
    known = range(a + 1, min(a + pre - 1, nsym))
    res = np.array([F[i] - step * ((6 * (i - a)) % 18) for i in known])
    fref = float(np.median(res)) if len(res) else float(F[a])
    o = 0.0
    t = np.zeros(nsym, dtype=np.int64)
    off = np.zeros(nsym)
    for i in range(a, nsym):
        if a < i <= a + pre - 2:
            t[i] = (6 * (i - a)) % 18
        elif i == a:
            t[i] = 0
        else:
            t[i] = int(np.round((F[i] - fref - o) / step))
        e = F[i] - (fref + step * t[i] + o)
        e = max(-step / 2, min(step / 2, e))
        o += mu * e
        off[i] = o
    return t, off, fref


def nibbles_from_tones(t, r):
    out = []
    for j in range(r, len(t) - 1):
        d = int(t[j + 1] - t[j]) % 18 - 2
        out.append(M.gray(d) if 0 <= d <= 15 else 99)
    return out


def nibbles_diff(F, r, step):
    st = np.round(np.diff(F[r:]) / step).astype(int)
    d = (st - 2) % 18
    return [M.gray(int(v)) if v < 16 else 99 for v in d]


# ----------------------------------------------------------------------------- segment decoder
def _find_preamble(F, step, pre):
    d = np.diff(F) / step
    w = np.abs(_wrap(d - 6.0)) < 0.7
    best = None
    i = 0
    n = len(w)
    while i < n:
        if w[i]:
            j = i
            while j + 1 < n and w[j + 1]:
                j += 1
            if j - i + 1 >= max(8, pre // 3):
                best = (i, j)
                break
            i = j + 1
        else:
            i += 1
    return best


def decode_segment(x, fs, t_on, speed, afc=True, band=None, max_sym=None):
    """Try to decode one frame inside samples x (signal onset ~ t_on seconds).
    Returns (frame_dict or None, info_dict)."""
    with MODEM_LOCK:
        M.set_speed(speed)
        baud, step, pre = M.BAUD, M.STEP, M.PRE_COUNT
        T0 = fs / baud
        if band is None:
            band = estimate_band(x, fs, step)
        fmin, fmax = band
        win = int(0.8 * T0)
        # --- coarse symbol phase: maximise spectral sharpness over preamble symbols
        s_a = t_on + 0.5
        scan = []
        for j in range(16):
            ph = s_a + j * (T0 / fs) / 16
            starts = (ph * fs + np.arange(8) * T0 + 0.1 * T0)
            _, S = sym_peaks(x, fs, starts, win, fmin, fmax)
            scan.append(float(np.mean(S)))
        best_j = int(np.argmax(scan))
        phase0 = s_a + best_j * (T0 / fs) / 16
        total_sym = int((len(x) / fs - (t_on - 0.6)) * baud) + 2
        if max_sym:
            total_sym = min(total_sym, max_sym)
        info = {'speed': speed, 'band': band}
        # attempts: timing offsets and sample-clock scale
        trials = [(0.0, 1.0)]
        for dphi in (-1 / 16, 1 / 16, -1 / 8, 1 / 8):
            trials.append((dphi, 1.0))
        for sc in (1 - 2e-4, 1 + 2e-4, 1 - 4e-4, 1 + 4e-4):
            trials.append((0.0, sc))
        for dphi, sc in trials:
            T = T0 * sc
            g0 = phase0 + dphi * T / fs
            m = int(np.floor((g0 - (t_on - 0.6)) / (T / fs)))
            g0 -= m * T / fs
            starts = g0 * fs + np.arange(total_sym) * T + 0.1 * T
            F, S = sym_peaks(x, fs, starts, int(0.8 * T), fmin, fmax)
            run = _find_preamble(F, step, pre)
            if run is None:
                continue
            a = run[0]
            cands = sorted({a + pre + j for j in range(-2, 3)} | {run[1] + 1 + j for j in range(-3, 1)})
            modes = []
            if afc:
                modes += [('afc', 0.25), ('afc', 0.1), ('afc', 0.5), ('afc', 0.0)]
            modes.append(('diff', 0))
            for mode, mu in modes:
                if mode == 'afc':
                    t, off, fref = afc_tones(F, a, pre, step, mu, len(F))
                for r in cands:
                    if r < 0 or r + 62 > len(F):
                        continue
                    nib = nibbles_from_tones(t, r) if mode == 'afc' else nibbles_diff(F, r, step)
                    fr = M.parse_frame(nib)
                    if fr:
                        used = slice(r, min(len(F), r + 60 * (1 + (fr['length'] + 11) // 12) + 1))
                        fr['speed'] = speed
                        info.update(mode=mode, mu=mu, ref_index=r, run=run,
                                    center_hz=float(np.median(F[used])),
                                    afc_hz=(float(np.ptp(off[a:used.stop])) if mode == 'afc' else 0.0),
                                    sharp=float(np.median(S[used])),
                                    t_start=float(g0 + a * T / fs))
                        return fr, info
        return None, info


# ----------------------------------------------------------------------------- streaming engine
class RxEngine:
    BLOCK_S = 0.1

    def __init__(self, fs, on_frame, on_event=None, speeds=SPEED_ORDER, afc=True,
                 sens_db=6.0, end_s=1.5, min_active_s=4.0):
        self.fs = fs
        self.on_frame = on_frame
        self.on_event = on_event or (lambda kind, info: None)
        self.speeds = tuple(speeds)
        self.afc = afc
        self.sens = 10 ** (sens_db / 10.0)
        self.pk_margin_default = 4.0
        self.end_blocks = int(round(end_s / self.BLOCK_S))
        self.min_active = int(min_active_s / self.BLOCK_S)
        self.blen = int(round(fs * self.BLOCK_S))
        self._pend = deque()
        self._plock = threading.Lock()
        self._carry = np.zeros(0, dtype=np.float32)
        self._hist = deque(maxlen=300)
        self._hist_pk = deque(maxlen=300)
        self._spec_acc = None
        self._spec_n = 0
        self.pk_floor = 0.0
        self.pk_margin = 4.0
        self._recent = deque(maxlen=8)
        self._seg = None
        self._act_run = 0
        self._quiet = 0
        self._paused = False
        self._resume_at = 0
        self._nblocks = 0
        self._dq = queue.Queue()
        self._stop = False
        self._threads = []
        self.max_blocks = int(260 / self.BLOCK_S)
        self.floor = None

    # ---- control
    def start(self):
        self._stop = False
        for fn in (self._pump_loop, self._decode_loop):
            th = threading.Thread(target=fn, daemon=True)
            th.start()
            self._threads.append(th)

    def stop(self):
        self._stop = True
        self._dq.put(None)

    def pause(self, on):
        """Mute reception (during our own transmission)."""
        with self._plock:
            self._paused = on
            self._pend.clear()
            self._carry = np.zeros(0, dtype=np.float32)
            self._seg = None
            self._act_run = 0
            self._quiet = 0
            if not on:
                self._resume_at = self._nblocks + 5     # ignore the first 0.5 s after TX

    def feed(self, samples):
        with self._plock:
            if not self._paused:
                self._pend.append(np.asarray(samples, dtype=np.float32).copy())

    # ---- internals
    def _pump_loop(self):
        while not self._stop:
            self.process_available()
            time.sleep(0.05)

    def _decode_loop(self):
        while not self._stop:
            item = self._dq.get()
            if item is None:
                break
            self._decode_item(item)

    def process_available(self):
        with self._plock:
            while self._pend:
                self._carry = np.concatenate([self._carry, self._pend.popleft()])
            carry = self._carry
            nb = len(carry) // self.blen
            blocks = [carry[i * self.blen:(i + 1) * self.blen] for i in range(nb)]
            self._carry = carry[nb * self.blen:]
        for b in blocks:
            self._block(b)

    def _block(self, b):
        self._nblocks += 1
        sp = np.abs(np.fft.rfft(b * np.hanning(len(b)))) ** 2
        fr = np.fft.rfftfreq(len(b), 1 / self.fs)
        sel = (fr >= 300) & (fr <= 3000)
        band = sp[sel]
        e = float(np.sum(band)) + 1e-12
        # narrow-band "peakiness": strongest 3-bin cluster vs median noise bin
        sm = np.convolve(band, np.ones(3) / 3, 'same')
        pk = 10 * np.log10((np.max(sm) + 1e-18) / (np.median(band) + 1e-18))
        # monitor data for the GUI: input level and a 300..3000 Hz spectrum slice (10 Hz bins)
        rms_db = 20 * np.log10(float(np.sqrt(np.mean(np.square(b, dtype=np.float64)))) + 1e-9)
        clip = bool(np.max(np.abs(b)) > 0.98)
        slice_ = sp[30:300] if len(sp) > 300 else np.zeros(270)
        self._spec_acc = slice_ if self._spec_n == 0 else self._spec_acc + slice_
        self._spec_n += 1
        if self._spec_n >= 2:
            self.on_event('spectrum', {'db': (10 * np.log10(self._spec_acc / 2 + 1e-12)).astype(np.float32)})
            self._spec_n = 0
        if self._nblocks < self._resume_at:
            return
        idle = self._seg is None
        if not idle:
            self._seg['peak_e'] = max(self._seg.get('peak_e', 0.0), e)
        # learn the noise floor while idle, and also from very weak blocks inside a segment
        # (so a rising background or a wrong initial estimate cannot lock the detector "on")
        if idle or e < 0.01 * self._seg.get('peak_e', 1e30):
            self._hist.append(e)
            self._hist_pk.append(pk)
            if len(self._hist) >= 15:
                self.floor = float(np.percentile(self._hist, 25))
                self.pk_floor = max(float(np.percentile(self._hist_pk, 90)), 8.0)
        ready = len(self._hist) >= 15
        floor = self.floor if self.floor else e
        # tone presence = narrow-band peakiness; independent of broadband level changes (AGC, QRN)
        loud = ready and (pk > max(self.pk_floor, 8.0) + self.pk_margin)
        if self._nblocks % 2 == 0:
            self.on_event('level', {'db': 10 * np.log10(e / floor) if floor else 0.0,
                                    'peak_db': pk, 'rms_db': rms_db, 'clip': clip,
                                    'receiving': self._seg is not None})
        if self._seg is None:
            self._recent.append(b)
            self._act_run = self._act_run + 1 if loud else 0
            if self._act_run >= 3:
                rec = list(self._recent)
                self._seg = {'blocks': rec[:], 'n_on': len(rec) - 3, 'active': 3}
                self._quiet = 0
                self.on_event('state', {'state': 'receiving'})
        else:
            self._seg['blocks'].append(b)
            if loud:
                self._seg['active'] += 1
                self._quiet = 0
            else:
                self._quiet += 1
            if self._quiet >= self.end_blocks or len(self._seg['blocks']) >= self.max_blocks:
                seg = self._seg
                self._seg = None
                self._act_run = 0
                self._recent.clear()
                if seg['active'] >= self.min_active:
                    x = np.concatenate(seg['blocks']).astype(np.float64)
                    self._dq.put((x, seg['n_on'] * self.BLOCK_S))
                    self.on_event('state', {'state': 'decoding'})
                else:
                    self.on_event('state', {'state': 'idle'})

    def _decode_item(self, item):
        x, t_on = item
        found = False
        try:
            for sp in self.speeds:
                # restart search from the signal onset repeatedly: several frames may follow each other
                fr, info = decode_segment(x, self.fs, t_on, sp, afc=self.afc)
                if fr:
                    found = True
                    self.on_frame(fr, info)
                    break
        except Exception as ex:                      # never kill the worker
            self.on_event('error', {'text': repr(ex)})
        if not found:
            self.on_event('fail', {'seconds': len(x) / self.fs})
        self.on_event('state', {'state': 'idle'})

    def flush(self, timeout=60):
        """For tests: wait until the decode queue is empty."""
        t = time.time()
        while time.time() - t < timeout:
            if self._dq.empty():
                time.sleep(0.2)
                if self._dq.empty():
                    return
            time.sleep(0.05)

    def decode_pending_sync(self):
        """For tests without threads: decode everything queued, synchronously."""
        while not self._dq.empty():
            it = self._dq.get()
            if it is not None:
                self._decode_item(it)
