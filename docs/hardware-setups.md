# Hardware set-ups for validating the USB 2.0 soft PHY

This document lists every physical arrangement that can validate part of the
design, what each one proves, what it cannot prove, and the exact pins. Keep
it current: whenever a new way to test on hardware is found or used, add it
here together with a pointer to the results in `docs/results/`.

All pin/bank data below was checked against the prjxray `package_pins.csv`
for `xc7a35tfgg484` / `xc7a35tcsg324` and the litex-boards platform files on
2026-09-06.

## Summary

| # | Set-up | Proves | Cannot prove | Status |
|---|--------|--------|--------------|--------|
| 1 | NeTV2 ↔ NeTV2 over HDMI cables (TMDS pairs) | 480 Mbit/s CDR, framing, stuffing, drift tracking with independent crystals, digital squelch, two-way packet exchange, resource use | full-speed line states, chirp, real USB electrical levels/terminations | boards exist at Welland; needs `pi` key authorisation on the four cross-connected units |
| 2 | NeTV2 self-loopback: HDMI out → own HDMI in | everything in 1 except independent-crystal drift (same clock both ends; drift must be emulated by the P6 phase slewer) | as 1 | **not present on `rpi5-netv2`** (measured 2026-09-06 with the discovery applet: no lane hears a beacon); needs a cable to be fitted |
| 3 | NeTV2 direct USB via PCIe "hax"/SMBus pair + bunnie's `netv2mvp-usb3-v1` breakout | real USB connector to a real host: FS line states, reset, chirp, HS data (with the adaptor network below) | nothing else once the network exists | needs the breakout and a resistor network; not installed on any Welland board today |
| 4 | Arty A7 PMOD + USB breakout, Raspberry Pi as host | same as 3 on the Arty fleet, plus the Pi is right there as host | — | needs a PMOD USB breakout and the resistor network |
| 5 | Future purpose-built board | everything | — | pin requirements in §6 |
| 0 | NeTV2 internal loopback with an offset TX PLL (no cable) | the whole digital datapath at real clock rates, CDR tracking of a genuine ±4000 ppm frequency offset, MMCM/PLL clock plan | the SelectIO front-end (ISERDES/OSERDES, IDELAY), real signal integrity | **works on `rpi5-netv2`** (`link-test --mode internal|async-fast|async-slow`, §0) |

## 0. NeTV2 internal loopback (no cable)

`uv run usb2soft build link-test --variant a7-100 --mode internal|async-fast|async-slow`
runs `TxPath → wire model → RxPath` inside the FPGA on any NeTV2 with only the
UART connected. `internal` expands the encoder's 4 line bits into 16 samples
per 120 MHz cycle (a synchronous loopback). `async-fast` / `async-slow` clock
the TX path from a second PLLE2 at 120.4545 MHz (+3788 ppm) / 119.4444 MHz
(−4630 ppm) and cross into the 120 MHz CDR domain through `AsyncResampler`
(`usb2soft/sim/expander.py`), which drops or repeats single samples: the CDR
sees exactly what a host crystal at that offset would produce. Both are well
outside the USB 2.0 limit of ±500 ppm. The UART line
`L <tx> <good> <bad> <err> <gaps> <slip_up> <slip_down> <phase>` gives the
packet counters and the CDR's slip counts (one slip per 1/(4·ppm) UI in the
direction of the offset). Results: `docs/results/2026-09-07-p4-link-hardware.md`.

## 1. NeTV2 ↔ NeTV2 over HDMI

The NeTV2 has two HDMI inputs and two HDMI outputs on ordinary 3.3 V HR banks
(`TMDS_33`). An HDMI cable from board A's TX port to board B's RX port gives a
differential 480 Mbit/s channel with the receiver's 50 Ω pull-ups to 3.3 V
already on the board. Tri-stating the OBUFTDS driver leaves a zero
differential at the receiver, which is exactly the "squelch" situation a USB
HS receiver must survive between packets.

