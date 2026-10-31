#!/usr/bin/env python3
"""
HFPager (PS-18) reference modem, 5.86 Bd mode.
Reverse-engineered from recordings and from the native modem library (CRC tables and
check-nibble logic).  Requires numpy.  See HFPAGER_PROTOCOL.md for the format.

  python3 hfpager_modem.py tx  OUT.wav --dest 3 --sender 10048 --counter 208 "TEXT"
  python3 hfpager_modem.py rx  IN.wav
"""
import sys, wave, argparse
import numpy as np

# speed table: baud (48 kHz / samples-per-symbol), tone spacing in Hz, preamble length in symbols
SPEEDS = {
    '5.86':  dict(baud=48000/8192, step=2*48000/8192, pre=24),   # verified
    '23.44': dict(baud=48000/2048, step=48000/2048,   pre=76),   # verified
}
BAUD=STEP=PRE_COUNT=None; CUR_SPEED=None
def set_speed(name):
    global BAUD,STEP,PRE_COUNT,CUR_SPEED
    p=SPEEDS[name]; BAUD,STEP,PRE_COUNT=p['baud'],p['step'],p['pre']; CUR_SPEED=name
set_speed('5.86')
F0     = 1611.4                # frequency of tone 0, Hz (audio carrier is user-selectable)
NTONE  = 18

# ------------------------------------------------------------------ CRCs
def _mk16():
    t=[]
    for i in range(256):
        c=i
        for _ in range(8): c=(c>>1)^0x8408 if c&1 else c>>1
        t.append(c)
    return t
T16=_mk16()
def crc16(data,init=0xFFFF):
    c=init
    for b in data: c=(c>>8)^T16[(c^b)&0xff]
    return c
def _mk4():
    t=[]
    for i in range(256):
        c=i
        for _ in range(8): c=(c>>1)^0xC if c&1 else c>>1
        t.append(c&0xf)
    return t
T4=_mk4()
def crc4(nibs,init=0xF):
    c=init
    for n in nibs: c=T4[(n^c)&0xff]
    return c
MASK_HDR=[0,2,4,6]; MASK_MORE=[0,3,5,7]; MASK_LAST=[0,8,9,10]

# ------------------------------------------------------------------ GF(16) / RS(15,7)
EXP=[0]*30; LOG=[0]*16
_v=1
for _i in range(15):
    EXP[_i]=_v; LOG[_v]=_i; _v<<=1
    if _v&16: _v^=0x13
for _i in range(15,30): EXP[_i]=EXP[_i-15]
def gmul(a,b): return 0 if not a or not b else EXP[LOG[a]+LOG[b]]
def ginv(a): return EXP[(15-LOG[a])%15]
def _gen():
    g=[1]
    for k in range(1,9):
        r=EXP[k]; ng=[0]*(len(g)+1)
        for i,c in enumerate(g):
            ng[i+1]^=c; ng[i]^=gmul(c,r)
        g=ng
    return g
GEN=_gen()
def rs_encode(data7):
    """c[0..7]=parity, c[8..14]=data (c(x)=sum c_i x^i, roots alpha^1..alpha^8)"""
    rem=[0]*8+list(data7)
    for i in range(14,7,-1):
        c=rem[i]
        if c:
            for j in range(9): rem[i-8+j]^=gmul(c,GEN[j])
    return rem[:8]+list(data7)
def _syndromes(cw):
    out=[]
    for k in range(1,9):
        s=0
        for i,c in enumerate(cw):
            if c: s^=EXP[(LOG[c]+k*i)%15]
        out.append(s)
    return out
