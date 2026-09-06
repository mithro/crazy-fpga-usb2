# P3 result: transmit path in simulation (2026-09-07)

Modules: `usb2soft/tx/encoder.py` (`PacketEncoder`, 4 line bits + 4 output-enable bits per
120 MHz cycle), `usb2soft/tx/bridge.py` (`TxUTMIBridge`, depth-4 async FIFO from the 60 MHz
`usb` domain), `usb2soft/tx/__init__.py` (`TxPath`). Tests: `tests/test_encoder.py` (8),
`tests/test_tx_path.py` (6), `tests/test_turnaround.py` (1).

## What is proven

- Encoder output is bit-exact against the wire model (`usbhs.packet_line_bits`) for 1-byte,
  16-byte, 40 × `0xFF` (maximum stuffing) and 300 random bytes; two back-to-back packets decode
  with the reference decoder; the output enable covers exactly the packet's bits and is low on
  the very next bit after the EOP.
- Starved source: the encoder closes the packet with a proper EOP and sets the sticky
  `underrun` flag; the truncated packet is still well-formed.
- Through the UTMI bridge with a LUNA-style source (hold `tx_valid`, advance on `tx_ready`):
  packets of 1, 3, 8, 64 and 200 bytes are bit-exact. `tx_ready` duty is >95 % for an all-zero
  payload (one byte per 60 MHz cycle is the line rate) and 80–90 % for all-ones (6/7 from
  stuffing).
- Round trip TX → resampled wire (−500/0/+500 ppm) → P2 `RxPath` → UTMI: payloads identical,
  no `rx_error`, for 4× and for 3× oversampling.

## Turnaround budget (`tests/test_turnaround.py`)

USB HS gives a device 192 bit times (400 ns) from the last received bit to the first bit of
its response.

| Segment | measured |
|---|---|
| last received line bit → `rx_active` low | 83 ns (40 bit times; reference is the first idle *word*, up to 4 bit times after the true last bit) |
| `tx_valid` asserted → first driven bit | 50 ns (24 bit times) |
| gateware total (no I/O primitives) | 133 ns (64 bit times) |

**Caveat:** this excludes the I/O primitives, which the simulation does not model:
IBUFDS + IDELAY + ISERDESE2 (about 2 CLKDIV cycles) on the way in and OSERDESE2 + OBUFTDS
(about 3 CLK cycles) on the way out, roughly 20–25 bit times more. Budget for the MAC is
therefore about 100 bit times, not 124.

The two `AsyncFIFOBuffered` crossings account for most of it; a P8 candidate is to run the
decoder/encoder in the `usb` domain's phase-related 120 MHz clock and replace the FIFOs by
synchronous gearboxes once the hardware confirms the MMCM phase relationship (that removes
≈30–40 bit times).

## Design decision recorded in the spec (§4.4, revision 6)

The serialiser will be OSERDESE2 in 4:1 DDR mode (CLK 240 MHz, CLKDIV 120 MHz) with
`TRISTATE_WIDTH=4`, because that is the only configuration with a per-bit tristate word; 8:1 mode
forces `TRISTATE_WIDTH=1` (byte-granular), which would dribble up to seven extra bits after the
EOP. The encoder therefore produces 4 bits per 120 MHz cycle, the same word width as the RX
decoder, and the tristate bits ride through the same serialiser as the data so no fabric delay
matching is needed.

## Deviations from the plan

- The empty-payload case was dropped from the bit-exact tests: USB has no empty packets and a
  lone end marker is swallowed by the encoder.
- The round trip covers 1–512 bytes (not 0–512); the plan's "tx_valid falling → last oe bit"
  latency was not measured separately.
- The byte FIFO is `AsyncFIFOBuffered(depth=4)`, which Amaranth rounds to 5 entries.

## Review fixes

- The starvation rule closed the packet with fewer than four bits queued, which put an
  output-enable hole before the EOP (a detached EOP burst on a real bus). It now closes at
  `fill < 8`, and the whole 32-bit SYNC is preloaded into a 44-bit queue so data refills start
  with 32 bits of margin (otherwise a source that withdraws `valid` on alternate cycles could
  livelock at data entry). `test_starved_source_closes_packet_contiguously_and_flags_underrun`
  asserts one contiguous segment, a strict payload prefix and `underrun == 1`; the bit-exact
  tests assert `underrun == 0`; stuffing-boundary cases `00 FC`, `7F 01`, `FF FF`, `FE FF 01`
  are pinned.
- `busy` now also covers the registered output word.

## Bugs found by the tests

- `Cat(0, 1)` as the end-marker FIFO word put the marker bit into the data field (a phantom
  `0x02` byte was transmitted and the packet only closed via the underrun path). Fixed with
  explicit `Const(0, 8)`.
- The first encoder testbench sampled `byte_ready` after the clock edge and therefore observed
  the *next* cycle's handshake; the harness now uses the proper valid/ready idiom (set inputs,
  read `ready`, tick).
