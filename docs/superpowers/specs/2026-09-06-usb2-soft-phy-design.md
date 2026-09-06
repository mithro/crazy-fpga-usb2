# USB 2.0 High-Speed soft PHY on Artix-7 SelectIO — design

Date: 2026-09-06
Status: draft for review
Repository: `mithro/crazy-fpga-usb2` (private)

## 1. Goal

Recover and transmit USB 2.0 high-speed (480 Mbit/s) signalling using only the
ordinary SelectIO pins of a Xilinx 7-series FPGA, with no external USB PHY, and
present the result to the LUNA USB stack as a drop-in UTMI-style PHY.

The starting point is LUNA's pure-gateware full-speed PHY
(`luna/gateware/interface/gateware_phy/`), which 4x-oversamples a 12 Mbit/s
signal at 48 MHz. This project builds the same pipeline "much faster": a
480 Mbit/s signal sampled 3x or 4x through ISERDESE2 primitives, with all
per-bit logic done word-parallel at FPGA fabric speeds. The precedent that
makes this credible is the NeTV2, whose Artix-7 HR banks already capture
1.485 Gbit/s HDMI on these same pins.

Constraints given by the user:

- Native Amaranth HDL. Vendor primitives are instantiated through Amaranth
  `Instance`; no VHDL; no large Verilog.
- Minimal resource usage, measured by Vivado utilisation reports.
- Careful clocking, PLL/MMCM use and clock-domain crossing, following LiteX
  patterns (LiteEth/LitePCIe/LiteVideo/LiteDRAM).
- Comprehensive low-level test benches showing lock and tracking under drift.
- Vivado first, then Yosys-synth + Vivado P&R, then full openXC7.
- Real hardware validation on the NeTV2 boards at Welland, and documentation
  of several hardware set-ups that can validate the work.
- Small commits, own branches/worktrees, private GitHub repo per `GitHub.md`,
  project layout modelled on Greg Davill's LUNA/ButterStick work, no upstream
  patches or issues.

## 2. Decomposition

This is several sub-projects. Each gets its own implementation plan and is
delivered in order; later ones depend on earlier ones.

| # | Sub-project | Deliverable |
|---|-------------|-------------|
| P0 | Repository bootstrap | GitHub repo, `uv` project, LUNA dependency, CLAUDE.md, build/program/test scaffolding, CI-free but scripted flows |
| P1 | Hardware bring-up | "hello" bitstream (LEDs + UART banner + board ID) built with Vivado and loaded on a NeTV2 over GPIO JTAG; HDMI link-discovery bitstream that maps which HDMI ports are cross-connected |
| P2 | RX path in simulation | sampler front-end model, oversampling CDR, word-parallel NRZI/unstuff/framer, byte packer; Python USB-HS wire model; drift/jitter test benches |
| P3 | TX path in simulation | byte-to-bit encoder (SYNC, stuffing, NRZI, EOP), elastic gearbox, OSERDES driver; loop tests TX→wire→RX |
| P4 | HDMI link on hardware | RX+TX on NeTV2 HDMI pairs: BER/ping-pong soak between two boards with independent crystals; measured utilisation |
| P5 | Virtual UTMI PHY + LUNA | `SoftUTMIPHY` with line-state, chirp, op-modes; LUNA `USBDevice` integration; full-stack simulation with a Python HS host model; hardware device test between two boards |
| P6 | TX clock discipline | frequency-error estimator from CDR slips + MMCM fine-phase-shift DPLL slaving the local clock tree to the host; sim + hardware |
| P7 | Open toolchains | Yosys→Vivado hybrid flow, then openXC7 (`Xray`) flow; fix primitive gaps; hardware re-validation |
| P8 | Optimisation | parallel review agents hunt resource reductions; measured before/after on Vivado |

P1 is scheduled early because it de-risks everything hardware-related (JTAG
access, UART, clocking, HDMI pins) while it can run in parallel with P2/P3
development.

## 3. Physical channel and electrical reality

USB high speed is a 400 mV differential, current-mode signal into 45 Ω
terminations to ground at both ends. The FPGA cannot produce or terminate that
natively; the electrical adaptation is board-level work, so the design is split
so that everything above the pins is validated independently of it.