Pins (litex-boards `kosagi_netv2.py`; "inv" = pair swapped on the PCB, so
invert in gateware):

| Port | clk | data0 | data1 | data2 | bank |
|------|-----|-------|-------|-------|------|
| hdmi_in 0 (RX0) | L19/L20 inv | K21/K22 inv | J20/J21 inv | J22/H22 inv | 15 |
| hdmi_in 1 (RX1) | Y18/Y19 inv | AA18/AB18 | AA19/AB20 inv | AB21/AB22 inv | 14 |
| hdmi_out 0 (TX0) | W19/W20 inv | W21/W22 | U20/V20 | T21/U21 | 14 |
| hdmi_out 1 (TX1) | G21/G22 inv | E22/D22 inv | C22/B22 inv | B21/A21 inv | 16 |

RX0 also has DDC (SCL T18, SDA V18) and RX1 (SCL W17 inv, SDA R17): slow
side channels that can carry out-of-band information such as a board ID.

The HDMI inputs pass through a TMDS switch/ESD part on the board (see the
`netv2mvp-pvt1` schematic, page with `RX0_TMDS*` and `IO1A/IO1B`); HPD is
"pass-through with overrides". The `hdmi_discovery` applet drives a unique
board/port identifier on every TX lane and reports what each RX lane receives
over the UART, so the actual cabling of the four cross-connected units is
measured rather than assumed.

Hosts: `pi-sw1-p10/12/14/16` (`10.21.1.10/.12/.14/.16`) behind
`welland.fpgas.online`; user `pi`; GPIO JTAG TCK=GPIO4, TMS=17, TDI=27,
TDO=22 (`openFPGALoader --cable libgpiod --pins 27:22:4:17` or openocd
`bcm2835gpio`); UART `/dev/ttyS0` at 115200. Access is blocked until the
user authorises the key for `pi` there.

## 2. NeTV2 self-loopback

Same pins as §1 with the cable from TX0 to RX0 of the same board. On
`rpi5-netv2` (XC7A100T, `tim@rpi5-netv2.welland.mithis.com`, UART
`/dev/ttyAMA0`, loader `host/netv2_run.py --host rpi5-netv2`) the discovery
applet measured on 2026-09-06 that **no** HDMI RX lane hears the board's own
TX beacons (`docs/results/2026-09-06-p1-hdmi-discovery.md`), so no loopback
cable is fitted there today. Because both ends share one crystal there is no
natural frequency offset; drift tolerance is exercised by slewing the TX
MMCM phase (the P6 mechanism run open-loop as a test stimulus) or by using
separate PLLs for RX and TX with different fractional multipliers.

## 3. NeTV2 direct USB via the PCIe edge

The NeTV2 PCIe edge carries the SMBus pair on B5/B6, routed on the mainboard
as a differential pair to FPGA balls **D19 (SM_N) / E19 (SM_P)**
(`IO_L14N/P_T2_SRCC_16`, bank 16, 3.3 V). Bunnie's `netv2mvp-usb3-v1`
breakout is a PCIe-edge card with a USB connector whose D- goes to SM_N and
D+ to SM_P, plus VBUS and the JTAG/hax pins on headers. luna-boards' NeTV2
platform instead wires D+/D- to hax pins **A15/A14** with a pull-up on
**C17** (someone's hand-soldered arrangement; it notes a fixed pull-up on D-
that must be moved for FS device use).

Either way the FPGA sees D+/D- on plain 3.3 V pins with no termination. To run
the soft PHY there the adaptor network in §5 is required. Nothing at Welland
has this fitted today; `rpi5-netv2`'s PCIe edge is used by the Pi 5 PCIe link,
so this set-up needs a spare NeTV2 or one of the four sw1 units.

## 4. Arty A7 PMOD + USB breakout, Raspberry Pi as host

The Arty A7 boards at Welland sit on Raspberry Pis with PMOD HATs, and the Pi
is a real USB 2.0 host. A PMOD-format USB breakout (connector + the §5
network) on PMOD JB gives four true differential pairs on bank 15:

