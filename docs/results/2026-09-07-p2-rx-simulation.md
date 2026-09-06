# P2 result: receive path in simulation (2026-09-07)

Modules: `usb2soft/sim/usbhs.py` (wire model), `usb2soft/rx/cdr.py`
(`OversamplingCDR`), `usb2soft/rx/decoder.py` (`PacketDecoder`),
`usb2soft/rx/bridge.py` (`RxUTMIBridge`), `usb2soft/rx/__init__.py` (`RxPath`).
Tests: `tests/test_usbhs_model.py` (7), `tests/test_cdr.py` (38),
`tests/test_decoder.py` (22), `tests/test_rx_path.py` (8); sweeps in
`tests/test_rx_margins.py` (`uv run pytest -m slow -q -s tests/test_rx_margins.py`).
Full suite: `131 passed, 2 skipped` (the two skips are phase points ≥ S for 3x).

## What is proven

- Bit-exact recovery of NRZI line bits at every static sample phase for 3x
  (W=12) and 4x (W=16) oversampling.
- Frequency offsets of ±100 … ±2000 ppm tracked over 8 kbit packets with
  zero errors; slip count equals the drift in UI (±2) and its sign matches the
  offset direction.
- Random jitter, deterministic (sinusoidal) jitter plus offset, phase steps
  between packets, and 200 UI of comparator chatter before a packet: all
  recovered.
- Decoder: SYNC lengths 12–32 bits, any word chunking 1–5 bits, all-ones
  payload (stuffing), empty/1/3-byte packets, back-to-back packets with an
  8 UI gap, misaligned EOP → ERROR, loss of activity mid-packet → ERROR,
  six-zero run is not a SYNC.
- End to end (samples in the 120 MHz `cdr` domain → UTMI in the 60 MHz `usb`
  domain): random packets at −1000 … +1000 ppm, a 1 kB all-zero payload at
  +500 ppm (maximum byte rate through the 16-entry elastic FIFO), the 3x
  variant, and packets embedded in noise with 0.06 UI rms jitter. UTMI
  ordering is asserted in the testbench: `rx_valid` only while `rx_active`,
  never in the cycle `rx_active` rose.

## Measured margins (S=4, W=16, 256-byte packets, +500 ppm, 10 seeds)

| Gaussian jitter (UI rms) | packets intact |
|---|---|
| 0.00 | 10/10 |
| 0.04 | 10/10 |
| 0.08 | 10/10 |
| 0.10 | 10/10 |
| 0.12 | 5/10 |
| 0.16 | 0/10 |
| 0.20 | 0/10 |

Same sweep for S=3, W=12: 0.04 → 10/10, 0.08 → 8/10, 0.10 → 1/10, ≥0.12 → 0.

Frequency-offset limit (S=4, 8 kbit packet, phase 0.5): 4000, 8000 and
16000 ppm tracked; 32000 ppm fails. The design target is ±500 ppm.

`track_threshold` 1, 2, 3, 4 or 6 all give 10/10 at 0.10 UI rms; the
default stays 3.

## A design change found by measurement

The first S=4 vote rule (edge one slot late votes "pick too early") let the
loop settle with the mean edge in the middle of a sample slot, which puts the
pick 0.625 UI after the edge and only 0.375 UI before the next one. Bit
errors appeared from 0.06 UI rms jitter. Ignoring the one-slot-late case
(a two-slot dead zone) moves the equilibrium to "mean edge on a sample
instant", centring the pick 0.5 UI after it. That single change moved the
knee from 0.06 to 0.12 UI rms (6/6 → and then 10/10 at 0.08, 0/6 → 10/10 at
0.10). Recorded in the CDR source and in the spec §4.2.

## Deviation from the original spec, now folded back

The decoder runs on the CDR's ≤5-bit words at 120 MHz and hands bytes and
events to the `usb` domain through a 16-entry `AsyncFIFOBuffered` (which is
also the elastic buffer), instead of a 2:1 gearbox plus a 10-bit-wide 60 MHz
decoder. Prefix logic over 5 bits is roughly a quarter of the 10-bit version,
120 MHz is comfortable on Artix-7 -2, and the async FIFO removes any
assumption about the phase between `rx_cdr` and `usb`.

## Not yet measured

- FPGA resource usage of the RX path (P4 builds it into a bitstream).
- Behaviour with a real receiver eye (P4 over HDMI, later real USB).