Three physical set-ups are supported by the same gateware, selected by
platform:

1. **NeTV2 HDMI cross-link (available now).** HDMI TX pairs (OBUFTDS,
   `TMDS_33`, bank 14/16) of one board to HDMI RX pairs (IBUFDS, `TMDS_33`,
   bank 15/16, with the board's 50 Ω pull-ups) of another. This is a genuine
   480 Mbit/s NRZI differential channel between two FPGAs with independent
   crystals, so it exercises CDR, framing, stuffing, drift and squelch
   behaviour. Tri-stating the TMDS driver leaves the receiver with a
   zero-differential input, which reproduces the HS idle (squelch) problem
   faithfully. It cannot exercise full-speed line states or chirp.
2. **NeTV2 direct USB via the PCIe "hax" pins.** Bunnie's `netv2mvp-usb3-v1`
   add-on routes a USB connector's D-/D+ onto the PCIe SMBus pair, which lands
   on FPGA balls **D19/E19** (`IO_L14N/P_T2_SRCC_16`, a true pair). luna-boards'
   NeTV2 platform instead uses hax pins A15/A14 with a pull-up on C17. Either
   way the pins are 3.3 V HR I/O with no HS termination, so this set-up needs
   the resistor network described in `docs/hardware-setups.md` and gives
   full-speed line states plus an approximation of HS levels.
3. **Arty A7 PMOD USB breakout with a Raspberry Pi as host.** Welland has Arty
   A7 boards on Pis with PMOD HATs; a USB breakout on a PMOD gives a real USB
   host (the Pi) talking to the FPGA. Same resistor-network requirements.

For a future "proper" board the document specifies the pin set the PHY wants:
one differential input pair (D+/D-), two single-ended inputs (D+, D-) for
full-speed line state, one differential or two single-ended outputs through a
resistor network able to produce both 3.3 V FS levels and ~400 mV HS levels
into 45 Ω, and a pull-up enable. These are recorded as requirements, not
designed here.

## 4. Architecture

```
                 pins                 rx_io / rx_div domains        rx_cdr 120 MHz            usb 60 MHz
 D+/D- ──IBUFDS_DIFF_OUT──► ISERDESE2 ×2 ──► 16 samples/cycle ──► OversamplingCDR ──► 2:1 gear ──► RxDecoder ──► UTMI rx_*
                                        (4 samples per UI)         (3..5 bits/cycle)            (NRZI, unstuff, SYNC/EOP, bytes)

 UTMI tx_* ──► TxEncoder ──► elastic 8-bit gearbox ──► OSERDESE2 8:1 DDR (240 MHz) ──► OBUFTDS ──► D+/D-
           (SYNC, stuff, NRZI, EOP)     usb 60 MHz                  tx_io 240 MHz

 D+, D- single-ended ──► LineStateMonitor (usb 60 MHz) ──► UTMI line_state; chirp/reset per UTMI op_mode/term_select

 CDR slip statistics ──► FrequencyEstimator ──► MMCM PSEN/PSINCDEC (TxClockDiscipline, P6)
```

### 4.1 Sampler front-end (`usb2soft.rx.sampler`)

Default: **4x oversampling with two ISERDESE2 per pair.** `IBUFDS_DIFF_OUT`
feeds the P-side ILOGIC with `O` and the N-side ILOGIC with `OB` (inverted in
fabric). Both ISERDESE2 run `NETWORKING`, `DDR`, `DATA_WIDTH=8`, CLK = 480 MHz
and CLKDIV = 120 MHz. The second ISERDES gets a 480 MHz clock shifted by 90°,
so the interleaved streams sample at 1920 MS/s: 16 samples per 120 MHz cycle,
4 per UI. All clocks are BUFG-driven (480 MHz is within the -2 BUFG limit of
628 MHz), which removes clock-region constraints and the BUFIO/BUFR
primitives that nextpnr-xilinx lacks.

Fallback variants, selectable by parameter, share the same downstream logic:

- 4x, single clock phase, second path offset by IDELAYE2 (~7 taps ≈ 0.52 ns)
  with IDELAYCTRL on a 200 MHz reference. Used if the two-phase ISERDES
  clocking fails timing analysis.
- 3x, one ISERDESE2, DDR 6:1, CLK = 720 MHz on BUFIO, CLKDIV = 240 MHz,
  followed by a 2:1 gearbox to 120 MHz (12 samples/cycle). Fewest resources
  but 720 MHz exceeds the -2 BUFIO limit (710 MHz); Vivado-only. Built and
  measured for the resource comparison the user asked for.
- 3x, two ISERDESE2, DDR 6:1, CLK = 360 MHz 0°/90°, CLKDIV = 120 MHz
  (12 samples/cycle). In-spec 3x.

The sampler also exposes the raw sample word for a debug tap.

### 4.2 Oversampling CDR (`usb2soft.rx.cdr`)

Generic over `S` samples per UI (3 or 4) and `W` samples per cycle (`W` a
multiple of `S`). State: pick phase `φ ∈ [0, S)`; the picks in a cycle are at
`φ, φ+S, φ+2S, …`. Each cycle:

1. Edge vector `e[i] = s[i] ^ s[i-1]` including the previous cycle's last
   sample.
2. Each edge votes on where the ideal pick is (edge + S/2 samples later). The
   vote is the difference from the current pick grid modulo `S`, mapped to
   {-1, 0, +1} (for S=4 the ambiguous ±2 case abstains).
3. Votes feed a saturating up/down loop filter. In *acquire* mode (no packet
   active) the threshold is 1 so the SYNC's edge-every-bit pattern aligns the
   phase within a few bits. In *track* mode the threshold is higher (2–4,
   parameter) for jitter tolerance. At most one ±1 phase step per cycle.
4. If `φ` steps to `S`, this cycle yields `W/S − 1` bits and `φ ← 0`. If `φ`
   steps to `−1`, the cycle yields `W/S + 1` bits, the first from the previous
   cycle's last sample, and `φ ← S − 1`. Otherwise `W/S` bits. This is where
   host/local frequency offset is absorbed: ±500 ppm is one slip every ~2000
   bits.
5. Outputs: `bits[W/S+1]`, `count`, `activity` (an edge seen within the last N
   UI), `slip_up`/`slip_down` strobes (consumed by P6), `phase` (debug).

No analog squelch exists, so idle detection is digital: `activity` drops when
no edge is seen for 8 UI (longer than any legal run inside a packet, thanks to
bit stuffing), and the framer additionally requires a real SYNC before opening
a packet.

Correspondence with LUNA's FS pipeline: `RxClockDataRecovery` (4x sampler and
phase re-align on every transition) becomes sampler + `OversamplingCDR`
(phase pointer with a loop filter, because at 480 Mbit/s a single glitch must
not re-align the phase).