def rs_decode(cw):
    """Berlekamp-Massey + Chien + Forney. Returns corrected 15 nibbles or None."""
    cw=[c if 0<=c<=15 else 0 for c in cw]   # invalid symbols are treated as errors
    S=_syndromes(cw)
    if not any(S): return cw
    # Berlekamp-Massey
    C=[1]+[0]*8; Bp=[1]+[0]*8; L=0; m=1; b=1
    for n in range(8):
        d=S[n]
        for i in range(1,L+1): d^=gmul(C[i],S[n-i])
        if d==0: m+=1; continue
        T=C[:]
        coef=gmul(d,ginv(b))
        for i in range(9-m): C[i+m]^=gmul(coef,Bp[i])
        if 2*L<=n:
            L=n+1-L; Bp=T; b=d; m=1
        else: m+=1
    if L>4: return None
    # Chien search: error at position i if Lambda(alpha^-i)==0
    pos=[]
    for i in range(15):
        x=EXP[(15-i)%15]; s=0
        for j in range(L+1):
            if C[j]: s^=gmul(C[j],EXP[(LOG[x]*j)%15] if x else (1 if j==0 else 0))
        if s==0: pos.append(i)
    if len(pos)!=L: return None
    # Omega = S(x)*Lambda(x) mod x^8   (S(x)=sum S_{k} x^{k-1})
    Om=[0]*8
    for i in range(8):
        for j in range(L+1):
            if i+j<8 and C[j] and S[i]: Om[i+j]^=gmul(C[j],S[i])
    for i in pos:
        xinv=EXP[(15-i)%15]
        num=0
        for j in range(8):
            if Om[j]: num^=gmul(Om[j],EXP[(LOG[xinv]*j)%15] if xinv else 0)
        den=0
        for j in range(1,L+1,2):   # formal derivative: odd terms
            if C[j]: den^=gmul(C[j],EXP[(LOG[xinv]*(j-1))%15])
        if den==0: return None
        cw[i]^=gmul(num,ginv(den))
    return cw if not any(_syndromes(cw)) else None

# ------------------------------------------------------------------ frame layer
def _pack_sb(by12,mask):
    blocks=[]
    for b in range(4):
        q=[]
        for x in (by12[b],by12[4+b],by12[8+b]): q+=[x&0xf,x>>4]
        q.append(crc4(q)^mask[b])
        blocks.append(rs_encode(q))
    out=[0]*60
    for b in range(4):
        for i in range(15): out[4*i+b]=blocks[b][i]
    return out
def frame_nibbles(dest,sender,counter,text,b0=0x05,b1=0xA0):
    t=bytes(text)
    if not 1<=len(t)<=252: raise ValueError('text length 1..252')
    hdr=[b0,b1,dest&0xff,(dest>>8)&0xff,sender&0xff,(sender>>8)&0xff,counter&0xff,len(t)]
    c=crc16(t); hdr+=[c&0xff,c>>8]
    c=crc16(hdr); hdr+=[c&0xff,c>>8]
    out=_pack_sb(hdr,MASK_HDR)
    n=(len(t)+11)//12
    for k in range(n):
        seg=list(t[12*k:12*k+12]); seg+=[0x20]*(12-len(seg))
        out+=_pack_sb(seg,MASK_LAST if k==n-1 else MASK_MORE)
    return out
def header_crc(dest,sender,counter,text,b0=0x05,b1=0xA0):
    """CRC-16 of header bytes B0..B9 (this is what an ACK quotes to identify the message)"""
    t=bytes(text)
    h=[b0,b1,dest&0xff,(dest>>8)&0xff,sender&0xff,(sender>>8)&0xff,counter&0xff,len(t)]
    c=crc16(t); h+=[c&0xff,c>>8]
    return crc16(h)
def ack_frame_nibbles(to_id,orig_counter,orig_hdr_crc,orig_len=1,own_counter=0,nak=False,b1=0xB5):
    """Delivery-confirmation frame (one header superblock).  Field meaning of B1,B5,B6,B7 is inferred
    from a single recording (see HFPAGER_PROTOCOL.md) -- experimental."""
    h=[0x0A if nak else 0x0C,b1,to_id&0xff,(to_id>>8)&0xff,orig_counter&0xff,orig_len&0xff,own_counter&0xff,0,
       orig_hdr_crc&0xff,orig_hdr_crc>>8]
    c=crc16(h); h+=[c&0xff,c>>8]
    return _pack_sb(h,MASK_HDR)
