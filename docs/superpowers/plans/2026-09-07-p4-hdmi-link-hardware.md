# P4 Link on Hardware Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The P2 receive path and P3 transmit path running in a real Artix-7 at 480 Mbit/s: the PHY clock plan (MMCM), the ISERDESE2 sampler front-end, the OSERDESE2 serialiser, and a `link-test` applet that soak-tests packets through either an internal digital loopback (available now on rpi5-netv2) or the HDMI TX0 → RX pair (as soon as a cable or the cross-connected boards are available), reporting counters over the UART. First measured resource usage and timing of the PHY datapath.

**Architecture:** `NeTV2PhyClocks` cascades the existing 50→60 MHz PLL with an MMCM (60 MHz × 16 = 960 MHz VCO → 480 `rx_io`, 240 `tx_io`, 120 `rx_cdr`/`tx_cdr`, 60 `usb`) and drives the 300 MHz IDELAYCTRL reference from the PLL. `Sampler` is IBUFDS_DIFF_OUT + two IDELAYE2 + two ISERDESE2 (NETWORKING, DDR 8:1, common CLK/CLKDIV) whose interleaved outputs form the 16-sample word. `Serializer` is OSERDESE2 4:1 DDR with `TRISTATE_WIDTH=4` into OBUFTDS. `LinkTest` wires `TxPath` and `RxPath` to a packet generator/checker with a loopback multiplexer: *internal* (TX 4-bit words expanded to 16 samples with a programmable phase offset and an injected ±ppm slip, fed straight to the CDR) or *external* (pins). Status lines via the existing `Console`/`TextReporter`.

**Tech Stack:** Amaranth 0.5.9, Vivado 2025.2, OpenOCD on rpi5-netv2, the P0 host tooling.

Branch: `p4-link-hw` from `main` after PR #3 (P3) merges. Hardware rules per CLAUDE.md (announce loads, volatile only).

---

## File structure

| File | Responsibility |
|------|----------------|
| `usb2soft/clock/netv2.py` | add `NeTV2PhyClocks` (PLL + MMCM, all domains, `USE_FINE_PS=TRUE` outputs; PSCLK tied to `tx_io` but PSEN held 0 until P6) |
| `usb2soft/io/sampler.py` | `Sampler` (IBUFDS_DIFF_OUT, 2× IDELAYE2 VAR_LOAD, 2× ISERDESE2, interleave; `delay_tap` input) and `IdelayCtrl` |
| `usb2soft/io/serializer.py` | `Serializer` (OSERDESE2 4:1 DDR, TRISTATE_WIDTH 4, OBUFTDS) |
| `usb2soft/sim/expander.py` | `SampleExpander`: 4 line bits + 4 oe → 16 samples with configurable sub-sample phase and periodic ±1-sample slip (the internal loopback "wire"); simulable |
| `usb2soft/applets/link_test.py` | `LinkTestCore` (generator, checker, counters, mode switch) + `LinkTest` applet with `--mode internal|hdmi` |
| `tests/test_phy_clocks.py`, `tests/test_sampler.py`, `tests/test_serializer.py` | RTLIL checks: primitives, parameters (DATA_WIDTH, INTERFACE_TYPE, IDELAY_TYPE, REFCLK_FREQUENCY=300.0, TRISTATE_WIDTH, DATA_RATE_TQ), clock/domain wiring |
| `tests/test_expander.py`, `tests/test_link_test.py` | expander bit-exactness; LinkTestCore in internal mode: N packets sent == received, zero errors, at +/-slip |
| `docs/results/2026-09-XX-p4-link-hardware.md` | utilisation (hierarchical, PHY only), timing, internal-loopback soak counters from rpi5-netv2, external status |

---

### Task 1: PHY clock plan