### 4.3 RX decoder (`usb2soft.rx.decoder`), `usb` 60 MHz domain

A 2:1 gearbox (synchronous, both clocks from the same MMCM) delivers up to 10
bits per 60 MHz cycle. Word-parallel stages, each carrying state across
cycles:

- **NRZI decode**: `d[i] = ~(b[i] ^ b[i-1])`, previous raw bit carried.
- **SYNC / packet detect**: HS SYNC is up to 32 bits (hubs may strip down to
  12): decoded as ≥12 zeros then a one. The bit after the one is bit 0 of the
  PID. Mirrors `RxPacketDetect`.
- **Bit unstuff**: running count of ones; after six ones the next bit is
  dropped. Seven consecutive ones inside a packet is the HS EOP (a deliberate
  stuffing violation), which closes the packet. Mirrors `RxBitstuffRemover`.
- **Byte packer**: variable-count shift-in (0–10 bits), byte-out when ≥8 are
  held, into a small elastic FIFO (16 bytes) that drains during inter-packet
  gaps. Needed because a faster host delivers slightly more than 8 bits per
  local 60 MHz cycle and UTMI allows at most one byte per cycle. Mirrors
  `RxShifter` plus the CDC FIFO, except no asynchronous crossing is needed:
  the host's clock offset is absorbed in the CDR, not in a FIFO.