| JB pin | ball | pad |
|--------|------|-----|
| 1/2 | E15 / E16 | IO_L11P/N_T1_SRCC_15 |
| 3/4 | D15 / C15 | IO_L12P/N_T1_MRCC_15 |
| 7/8 | J17 / J18 | IO_L23P/N_T3_15 |
| 9/10 | K15 / J15 | IO_L24P/N_T3_15 |

Suggested assignment: pair 1 = D+/D- differential input; pair 2 = D+ and D-
single-ended inputs (line state); pair 3 = driver outputs; pair 4 = pull-up
enable and HS-termination enable. PMOD JA (B11/A11, D12/D13, B18/A18) and JD
(F4/F3, E2/D2, H2/G2 on bank 35) also have true pairs. The Arty's Pi hosts
are reached like the NeTV2 hosts; see the fpgas.online docs for FTDI JTAG.

## 5. Electrical adaptation for real USB (set-ups 3–5)

USB HS is 400 mV differential, current mode, into 45 Ω to ground at both
ends; FS is 3.3 V CMOS levels with a 1.5 kΩ pull-up on D+ and 15 kΩ
pull-downs at the host. A 3.3 V HR bank can approximate both with a passive
network per line:

- **Receive**: D+/D- into an `IBUFDS` for HS data and chirp detection, and
  into two `LVCMOS33` single-ended inputs for FS line state. This needs the
  lines fanned out to three FPGA pins each side, or a differential pair plus
  two single-ended taps.
- **Common mode (hard requirement)**: HS and chirp signalling sit at 0–400 mV
  (common mode ≈200 mV, chirp ≈800 mV single-ended) but a 7-series `LVDS_25`
  receiver needs V_ICM ≥ 0.3 V (DS181 Table 10) and `TMDS_33` needs
  2.7–3.23 V. The network must shift the common mode into the receiver's
  window: either AC-couple the differential tap and bias it (fine for HS
  packets, loses DC chirp levels unless the bias path is slow enough) or a
  resistive level shift from 3.3 V. Without this the differential receiver's
  output is undefined and no set-up 3/4/5 result is meaningful.
- **HS termination**: an FPGA output driving low through 45 Ω is a 45 Ω
  termination to ground; tri-stating it removes the termination for FS mode.
  One pin per line (`term_en`).
- **HS transmit**: drive through a series resistor sized so that the 3.3 V
  swing into the doubled 45 Ω load (22.5 Ω) gives ≈400 mV; ≈150 Ω. Chirp K/J
  (device drives ≈800 mV into the host's 45 Ω with its own termination off)
  comes out of the same network at a different amplitude; hosts tolerate a
  wide range.
- **FS transmit**: drive D+/D- directly from `LVCMOS33` outputs through 27 Ω
  series resistors; tri-state to release.
- **Pull-up**: 1.5 kΩ from D+ to an FPGA pin driven high (`pullup_en`).

This is a starting point, not a finished design: the P5 hardware tests are
where the values get tuned. Record measured eye/BER results here.

## 6. Pin requirements for a purpose-built board

Per USB port the PHY wants, on one 3.3 V HR bank:

| Signal | Pins | Notes |
|--------|------|-------|
| `d_diff` | 1 true P/N pair | HS receive; prefer a clock-capable pair (SRCC/MRCC) so BUFIO/BUFR remain an option |
| `dp_se`, `dm_se` | 2 single-ended | FS line state (`LVCMOS33`) |
| `tx_p`, `tx_n` | 1 P/N pair or 2 single-ended | through the §5 network |
| `term_en` | 1–2 | HS termination control |
| `pullup_en` | 1 | 1.5 kΩ D+ pull-up |
| `vbus_det` | 1 | optional, divided VBUS |

Keep all pins of one port in the same bank; two ISERDESE2 on the P/N pair
need nothing else. The 300 MHz reference for IDELAYCTRL is generated on
chip; every bank that hosts a sampler needs its own IDELAYCTRL.
