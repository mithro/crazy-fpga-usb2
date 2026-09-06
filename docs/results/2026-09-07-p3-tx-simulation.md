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
| last received line bit → `rx_active` low | 83 ns (40 bit times) |
| `tx_valid` asserted → first driven bit | 58 ns (28 bit times) |
| PHY-only total | 142 ns (68 bit times), leaving 124 for the MAC |

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

## Bugs found by the tests

- `Cat(0, 1)` as the end-marker FIFO word put the marker bit into the data field (a phantom
  `0x02` byte was transmitted and the packet only closed via the underrun path). Fixed with
  explicit `Const(0, 8)`.
- The first encoder testbench sampled `byte_ready` after the clock edge and therefore observed
  the *next* cycle's handshake; the harness now uses the proper valid/ready idiom (set inputs,
  read `ready`, tick).