`NeTV2PhyClocks`: PLL (existing solution, 1200 MHz VCO → 60 MHz, 300 MHz) then `MMCME2(solve(kind="mmcm", speed=..., fin=60e6, vco=960e6, outputs={"rx_io": 480e6, "tx_io": 240e6, "cdr": 120e6, "usb": 60e6}), fine_ps=True, ps_clk=<tx_io BUFG>)`; domains `rx_io`, `tx_io`, `rx_cdr`, `tx_cdr` (both = 120 MHz output), `usb`, `sync`(=usb), `idelay_ref` (300 MHz from the PLL); resets via `ResetSynchronizer(~(pll.locked & mmcm.locked))`. MMCM reset is `~pll.locked`. Attach domains at the top as in P1. Test: RTLIL contains one PLLE2_ADV and one MMCME2_ADV with `CLKFBOUT_MULT_F 16.0`, `CLKOUT0_DIVIDE_F 2.0`, `USE_FINE_PS "TRUE"`, and `NeTV2Platform.build(do_build=False)` succeeds with all domains used.

### Task 2: Sampler front-end

`Sampler(port: io.DifferentialPort, *, io_domain="rx_io", div_domain="rx_cdr", ref_domain="idelay_ref", delay_taps=10)`:

- `IBUFDS_DIFF_OUT(I=port.p, IB=port.n) → o, ob`; apply `port.invert` in fabric.
- Two `IDELAYE2(IDELAY_TYPE="VAR_LOAD", DELAY_SRC="IDATAIN", REFCLK_FREQUENCY=300.0, SIGNAL_PATTERN="DATA", HIGH_PERFORMANCE_MODE="TRUE", CINVCTRL_SEL="FALSE", PIPE_SEL="FALSE", IDELAY_VALUE=0)` with `CNTVALUEIN` = 0 for the `o` path and `delay_taps` for the `~ob` path, `LD` pulsed once after reset (and whenever `delay_taps` changes), `C` = div clock, `CE=INC=0`, `LDPIPEEN=0`, `REGRST=0`.
- Two `ISERDESE2(DATA_RATE="DDR", DATA_WIDTH=8, INTERFACE_TYPE="NETWORKING", IOBDELAY="IFD", NUM_CE=1, SERDES_MODE="MASTER", OFB_USED="FALSE", DYN_CLKDIV_INV_EN="FALSE", DYN_CLK_INV_EN="FALSE")` with `DDLY` from the IDELAYs, `CLK`=io clock, `CLKB`=~io clock, `CLKDIV`=div clock, `CE1=1`, `RST`=div-domain reset, `BITSLIP=0`, OCLK/OCLKB/SHIFTIN/D/OFB tied 0.
- Interleave: with the second path *delayed* by half a sample period, at a given clock edge it captures the signal from earlier, so the word is `[s0, m0, s1, m1, …]` where `m` = main (`o`) and `s` = slave (`ob`) samples; ISERDES Q8 is the oldest bit (LiteVideo mapping `o_Q8=d[0] … o_Q1=d[7]`). Both orderings are parameters (`q_reversed`, `slave_first`) with these defaults; the hardware run in Task 6 verifies them (the internal loopback cannot).
- `IdelayCtrl(ref_domain)`: `IDELAYCTRL(REFCLK=ClockSignal(ref), RST=ResetSignal(ref))`, `rdy` output. One per bank; the applet instantiates it.
- Test: RTLIL has 2× ISERDESE2 / 2× IDELAYE2 / IBUFDS_DIFF_OUT with the parameters above; `words` output is 16 bits.

### Task 3: Serialiser

`Serializer(port: io.DifferentialPort, *, io_domain="tx_io", div_domain="tx_cdr")`: `OSERDESE2(DATA_RATE_OQ="DDR", DATA_RATE_TQ="DDR", DATA_WIDTH=4, TRISTATE_WIDTH=4, SERDES_MODE="MASTER", TBYTE_CTL="FALSE", TBYTE_SRC="FALSE")`, `D1..D4` = line bits (D1 first on the wire), `T1..T4` = ~oe bits, `CLK`/`CLKDIV`, `OCE=1`, `TCE=1`, `RST`, → `OQ`/`TQ` → `OBUFTDS(I=OQ, T=TQ, O=port.p, OB=port.n)`; `port.invert` applied to the data. Test: RTLIL parameters; port order.