def parse_frame(nibs):
    """nibs: stream of data nibbles (after the reference symbol, preamble stripped).
    Returns dict or None."""
    if len(nibs)<60: return None
    def block(sb,b):
        cw=[nibs[60*sb+4*i+b] for i in range(15)]
        return rs_decode(cw)
    def sbbytes(sb,mask):
        by=[0]*12; 
        for b in range(4):
            cw=block(sb,b)
            if cw is None: return None
            q=cw[8:15]
            if crc4(q[:6])^mask[b]!=q[6]: return None
            for p in range(3): by[4*p+b]=q[2*p]|(q[2*p+1]<<4)
        return by
    h=sbbytes(0,MASK_HDR)
    if h is None: return None
    if crc16(h[:10])!=(h[10]|(h[11]<<8)): return None
    if h[0] in (0x0A,0x0C):                   # delivery confirmation (0x0C = ACK) / refusal (0x0A, presumed NAK)
        return dict(kind='ack' if h[0]==0x0C else 'nak', b0=h[0], b1=h[1], dest=h[2]|(h[3]<<8),
                    orig_counter=h[4], f5=h[5], f6=h[6], f7=h[7], orig_crc=h[8]|(h[9]<<8),
                    sender=None, counter=h[4], length=0, text=b'')
    L=h[7]; nsb=(L+11)//12
    if len(nibs)<60*(1+nsb): return None
    text=[]
    for k in range(nsb):
        by=sbbytes(1+k,MASK_LAST if k==nsb-1 else MASK_MORE)
        if by is None: return None
        text+=by
    text=bytes(text[:L])
    if crc16(text)!=(h[8]|(h[9]<<8)): return None
    return dict(kind='beacon' if h[0]==0x06 else 'msg',b0=h[0],b1=h[1],dest=h[2]|(h[3]<<8),sender=h[4]|(h[5]<<8),counter=h[6],length=L,text=text,
                hdr_crc=h[10]|(h[11]<<8))

# ------------------------------------------------------------------ Gray / tone mapping
def gray(n): return n^(n>>1)
def ungray(v):
    n=v; 
    while v: v>>=1; n^=v
    return n
PRE_NIB=gray(4)   # = 6 -> +6 tones per symbol, 24 times
def tx_nibbles(frame_nibs): return [PRE_NIB]*PRE_COUNT+list(frame_nibs)+[0,0]
def nibbles_to_tones(nibs):
    t=0; seq=[0]
    for v in nibs:
        t=(t+2+ungray(v))%NTONE; seq.append(t)
    return seq

# ------------------------------------------------------------------ audio synthesis
def synth(tones,fs=48000,f0=F0,amp=0.85,lead=0.4,tail=0.4):
    sym=int(round(fs/BAUD)); n=len(tones)*sym
    f=np.repeat(np.array([f0+STEP*t for t in tones]),sym)
    # symbol grid at fractional sample positions
    pos=np.arange(n); idx=np.minimum((pos*BAUD/fs).astype(int),len(tones)-1)
    f=np.array([f0+STEP*t for t in tones])[idx]
    ph=2*np.pi*np.cumsum(f)/fs
    sig=amp*np.sin(ph)
    return np.concatenate([np.zeros(int(lead*fs)),sig,np.zeros(int(tail*fs))])