- **UTMI outputs**: `rx_active` from SYNC until the EOP byte has drained,
  `rx_valid`/`rx_data` per byte, `rx_error` on a packet that ends unaligned.

### 4.4 TX path (`usb2soft.tx`)

- **TxEncoder** (usb domain): on `tx_valid` rising, emit the 32-bit SYNC, then
  bytes LSB-first with stuffing (a zero after six ones), NRZI, then EOP (eight
  ones; extended SOF EOP is a host-only concern). Mirrors
  `TxShifter`/`TxBitstuffer`/`TxNRZIEncoder`, word-parallel.
- **Elastic gearbox**: a bit accumulator that always hands exactly 8 raw bits
  per cycle to the serialiser and asserts `tx_ready` only when it can accept
  another byte; stuffing makes some bytes cost 9 bits, which is exactly when
  UTMI expects `tx_ready` to drop.
- **Serialiser**: OSERDESE2 `DDR`, `DATA_WIDTH=8`, `TRISTATE_WIDTH=1`, CLK =
  240 MHz, CLKDIV = 60 MHz, into OBUFTDS. Tristate is byte-granular, which is
  sufficient because EOP is exactly one byte. Idle = driver off.
- `op_mode = 2` (no NRZI/no stuffing, used for chirp) bypasses encoder and
  drives a constant level.

### 4.5 Line state, chirp and reset (`usb2soft.phy.linestate`)

Only meaningful with single-ended D+/D- inputs (set-ups 2 and 3). Two
FFSynchronizers plus a 2-of-3 filter at 60 MHz produce UTMI `line_state`
(SE0/J/K/SE1). In HS mode LUNA's reset sequencer drives `op_mode`,
`term_select` and `xcvr_select`; the PHY maps them to pull-up enable, HS
termination enable (a resistor network control pin) and chirp K/J drive. Host
chirp K/J (≈800 mV differential DC) is detected on the differential input via
the CDR's `activity`-less static level path. The HDMI-only platform hard-codes
`line_state` to J/HS-idle and reports "HS-only" so LUNA skips the chirp.

### 4.6 `SoftUTMIPHY` and LUNA integration (`usb2soft.phy`)

`SoftUTMIPHY` exposes the same attribute names LUNA's `GatewarePHY` does
(`tx_data/valid/ready`, `rx_data/valid/active/error`, `line_state`,
`vbus_valid`, `session_end`, `xcvr_select`, `term_select`, `op_mode`,
`dp_pulldown`, `dm_pulldown`). LUNA's `USBDevice` accepts such an object
directly but then assumes full-speed only (`always_fs=True`,
`data_clock=12e6`), so `usb2soft.luna.SoftPHYUSBDevice` subclasses
`USBDevice` and sets `always_fs=False`, `data_clock=60e6` — both are read only
during `elaborate`. No LUNA source changes.

### 4.7 Clocking (`usb2soft.clock`)

NeTV2 input is 50 MHz. Two cascaded primitives, following the luna-boards
NeTV2 and LiteVideo precedents:

- PLLE2_ADV: 50 MHz × 24 = 1200 MHz VCO → 60 MHz (÷20, `usb`) and 200 MHz
  (÷6, IDELAYCTRL reference when used).
- MMCME2_ADV fed by the 60 MHz: ×16 = 960 MHz VCO → 480 MHz at 0° and 90°
  (sampler CLKs), 240 MHz (TX OSERDES CLK), 120 MHz (`rx_cdr`, sampler
  CLKDIV), 60 MHz (`usb`; the PLL's 60 MHz is only the MMCM reference so all
  fabric domains are MMCM siblings with known phase).
- The 3x variants use ×24 = 1440 MHz VCO → 720/360/240/120/60.
- All MMCM outputs have `USE_FINE_PS=TRUE` so P6 can slew the entire clock
  tree together (see 4.8). Resets are `~locked` through `ResetSynchronizer`.
- Domains: `usb` (60), `rx_cdr` (120), `rx_io0`/`rx_io1` (480 0°/90°, or
  BUFIO variants), `tx_io` (240), `sync` (= `usb`, control/UART).
- One sampler per bank in the HDMI platform; the two-phase BUFG design has no
  clock-region coupling, so a second RX pair costs only two more ISERDES.

