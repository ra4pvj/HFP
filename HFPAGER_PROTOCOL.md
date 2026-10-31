# HFPager (PS-18) modem — reverse-engineered format

Source of facts: 53 recordings (5.86 Bd, 23.44 Bd, one beacon) and the native modem library
(`lib_hfp_rx.so`, CRC lookup tables and check-nibble logic). Everything marked **[verified]**
was reproduced bit-exactly: 47/47 frames at 5.86 Bd and 5/5 frames at 23.44 Bd decode with all
CRCs passing, and the transmitter in `hfpager_modem.py` reproduces the recorded tone sequences.

## 1. Physical layer

* Constant-amplitude, **phase-continuous** FSK, 18 tones (index 0..17). **[verified]**
* Tone frequency: `f = f0 + step * t`. `f0` is user-selectable (recordings: tone 0 = 1611.4 Hz).
* **Differential (incremental) coding, DominoEX-style:** for every data nibble `v`
  `t[k+1] = (t[k] + 2 + gray_inverse(v)) mod 18`, i.e. `v = gray(((t[k+1]-t[k]) mod 18) - 2)`,
  `gray(n) = n ^ (n >> 1)`. **[verified]**
* Receiver tolerance: recordings contain slow/step frequency offsets of up to ±0.5 tone, so
  the demodulator must work on tone differences and rely on FEC.

| Mode | Symbol rate | Samples/symbol @48 kHz | Tone step | Preamble (symbols) | Status |
|---|---|---|---|---|---|
| 5.86 Bd | 48000/8192 = 5.859375 | 8192 | 2 x baud = 11.71875 Hz | 24 | verified |
| 23.44 Bd | 48000/2048 = 23.4375 | 2048 | 1 x baud = 23.4375 Hz | 76 | verified |
| 1.46 Bd | 48000/32768 | 32768 | ~2 x baud (6-tone preamble step 17.6 Hz) | ~11? | **not decoded** |
| 46.88 Bd | – | – | – | – | no recording |

Frame on the air: `[preamble: N symbols][reference symbol][nibbles ...][2 trailing symbols]`.
The preamble is the nibble `6` repeated N times (= +6 tones per symbol, tone pattern 0,6,12,0,6,12...).
The symbol after the preamble is the differential reference; data nibble 0 is the step from the
reference symbol to the next one. The two trailing symbols carry no data.

## 2. Nibble stream: superblocks

* Data stream = `1 + n` superblocks of **60 nibbles** (n = ceil(text_length / 12), 1..21).
* One superblock = **4 interleaved RS(15,7) blocks**: stream position `p = 4*i + b`
  (`i` = symbol index in the codeword 0..14, `b` = block 0..3). **[verified]**
* **RS(15,7) over GF(16)**, primitive polynomial `x^4 + x + 1`, roots `alpha^1 .. alpha^8`
  (narrow sense), systematic, **parity first**: `c[0..7]` parity, `c[8..14]` the 7 data nibbles,
  `c(x) = sum c[i] x^i`. Corrects up to 4 nibble errors per block. **[verified]**
* The 7 data nibbles of a block, `q0..q6`: `q0..q5` = three bytes (low nibble first), `q6` = CRC-4:
  `q6 = crc4(q0..q5) xor MASK[b]`, where `crc4` starts at `0xF` and for every nibble does
  `c = T4[(nibble ^ c) & 0xFF]` (T4 = reflected CRC with polynomial 0xC applied bytewise, 256-entry
  table in the app); `MASK` = `[0,2,4,6]` for the header superblock, `[0,3,5,7]` for text
  superblocks that are followed by another one, `[0,8,9,10]` for the last text superblock. **[verified]**
* Byte layout of a superblock (12 bytes): byte `4*p + b` = data bytes `p` of block `b`:
  `B0..B3 = q0q1 of blocks 0..3`, `B4..B7 = q2q3`, `B8..B11 = q4q5`.

## 3. Header superblock (first), bytes B0..B11 [verified]

| Byte | Meaning |
|---|---|
| B0 | frame type: `0x05` message, `0x06` beacon, **`0x0C` delivery confirmation (ACK)** [recorded]; `0x0A` presumably refusal (NAK, not recorded) |
| B1 | `0xA0`; **bit 1 set (`0xA2`) = delivery confirmation (ACK) requested** |
| B2,B3 | destination ID, 16 bit little endian (beacon: 0) |
| B4,B5 | sender ID, 16 bit little endian (recordings: 10048 = 0x2740) |
| B6 | message number (the number shown in the app), 8 bit, +1 per sent message |
| B7 | text length in bytes (1..252) |
| B8,B9 | CRC-16 of the text (unpadded), little endian |
| B10,B11 | CRC-16 of B0..B9, little endian |

CRC-16 = reflected polynomial 0x8408 (the table in the app starts 0000 1189 2312 ...), init `0xFFFF`,
no final XOR, update `c = (c >> 8) ^ T16[(c ^ byte) & 0xFF]`.

## 4. Text superblocks [verified]

Each carries 12 text bytes (single-byte code page; Windows-1251 in the Android app), padded with `0x20`.
Text byte `k` of a superblock is `B[k]` in the layout above. 1..12 chars -> 2 superblocks in total,
13..24 -> 3, 25..36 -> 4, ..., 252 chars -> 22.
Beacon text is `Beacon from <CALLSIGN>` (type 0x06, destination 0); after the digital frame the beacon
file contains a further ~39 symbol-times of signal (probably a CW identification), not decoded.

## 4a. Delivery confirmation (ACK) frame  [one recording, partially verified]

Sent by the addressee after a message with the ACK-request bit (B1 bit 1). One header superblock only
(no text superblocks, `PRE + 1 + 60 + 2` symbols), same preamble/RS/CRC-4 (masks 0,2,4,6)/CRC-16 rules.

| Byte | Value in the recording | Meaning |
|---|---|---|
| B0 | `0x0C` | ACK |
| B1 | `0xB5` | flags (meaning of the bits not known) |
| B2,B3 | `01 00` | **destination of the ACK = ID of the original sender (1)** |
| B4 | `06` | **message number of the original message (#6)** |
| B5 | `01` | = length of the original text (1) or a status code — not separated yet |
| B6 | `1E` | probably the confirming station's own message counter (30) — unverified |
| B7 | `00` | unknown |
| B8,B9 | `B6 19` | **CRC-16 of the original header bytes B0..B9 (= B10,B11 of the original frame)** — verified: it equals the CRC of the message ID1 -> 10048, #6, text "A", ACK requested |
| B10,B11 | CRC-16 of B0..B9 | as in all frames |

The ACK does not contain the confirming station's ID: the original sender recognises it by
(destination = own ID, original message number, original header CRC). `hfpager_modem.ack_frame_nibbles`
re-encodes the recorded ACK bit-exactly (including RS parity) from these fields.

## 5. Open items

1. 1.46 Bd mode: preamble length, tone mapping and possibly a different frame arrangement.
2. Meaning of ACK bytes B1, B5, B6, B7; the NAK frame (type 0x0A?); retransmission and timeout logic —
   needs more ACK/NAK recordings (different lengths, counters, speeds).
3. Exact meaning of the 2 trailing symbols, the CW tail of the beacon, GPS frames.
4. Whether the `0xA0` byte carries a protocol version / more flag bits.

Transmitting on HF requires an amateur-radio (or other appropriate) licence.
