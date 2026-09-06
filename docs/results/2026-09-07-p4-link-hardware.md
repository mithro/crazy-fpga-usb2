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
| `core/tx` (PHY transmit) | 216 | 94 | encoder 163/49, bridge (byte FIFO) 53/45 |
| **PHY datapath total** | **445** | **225** | budget in spec §8: ≤700 LUTs, ≤600 FFs |
| `core/console` (text reporter + UART) | 328 | 259 | test instrument |
| `core/chk` | 88 | 222 | test instrument |
| `core/gen` | 95 | 101 | test instrument |

### Rebuilt at the review-fixed head (commit after the code review)

The table above is from the build that produced the first hardware run (before the P3 review
fixes to the encoder were rebased in). After the review fixes (CDR-domain 32-bit slip counters,
overflow field, IDELAY loader, per-generator resets) the four modes rebuild as:

| Mode | WNS / WHS | LUTs / FFs (whole design) | `core/rx` | `core/tx` | Notes |
|---|---|---|---|---|---|
| internal | 0.356 / 0.073 ns | 1018 / 982 | 234 / 131 | 241 / 106 | encoder 185/61 after the 44-bit SYNC-preload queue |
| hdmi (RX0) | 0.318 / 0.069 ns (WPWS 0.491 at 480 MHz) | 1065 / 984 | same | same | LD now driven; `IS_*_INVERTED` explicit |
| async-fast | 0.353 / 0.078 ns | 1446 / 1227 | same | same | 2 PLLE2 + MMCME2 |
| async-slow | 0.429 / 0.013 ns | 1447 / 1227 | same | same | |

PHY datapath at head: **475 LUTs / 237 FFs** (budget ≤700 / ≤600). Vivado methodology check
LUTAR-1 (LUT driving async reset) is gone in all four.

## Hardware: rpi5-netv2 (NeTV2 XC7A100T), internal loopback, phase rotation 1

Log: `logs/2026-09-07-linktest-internal-rpi5.log` (12 s capture, compressed bitstream loaded in
12.5 s over GPIO JTAG). Every line has `tx == rx_good`, zero bad, zero errors, zero gaps:

```
L 001092A3 001092A3 00000000 00000000 00000000 00000000 00000001 3
...
L 003E25E0 003E25E0 00000000 00000000 00000000 00000000 00000001 3
```

- 4 072 928 packets since load (2.99 M inside the 12 s window; consecutive lines differ by
  271 525 packets/s ≈ 53 MB/s of payload, close to the 60 MB/s line rate given the 16-cycle
  inter-packet gaps and stuffing).
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

## Hardware: rpi5-netv2, asynchronous loopback (TX PLL offset by +3788 / −4630 ppm)

`--mode async-fast` / `--mode async-slow` clock the TX path from a second PLLE2 at
120.4545 MHz / 119.4444 MHz and cross into the 120 MHz CDR domain through `AsyncResampler`,
which drops or repeats single samples (see `docs/hardware-setups.md` §0). Builds
`build/link-test-async-{fast,slow}-netv2-a7-100/vivado`: WNS 0.283 / 0.561 ns, WHS 0.076 /
0.088 ns, all met (the first build missed by 1.6 ns until the resampler was pipelined and
`raw_tx_async` was declared asynchronous with `set_clock_groups`); 1450 LUTs / 1165 FFs whole
design (2 PLLE2 + 1 MMCME2, 8 BUFGs). Logs: `logs/2026-09-07-linktest-async-fast-rpi5.log`,
`logs/2026-09-07-linktest-async-slow-rpi5.log`, 30 s each.

| Mode | Packets (first → last line) | bad / err / gaps | Major slips per second | Predicted | Minor slips per second |
|---|---|---|---|---|---|
| async-fast (+3788 ppm) | 0x1D94BC → 0x98217B (8.0 M) | 0 / 0 / 0 on all 30 lines | 1 687 285 down | 3788e-6 × 480e6 × 0.93 duty ≈ 1.69 M | 8 275 up |
| async-slow (−4630 ppm) | 0x10CA2D → 0x8A8404 (8.0 M) | 0 / 0 / 0 on all 30 lines | 2 064 632 up | 4630e-6 × 480e6 × 0.93 duty ≈ 2.07 M | 9 937 down |

```
L 0093E7A9 0093E7A8 00000000 00000000 00000000 000469A0 0385238E 0   (fast: tx, good differ by the packet in flight)
L 008A8404 008A8404 00000000 00000000 00000000 040FA545 00050396 0   (slow)
```

- On every line `rx_good` equals `tx` or `tx − 1` (one packet in flight when the counters were
  sampled); the bad/error/gap counters stayed zero for 16 M packets across both runs.
- A slip is one phase-pointer wrap, i.e. one UI of accumulated drift, so the slip rate is
  ppm × bit rate × packet duty cycle (the CDR only tracks while a packet is present); the measured
  rates match that prediction within 2 %. The minor-direction slips (0.5 % of the major rate,
  ≈0.03 per packet) are acquisition steps at packet start, where the CDR moves with a vote
  threshold of 1.
- The offsets are 7.6× and 9.3× the USB 2.0 tolerance of ±500 ppm, so this covers the
  "lock even as the USB signal drifts" requirement on real silicon, at real clock rates, without
  a cable. It does not exercise the SelectIO front-end (that needs a physical channel).