CDC rules: `usb`↔`rx_cdr` is a synchronous 2:1 gearbox; control/status to a
UART/CSR block uses `FFSynchronizer`/`PulseSynchronizer` from
`amaranth.lib.cdc`; any counter read across domains is Gray-coded or captured
via `BusSynchronizer`-style handshake. Reset of the sampler domains is held
until the MMCM locks and released synchronously.

### 4.8 TX clock discipline (P6, `usb2soft.clock.discipline`)

USB HS does not require a device to lock its transmit clock to the host (each
end is ±500 ppm), but the user asked for it and it matters for pass-through
designs. Design:

- `FrequencyEstimator`: counts CDR slip-up minus slip-down strobes over a
  window of received bits; the sign and magnitude are the local-vs-host
  frequency error.
- `MMCMPhaseSlewer`: drives PSEN/PSINCDEC/PSDONE to step every
  `USE_FINE_PS` output by 1/56 VCO period per request. Continuous stepping is a
  frequency offset. With VCO 960 MHz and PSCLK 120 MHz the pull range is about
  ±180 ppm (each step ≈18.6 ps, ≥12 PSCLK cycles per step); saturation is
  reported and the design keeps working un-disciplined because the CDR still
  absorbs the offset.
- Loop: a slow integral controller nulling the estimator; when locked, the CDR
  phase stays constant and TX bits are frequency-locked to the host.
- Simulation uses a behavioural MMCM model (phase accumulator) and the same
  wire model with a ramping frequency offset.

### 4.9 Debug and control

A minimal UART command/status block (`usb2soft.debug`) exposes counters
(packets, CRC/framing errors, slips, phase histogram), sampler raw taps and
test-pattern controls. No soft CPU in v1; the control loops are tiny FSMs. If
P6's controller or eye-scan features grow, a Minerva-based control CPU is the
documented escalation path (the user explicitly permits it), but YAGNI now.

## 5. Project layout

Modelled on Greg Davill's ButterStick-projects (`cli.py`, `applets/`,
`platform/`, `util/`) and LUNA's `applets/`.

```
crazy-fpga-usb2/
  pyproject.toml            uv project; deps: amaranth~=0.5.9, luna-usb @ gregdavill/luna@a28cf60, pytest
  CLAUDE.md                 working rules, hardware guidance, "hardware is never broken"
  README.md, LICENSE (Apache-2.0)
  usb2soft/
    __main__.py, cli.py     `uv run usb2soft build|program|test <applet> --platform ... --toolchain vivado|yosys-vivado|xray`
    applets/                hello_netv2, hdmi_discovery, hdmi_loopback_ber, luna_device, ...
    platforms/              netv2_hdmi.py, netv2_hax_usb.py, arty_pmod_usb.py (Amaranth platforms with clock generators)
    rx/  sampler.py cdr.py decoder.py
    tx/  encoder.py gearbox.py serializer.py
    phy/ utmi.py linestate.py
    clock/ plan.py mmcm.py discipline.py
    luna/ device.py
    debug/ uart_regs.py
    sim/ wire.py (USB-HS bit/sample model), host.py (Python HS host model)
    build/ vivado.py, yosys_vivado.py, xray.py (toolchain glue, report harvesting)
  tests/                    pytest + amaranth sim; one file per module, plus loop and full-stack tests
  host/                     Pi-side scripts: netv2_load.py (volatile JTAG load via openocd), console.py, run_remote.py
  docs/                     design.md, hardware-setups.md, clocking.md, testing.md, results/<toolchain>-<date>.md
  docs/superpowers/specs/, docs/superpowers/plans/
```

## 6. Testing strategy

Simulation (pytest, Amaranth simulator, run under `uv run pytest`):

- `sim/wire.py` produces sample streams from byte packets: SYNC length,
  stuffing, NRZI, EOP, inter-packet idle with configurable noise (random
  toggles, stuck level), frequency offset in ppm, linear frequency ramps,
  Gaussian and deterministic jitter, phase steps between packets. It also
  decodes sample streams back to bytes as the reference model.
