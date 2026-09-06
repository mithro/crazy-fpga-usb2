# P4 result: PHY datapath in silicon (2026-09-07)

Applet: `link-test` (`usb2soft/applets/link_test.py`): `TxPath` → loopback → `RxPath` with a
packet generator/checker (sequence number, LFSR payload of 8/64/512 bytes, Fletcher-16),
counters reported once per second over the UART as
`L <tx> <rx_good> <rx_bad> <rx_err> <seq_gaps> <slips_up> <slips_down> <cdr_phase>` (hex).
Clocks from `NeTV2PhyClocks` (PLL 50→60/300 MHz, MMCM 960 MHz VCO → 240/120/60 MHz;
480 MHz only in HDMI mode).

## Build: internal loopback, XC7A100T (`build/link-test-internal-netv2-a7-100/vivado`)

| Item | Value |
|---|---|
| WNS / WHS / WPWS | 0.471 ns / 0.078 ns / 1.084 ns (all met) |
| Worst path | `core/rx/cdr/count` → `core/rx/decoder/acc_n` at 120 MHz: 8 logic levels, 1.46 ns logic + 5.93 ns routing |
| Clocks present | 50 (input), 60 (`usb`), 120 (`rx_cdr`/`tx_cdr`), 240 (`tx_io`); no 480 MHz (no ISERDES in this mode) |
| Whole design | 960 LUTs, 914 FFs, 87 CARRY4, 0 BRAM, 6 BUFG, 1 PLL, 1 MMCM |

Hierarchical utilisation (`top_utilization_hierarchical_place.rpt`):

| Module | LUTs | FFs | Note |
|---|---|---|---|
| `core/rx` (PHY receive) | 229 | 131 | cdr 104/23, decoder 89/32, bridge (event FIFO + UTMI) 37/76 |
| `core/tx` (PHY transmit) | 218 | 94 | encoder 163/49, bridge (byte FIFO) 55/45 |
| **PHY datapath total** | **447** | **225** | budget in spec §8: ≤700 LUTs, ≤600 FFs |
| `core/console` (text reporter + UART) | 328 | 259 | test instrument |
| `core/chk` | 88 | 222 | test instrument |
| `core/gen` | 95 | 101 | test instrument |

## Hardware: rpi5-netv2 (NeTV2 XC7A100T), internal loopback, phase rotation 1

Log: `logs/2026-09-07-linktest-internal-rpi5.log` (12 s capture, compressed bitstream loaded in
12.5 s over GPIO JTAG). Every line has `tx == rx_good`, zero bad, zero errors, zero gaps:

```
L 001092A3 001092A3 00000000 00000000 00000000 00000000 00000001 3
...
L 003E25E0 003E25E0 00000000 00000000 00000000 00000000 00000001 3
```

- 4 072 928 packets in the capture window (≈255 000 packets/s ≈ 51 MB/s of payload, i.e. close
  to the 60 MB/s line rate given the 16-cycle inter-packet gaps and stuffing).
- One `slip_down` at start (initial acquisition), then the phase pointer stayed at 3 for the
  whole run, as expected for a loopback with no frequency offset.
- The `bad`/`errors` LEDs stayed off; the fault-injection path (`inject` mask) is exercised in
  simulation only.

What this proves: the CDR, packet decoder, event FIFO, byte FIFO and encoder work at their real
clock rates (120 MHz and 60 MHz) on the actual FPGA, the MMCM clock plan locks, and the UTMI-side
protocol is consistent across ~4 million packets. What it does not prove: the 480 MHz ISERDES /
240 MHz OSERDES I/O path and tracking of a genuinely asynchronous source; those need a physical
channel (see below).

## HDMI mode

Build `build/link-test-hdmi-rx0-netv2-a7-100/vivado` instantiates the sampler on HDMI RX0
lane d0 and the serialiser on HDMI TX0 lane d0:

| Item | Value |
|---|---|
| WNS / WHS / WPWS | 0.445 ns / 0.036 ns / 0.491 ns (all met; WPWS is the 480 MHz BUFG pulse width) |
| Clocks present | 50, 60 (`usb`, MMCM ref), 120 (`cdr`), 240 (`tx_io`), **480 (`rx_io`)**, 300 (`idelay_ref`) |
| Primitives | 2× ISERDESE2, 2× IDELAYE2, 1× IDELAYCTRL, 1× OSERDESE2, IBUFDS_DIFF_OUT, OBUFTDS |
| Whole design | 999 LUTs, 915 FFs; no critical warnings, DRC clean |

There is no cable between TX0 and RX0 on rpi5-netv2 (measured in P1), so a hardware run
reports `rx == 0`; the bitstream is ready for the moment a TX0→RX0 cable or the cross-connected
NeTV2 units are available. The sample-order parameters (`q_reversed`, `slave_first`) are
unverified until then.