### Task 4: Sample expander and LinkTestCore

`SampleExpander(phase_shift=0..3, slip_period=None)`: from `line[4]`, `oe[4]` (idle → level 1) produce 16 samples (each bit ×4), rotated by `phase_shift` samples across cycles (keeps a 3-sample carry), and every `slip_period` cycles drop or duplicate one sample (emulating ±ppm; e.g. period 250 cycles ≈ ±250 ppm). Simulable and synthesisable (it is part of the internal loopback in the bitstream).

`LinkTestCore(mode_internal: Signal)`: `TxPath`, `RxPath`, `SampleExpander`, generator (LFSR payload, lengths cycling 8/64/512, sequence number in the first two bytes, CRC16 as LUNA computes it — reuse `luna.gateware.usb.usb2.packet`'s CRC or a local one), checker (sequence continuity, CRC, length), counters: `tx_packets`, `rx_packets`, `rx_bad`, `rx_errors` (rx_error pulses), `seq_gaps`, CDR `slips_up/down`, `cdr_phase`. Report line every second via `Console`: `L <tx> <rx> <bad> <err> <gaps> <slipup> <slipdn> <phase>\r\n` (hex). Mode is a constant per applet build (`--mode`), plus a `--hdmi-rx 0|1` choice; in HDMI mode the receiver comes from `Sampler`, the transmitter goes to `Serializer` on TX0 lane 0 (`d0`), and the other 7 TX lanes idle.

Tests: expander bit-exactness vs `usbhs` sampling of the same bits; `LinkTestCore` in internal mode with slip periods `None`, `+400`, `−400`: after N seconds of simulated time (scaled down: generator period parameter) `rx_packets == tx_packets`, `rx_bad == 0`, `seq_gaps == 0`.

### Task 5: Applet, Vivado build, utilisation

`link-test` applet (`usb2soft/applets/link_test.py`): clocks, IDELAYCTRL, LinkTestCore, UART console, LEDs (lock, activity, error sticky). Build both modes for a7-100 (rpi5-netv2) and a7-35. Record from `top_utilization_hierarchical_place.rpt` the LUT/FF/carry counts of `core/tx`, `core/rx` (cdr, decoder, bridge), `sampler`, `serializer` separately, and WNS per clock (480/240/120/60). Expect the PHY datapath (tx+rx) within the spec's ≤700 LUT / ≤600 FF budget; if not, note the gap for P8. Timing must close at 480/240/120 MHz; if the CDR fails at 120 MHz, add a pipeline register between vote counting and the pick mux (the plan for that is one extra cycle of latency).

### Task 6: Hardware

1. Internal loopback on rpi5-netv2 (announce first): `link-test --mode internal` bitstream, capture 30 s of report lines, expect `rx == tx`, `bad == 0`, `err == 0`, slips consistent with the expander's slip period. This proves the datapath at real clock rates in silicon (CDR at 120 MHz, encoder, FIFOs, MMCM).
2. External HDMI mode: build and load `--mode hdmi --hdmi-rx 0`; without a cable the report shows `rx == 0`. Leave documented steps for the moment a TX0→RX0 cable exists on rpi5-netv2 or the cross-connected boards are reachable: run the same bitstream, verify the Q-order/slave-first parameters via a lane-order self-check (a known SYNC pattern must decode; if not, flip `q_reversed` / `slave_first` and rebuild), then soak for ≥10 minutes and record BER-equivalent counters.
3. Results page + `docs/hardware-setups.md` update.

### Task 7: Review and PR

Code review, fixes, PR "P4: PHY front-end, link test applet, internal loopback on hardware".