- CDR tests: acquisition within the SYNC; zero bit errors over long random
  packets at 0, ±100, ±500, ±1000 ppm (S=3 and S=4); ramping offset; jitter
  sweeps to find the failure point and assert margin; idle-noise rejection.
- Decoder tests: SYNC lengths 12–32, byte alignment, unstuffing, EOP,
  back-to-back packets at the minimum HS inter-packet gap, elastic FIFO under
  +500 ppm with all-zero payloads, UTMI timing (`rx_valid` only while
  `rx_active`, data stable).
- TX tests: encoder output decoded by the reference model, `tx_ready` pattern
  under heavy stuffing, EOP byte alignment, tristate timing.
- Loop tests: TX → wire (resampled with offset) → RX for one and two PHYs.
- Full stack: LUNA `USBDevice` on `SoftUTMIPHY` against `sim/host.py`
  (SOF, SETUP/IN/OUT, handshakes, CRC5/16) driving enumeration at the bit
  level, at ±500 ppm.
- P6: MMCM behavioural model; loop converges and holds under a ramp.

Hardware (each recorded in `docs/results/`):

- P1: hello bitstream on each reachable NeTV2; HDMI discovery table.
- P4: two-board ping-pong and one-way BER soak (≥10^10 bits) over HDMI with
  independent crystals; phase/slip histograms; utilisation and timing reports.
- P5: LUNA device on one board, minimal host-lite packet scheduler on the
  other; later a real host through set-up 2 or 3.
- P7: same tests re-run from Yosys→Vivado and openXC7 bitstreams.

## 7. Toolchain flows

- **Vivado**: Amaranth `Xilinx7SeriesPlatform(toolchain="Vivado")`; build
  scripts add `report_utilization -hierarchical` and `report_timing_summary`,
  harvested into `docs/results/`.
- **Yosys → Vivado**: custom `toolchain_prepare` producing a Yosys
  `synth_xilinx` netlist (EDIF/Verilog) that a Vivado script links, places,
  routes and bitgens. Catches Yosys-side synthesis problems while keeping
  timing-driven P&R.
- **openXC7**: Amaranth toolchain `Xray` with `DB_DIR`, `CHIPDB_DIR` and the
  openXC7 0.8.2 wrappers already unpacked under the fpgas-online test-designs
  venv (to be copied/installed into this project's own environment). Missing
  primitives are worked around in the platform, never by rewriting the core.

## 8. Resource budget and measurement

Targets for the PHY alone on XC7A35T (4x, W=16), excluding LUNA and debug:
≤ 700 LUTs, ≤ 600 FFs, 2 ISERDESE2, 1 OSERDESE2, 1 MMCM, 1 PLL, 0 BRAM. Every
milestone records `report_utilization -hierarchical` per module; P8 uses those
numbers as the baseline for the optimisation review agents.

## 9. Risks and mitigations

| Risk | Mitigation |
|------|------------|
| ISERDES CLK at 90° from CLKDIV violates internal timing | IDELAY-offset variant; Vivado timing report is the arbiter |
| Zero-differential idle makes the comparator chatter | digital squelch = activity timeout + mandatory SYNC; tested with noisy-idle wire model and on HDMI with tri-stated TX |
| 720 MHz over-spec clocks (3x single) | default 4x/480 MHz plan; 3x variant is measurement-only |
| nextpnr-xilinx lacks BUFIO/BUFR | default plan uses BUFGs only |
| Host chirp/FS levels impossible on HDMI | HS-only platform mode; FS/chirp validated in simulation and on set-ups 2/3 |
| Board access: four cross-connected NeTV2s need `pi` key authorisation | flagged to user; P1 proceeds on rpi3-netv2/rpi5-netv2 (shared with other sessions; check `w` and recent files first, volatile loads only, never flash) |
| Two NeTV2 dev boards are different parts (35T vs 100T) | platform `variant` parameter; chipdbs exist for both |
| Machine memory pressure from other sessions | one Vivado run at a time, ≤2 sub-agents, builds under `build/` not `/tmp` |

## 10. Out of scope (v1)

USB low speed, host mode, hub repeater, SOF long-EOP generation, VBUS
sensing, suspend/resume power states beyond what LUNA already implements, a
soft CPU.