def write_wav(path,x,fs=48000):
    pcm=np.clip(x*32767,-32768,32767).astype('<i2')
    w=wave.open(path,'wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(fs); w.writeframes(pcm.tobytes()); w.close()
def read_wav(path):
    w=wave.open(path); n=w.getnframes(); fs=w.getframerate(); ch=w.getnchannels(); sw=w.getsampwidth()
    raw=w.readframes(n); w.close()
    if sw==2: a=np.frombuffer(raw,'<i2').astype(float)/32768
    elif sw==3:
        b=np.frombuffer(raw,np.uint8).reshape(-1,3)
        v=(b[:,0].astype(np.int32)|(b[:,1].astype(np.int32)<<8)|(b[:,2].astype(np.int32)<<16))
        a=np.where(v>=1<<23,v-(1<<24),v).astype(float)/2**23
    elif sw==4: a=np.frombuffer(raw,'<i4').astype(float)/2**31
    else: raise ValueError('unsupported sample width')
    return a.reshape(-1,ch)[:,0],fs

# ------------------------------------------------------------------ demodulator
def _sym_freqs(x,fs,t0,nsym,fmin,fmax):
    T=fs/BAUD; out=[]
    N=1<<17
    fr=np.fft.rfftfreq(N,1/fs); m=(fr>fmin)&(fr<fmax)
    for k in range(nsym):
        a=int(t0*fs+k*T+0.1*T); b=int(t0*fs+k*T+0.9*T)
        if b>len(x): break
        w=x[a:b]*np.hanning(b-a)
        S=np.abs(np.fft.rfft(w,N))
        out.append(fr[m][np.argmax(S[m])])
    return np.array(out)
def find_frames(x,fs):
    e=np.convolve(np.abs(x),np.ones(441)/441,'same'); act=e>0.05*e.max()
    d=np.diff(act.astype(int)); st=np.where(d==1)[0]/fs; en=np.where(d==-1)[0]/fs
    segs=[]
    for a,b in zip(st,en):
        if segs and a-segs[-1][1]<0.5: segs[-1][1]=b
        else: segs.append([a,b])
    return [(a,b) for a,b in segs if (b-a)*BAUD>60]
def _nibbles_from_freqs(f,mode,o0=0.0):
    """mode 0: independent differential rounding; mode 1: track slow frequency offset"""
    if mode==0:
        steps=np.round(np.diff(f)/STEP).astype(int)
    else:
        o=o0; t_prev=0; steps=[]
        base=f[0]
        for k in range(1,len(f)):
            t=int(np.round((f[k]-base-o)/STEP))
            o=(f[k]-base)-STEP*t
            steps.append(t-t_prev); t_prev=t
        steps=np.array(steps)
    n=(steps-2)%NTONE
    return [gray(int(v)) if v<16 else 99 for v in n][PRE_COUNT:]
def decode_segment(x,fs,t0,t1,fmin=1000,fmax=2400):
    """returns a list of candidate nibble streams (most plausible first)"""
    cands=[]
    nsym=min(int(round((t1-t0)*BAUD)),PRE_COUNT+1+60*23+3)
    for dt in (0.0,-0.004,0.004,-0.008,0.008):
        f=_sym_freqs(x,fs,t0+dt,nsym,fmin,fmax)
        cands.append(_nibbles_from_freqs(f,0))
        for o0 in (0.0,0.25*STEP,-0.25*STEP):
            cands.append(_nibbles_from_freqs(f,1,o0))
    return cands
def decode_wav(path,speeds=None):
    x,fs=read_wav(path); res=[]
    names=speeds or list(SPEEDS); prev=CUR_SPEED
    for name in names:
        set_speed(name)
        for a,b in find_frames(x,fs):
            ns=(b-a)*BAUD
            if ns<PRE_COUNT+1+60: continue
            g=_sym_freqs(x,fs,a,10,1000,2400)                 # cheap preamble gate: constant +6-tone steps
            st=np.round(np.diff(g)/STEP).astype(int)%NTONE
            if np.sum(st==6)<6: continue
            fr=None
            for nib in decode_segment(x,fs,a,b):
                fr=parse_frame(nib)
                if fr: break
            if fr: fr['speed']=name; res.append((a,fr))
    res.sort(key=lambda r:r[0])
    set_speed(prev)
    return res

def main():
    ap=argparse.ArgumentParser(); sp=ap.add_subparsers(dest='cmd')
    t=sp.add_parser('tx'); t.add_argument('out'); t.add_argument('text')
    t.add_argument('--dest',type=int,required=True); t.add_argument('--sender',type=int,required=True)
    t.add_argument('--counter',type=int,required=True); t.add_argument('--f0',type=float,default=F0)
    t.add_argument('--speed',choices=list(SPEEDS),default='5.86')
    r=sp.add_parser('rx'); r.add_argument('wav')
    a=ap.parse_args()
    if a.cmd=='tx':
        set_speed(a.speed); txt=a.text.encode('cp1251')
        nib=tx_nibbles(frame_nibbles(a.dest,a.sender,a.counter,txt))
        write_wav(a.out,synth(nibbles_to_tones(nib),f0=a.f0))
        print('wrote',a.out,len(nib)+1,'symbols,',round((len(nib)+1)/BAUD,1),'s')
    elif a.cmd=='rx':
        for t0,fr in decode_wav(a.wav):
            if fr['kind'] in ('ack','nak'):
                print('%.2fs [%s Bd] %s to=%d orig_msg#=%d orig_hdr_crc=%04x'%(t0,fr['speed'],fr['kind'].upper(),fr['dest'],fr['orig_counter'],fr['orig_crc']))
            else:
                print('%.2fs [%s Bd] dest=%d sender=%d msg#=%d type=%02x/%02x len=%d text=%r'%(t0,fr['speed'],fr['dest'],fr['sender'],fr['counter'],fr['b0'],fr['b1'],fr['length'],fr['text'].decode('cp1251','replace')))
    else: ap.print_help()
if __name__=='__main__': main()
