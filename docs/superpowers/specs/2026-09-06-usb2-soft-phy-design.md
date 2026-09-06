# USB 2.0 High-Speed soft PHY on Artix-7 SelectIO — design

Date: 2026-09-06
Status: revision 6 (2026-09-07): P2 (§4.2 vote dead zone, §4.3 120 MHz decoder + event FIFO) and P3 (§4.4 4:1 serialiser with per-bit tristate) as built and measured
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
   `TMDS_33`, TX0 bank 14 / TX1 bank 16) of one board to HDMI RX pairs
   (IBUFDS, `TMDS_33`, RX0 bank 15 / RX1 bank 14, with the board's 50 Ω
   pull-ups) of another. This is a genuine
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
   host (the Pi) talking to the FPGA. Same resistor-network requirements. The
   Arty A7-35T is an `xc7a35ticsg324-1L` (speed grade -1), whose BUFG
   (464 MHz) and MMCM VCO (1200 MHz) limits rule out the 480 MHz default clock
   plan; it uses the in-spec 3x plan in §4.7.

Receiver common mode matters for set-ups 2 and 3: HS signalling swings
0–400 mV (common mode ≈200 mV) while a 7-series `LVDS_25` input needs
V_ICM ≥ 0.3 V and `TMDS_33` needs 2.7–3.23 V (DS181 Table 10). The resistor
network must therefore also shift the common mode into the receiver's range
(AC coupling with a bias, or a resistive level shift); this is recorded in
`docs/hardware-setups.md` as a hard requirement.

For a future "proper" board the document specifies the pin set the PHY wants:
one differential input pair (D+/D-) with a common-mode-compatible network,
two single-ended inputs (D+, D-) for full-speed line state, one differential or
two single-ended outputs through a resistor network able to produce both 3.3 V
FS levels and ~400 mV HS levels into 45 Ω, and a pull-up enable. These are
recorded as requirements, not designed here.

## 4. Architecture

```
                 pins                 rx_io / rx_div domains        rx_cdr 120 MHz            usb 60 MHz
 D+/D- ──IBUFDS_DIFF_OUT──► ISERDESE2 ×2 ──► 16 samples/cycle ──► OversamplingCDR ──► PacketDecoder ──► event FIFO ──► UTMI rx_*
                                        (4 samples per UI)         (3..5 bits/cycle)   (120 MHz: NRZI, SYNC,   (rx_cdr → usb)
                                                                                        unstuff, EOP, bytes)

 UTMI tx_* ──► byte FIFO ──► PacketEncoder (4 bits + 4 oe / 120 MHz) ──► OSERDESE2 4:1 DDR, T 4:1 (240 MHz) ──► OBUFTDS ──► D+/D-
           usb 60 MHz              (SYNC, stuff, NRZI, EOP)                          tx_io 240 MHz

 D+, D- single-ended ──► LineStateMonitor (usb 60 MHz) ──► UTMI line_state; chirp/reset per UTMI op_mode/term_select

 CDR slip statistics ──► FrequencyEstimator ──► MMCM PSEN/PSINCDEC (TxClockDiscipline, P6)
```

### 4.1 Sampler front-end (`usb2soft.rx.sampler`)

Default: **4x oversampling with two ISERDESE2 per pair, offset by IDELAY**
(the XAPP523 arrangement). `IBUFDS_DIFF_OUT` feeds the P-side ILOGIC with `O`
and the N-side ILOGIC with `OB` (inverted in fabric). Both ISERDESE2 run
`NETWORKING`, `DDR`, `DATA_WIDTH=8`, with the *same* CLK = 480 MHz and
CLKDIV = 120 MHz, so UG471's rule that NETWORKING-mode CLK and CLKDIV be
phase aligned is met trivially. Both paths pass through an IDELAYE2
(`VAR_LOAD`, 300 MHz IDELAYCTRL reference, 52 ps taps) so their insertion
delays match; the `O` path sits at tap 0 and the `OB` path ≈10 taps later
(≈0.52 ns, half a sample period; any odd multiple of half a sample also works
and the P4 eye scan may pick one). The interleaved streams then sample at
1920 MS/s: 16 samples per 120 MHz cycle, 4 per UI. All clocks are
BUFG-driven (480 MHz is within the -2 BUFG limit of 628 MHz), which removes
clock-region constraints and the BUFIO/BUFR primitives that nextpnr-xilinx
lacks. The run-time-loadable delay is also the hook for later software eye
alignment.

Fallback variants, selectable by parameter, share the same downstream logic:

- 4x, two clock phases instead of IDELAY: ISERDES #2 gets CLK at 90° *and its
  own CLKDIV at the matching 22.5° offset* (both from the MMCM; 5.625° phase
  resolution at ÷8 makes 22.5° exact), so each ISERDES keeps aligned
  CLK/CLKDIV and the two words are combined in fabric as a related-clock path
  with a ≈7.8 ns budget. Costs one MMCM output and two BUFGs more, no
  IDELAYCTRL. Used if IDELAY tap drift or openXC7 IDELAY support becomes a
  problem.
- 3x, one ISERDESE2, DDR 6:1, CLK = 720 MHz on BUFIO, CLKDIV = 240 MHz
  (BUFR ÷3), followed by a 2:1 gearbox to 120 MHz (12 samples/cycle). Fewest
  resources but 720 MHz exceeds the -2 BUFIO limit (680 MHz, DS181 Table 33);
  Vivado-only. Built and measured for the resource comparison the user asked
  for.
- 3x, two ISERDESE2, DDR 6:1, CLK = 360 MHz BUFG, IDELAY offset ≈0.69 ns
  (13 taps), CLKDIV = 120 MHz (12 samples/cycle). In spec on -2 **and -1**
  (BUFG ≤464 MHz, VCO 720 MHz); this is the plan for the Arty A7 (-1L).

The sampler also exposes the raw sample word for a debug tap.

### 4.2 Oversampling CDR (`usb2soft.rx.cdr`)

Generic over `S` samples per UI (3 or 4) and `W` samples per cycle (`W` a
multiple of `S`). State: pick phase `φ ∈ [0, S)`; the picks in a cycle are at
`φ, φ+S, φ+2S, …`. Each cycle:

1. Edge vector `e[i] = s[i] ^ s[i-1]` including the previous cycle's last
   sample.
2. Each edge votes on where the ideal pick is (edge + S/2 samples later). The
   vote is the difference from the current pick grid modulo `S`. For S=3:
   +1 → step later, 2 → step earlier. For S=4 (revision 5, measured in P2):
   differences 0 and 1 are a dead zone, 2 → step later, 3 → step earlier.
   With jitter the loop then settles with the mean edge *on* a sample
   instant, centring the pick 0.5 UI after it with symmetric margins; voting
   on difference 1 as well centred the mean edge mid-slot and cost a third of
   the jitter margin (knee 0.06 instead of 0.12 UI rms).
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

### 4.3 RX decoder (`usb2soft.rx.decoder`), `rx_cdr` 120 MHz domain

The decoder consumes the CDR's words directly (≤5 bits per 120 MHz cycle);
bytes and packet events then cross into the 60 MHz `usb` domain through a
16-entry `AsyncFIFOBuffered` (`usb2soft.rx.bridge.RxUTMIBridge`), which is
also the elastic buffer. (Revision 5: the earlier plan of a 2:1 gearbox and a
10-bit-wide 60 MHz decoder was dropped in P2 — prefix logic over 5 bits is
about a quarter of the size, 120 MHz is comfortable on Artix-7 -2, and the
async FIFO removes any assumption about the `rx_cdr`/`usb` phase.)
Word-parallel stages, each carrying state across cycles:

- **NRZI decode**: `d[i] = ~(b[i] ^ b[i-1])`, previous raw bit carried.
- **SYNC / packet detect**: HS SYNC is 32 bits (KJKJ…KK), of which up to 20
  may be stripped by five hubs, leaving 12 bits = 11 decoded zeros then a
  one, and the CDR consumes a bit or two acquiring. The detector therefore
  requires **≥8 decoded zeros followed by a one**; the bit after the one is
  bit 0 of the PID. Mirrors `RxPacketDetect` (which needs only 5 of FS's 7
  zeros).
- **Bit unstuff**: running count of ones; after six ones the next bit is
  dropped. Seven consecutive ones inside a packet is the HS EOP (a deliberate
  stuffing violation), which closes the packet. The EOP is the NRZ byte
  `01111111` (a zero, then seven ones, no stuffing), so at the violation the
  packer must hold exactly 7 bits since the last byte boundary; anything else
  is `rx_error`. Mirrors `RxBitstuffRemover`.
- **Byte packer**: variable-count shift-in (0–5 bits), byte-out when 8 are
  held; bytes and START/END/ERROR events go into the 16-entry async FIFO that
  drains during inter-packet gaps. Needed because a faster host delivers
  slightly more than 8 bits per local 60 MHz cycle and UTMI allows at most one
  byte per cycle. Mirrors `RxShifter` plus the CDC FIFO; the host's clock
  offset itself is absorbed in the CDR.
- **UTMI outputs**: `rx_active` from SYNC until the EOP byte has drained,
  `rx_valid`/`rx_data` per byte, `rx_error` on a packet that ends unaligned.
- **Turnaround budget**: a HS device must start its response within 192 bit
  times (400 ns, 24 `usb` cycles) of the last received bit. The plan must
  account RX pipeline + elastic drain + LUNA's response + TX SYNC start +
  OSERDES latency against that figure; LUNA's `USBInterpacketTimer` assumes
  ULPI-PHY-like latencies, so the PHY's RX-to-`rx_active`-low and
  `tx_valid`-to-first-bit latencies are measured in simulation and recorded.

### 4.4 TX path (`usb2soft.tx`)

- **TxEncoder** (`tx_cdr` 120 MHz domain, 4 bits per cycle): on the first byte, emit the 32-bit SYNC, then
  bytes LSB-first with stuffing (a zero after six ones), NRZI, then the EOP
  byte `01111111` in NRZ (a zero forcing one transition, then seven ones with
  stuffing disabled, so the violation lands byte-aligned regardless of how
  the CRC ended; the 40-bit SOF EOP is host-only). Mirrors
  `TxShifter`/`TxBitstuffer`/`TxNRZIEncoder`, word-parallel.
- **Elastic gearbox**: a bit accumulator that always hands exactly 8 raw bits
  per cycle to the serialiser and asserts `tx_ready` only when it can accept
  another byte; stuffing makes some bytes cost 9 bits, which is exactly when
  UTMI expects `tx_ready` to drop.
- **Serialiser** (revision 6, from P3): OSERDESE2 `DDR`, `DATA_WIDTH=4`,
  `TRISTATE_WIDTH=4`, `DATA_RATE_TQ="DDR"`, CLK = 240 MHz, CLKDIV = 120 MHz,
  into OBUFTDS. 4:1 is the only OSERDESE2 configuration with a per-bit
  tristate word (8:1 forces `TRISTATE_WIDTH=1`, byte-granular, which would
  dribble up to seven bits after the EOP), so the encoder produces 4 line bits
  plus 4 output-enable bits per 120 MHz cycle and the driver turns off on the
  exact bit after the EOP; T rides through the same serialiser as the data,
  so no fabric delay matching is needed. Bytes reach the encoder from the
  `usb` domain through a depth-4 async FIFO so `tx_ready` still tracks the
  line rate. Idle = driver off.
- `op_mode = 2` (no NRZI/no stuffing, used for chirp) bypasses the encoder and
  drives the level given by `tx_data[0]` while `tx_valid` (0 → K, as LUNA's
  reset sequencer expects).

### 4.5 Line state, chirp and reset (`usb2soft.phy.linestate`)

LUNA has no "high-speed only" mode: its reset sequencer reaches HS only by
walking the real sequence (SE0 ≥ 2.5 µs → device chirp K → three host K/J
pairs each ≥ 2.5 µs → `IS_HIGH_SPEED`), drops back to FS if `line_state`
reads SE0 for 3 ms while in HS, and needs `session_end = 0` to leave bus
reset. The PHY therefore produces a UTMI-correct `line_state` in every mode:

- **FS mode** (set-ups 2/3, single-ended inputs): two FFSynchronizers plus a
  2-of-3 filter at 60 MHz give SE0/J/K/SE1 from the 3.3 V levels.
- **Chirp**: host chirp K/J are ≈800 mV differential DC, below LVCMOS33 V_IH,
  so while `xcvr_select` selects HS and `term_select` is in chirp mode,
  `line_state` is taken from the differential receiver's static level
  (K = 0b10, J = 0b01). Device chirp K is driven through `op_mode = 2`.
- **HS mode**: HS traffic is also below V_IH, so `line_state` is
  squelch-derived as in a real UTMI PHY: SE0 while the CDR reports no
  activity, J/K (from the differential level) while a packet is on the wire.
  A genuine HS reset (SE0 > 3 ms) is then detected by LUNA exactly as with a
  hardware PHY.
- **HDMI platform** (set-up 1, no FS levels possible): the platform's
  `LineStateSynthesiser` plays the host's half of reset-and-chirp towards
  LUNA: SE0 from power-up until LUNA's device chirp ends (LUNA needs >5 µs of
  SE0 to start HS detection and the chirp itself lasts 2 ms), then, **every
  time the device chirp ends** (`op_mode == CHIRP` and `tx_valid` falling)
  and within 2.5 ms of it (LUNA's `AWAIT_HOST_K` timeout), the three host K/J
  pairs with each level held ≈3–50 µs (strictly longer than LUNA's 150-cycle
  minimum, all six levels done well inside the 2.5 ms window). Outside the
  synthesiser's SE0 and K/J windows `line_state` is squelch-derived
  regardless of `xcvr_select`, so the SE0-during-idle path that triggers a
  re-chirp works while LUNA is temporarily in FS. Re-arming on the device chirp
  is essential, not a nicety: after 3 ms of squelch LUNA drops to FS, sees SE0
  again, re-enters HS detection and chirps once more; a power-up-only replay
  would leave the link dead after any idle gap or peer reset. The far-end
  board is assumed to be in HS. This keeps LUNA unmodified and puts the
  fiction in one small, clearly named platform block.

`op_mode`, `term_select` and `xcvr_select` from LUNA map to pull-up enable, HS
termination enable and chirp drive on the platforms that have those pins;
`session_end` is tied to 0 (VBUS assumed present, as luna-boards does for the
NeTV2) and `vbus_valid` to 1.

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

NeTV2 input is 50 MHz. Two cascaded primitives (the MMCM-cascade pattern of
the luna-boards NeTV2 platform and LiteVideo's `S7Clocking`; note LiteVideo
then uses BUFIO/BUFR for its ISERDES clocks, which this design deliberately
does not):

- PLLE2_ADV: 50 MHz × 24 = 1200 MHz VCO (PLL range 800–1866 MHz) → 60 MHz
  (÷20, MMCM reference) and 300 MHz (÷4, IDELAYCTRL reference, 52 ps taps).
- MMCME2_ADV fed by the 60 MHz: ×16 = 960 MHz VCO → 480 MHz (sampler CLK,
  both ISERDES), 240 MHz (TX OSERDES CLK and PSCLK), 120 MHz (`rx_cdr`,
  sampler CLKDIV), 60 MHz (`usb`; the PLL's 60 MHz is only the MMCM reference
  so all fabric domains are MMCM siblings with known phase). The two-phase
  fallback adds 480 MHz @ 90° and 120 MHz @ 22.5°; 6 of the 7 outputs.
- -2 3x variants use ×24 = 1440 MHz VCO (the -2 maximum) → 720/360/240/120/60.
  The -1 (Arty) plan uses ×12 = 720 MHz VCO → 360/240/120/60 with the
  two-ISERDES 3x sampler; every clock is then within -1 limits (BUFG 464 MHz,
  VCO 600–1200 MHz).
- All MMCM outputs have `USE_FINE_PS=TRUE` so P6 can slew the entire clock
  tree together (see 4.8). Resets are `~locked` through `ResetSynchronizer`.
- Domains: `usb` (60), `rx_cdr` (120), `rx_io` (480), `tx_io` (240),
  `idelay_ref` (300), `sync` (= `usb`, control/UART); the two-phase fallback
  adds `rx_io90` (480 @ 90°) and `rx_cdr90` (120 @ 22.5°).
- The BUFG-only design has no clock-region coupling, so a second RX pair
  costs two more ISERDES and two IDELAYs; if it sits in another bank (RX0 is
  bank 15, RX1 bank 14) it also needs that bank's own IDELAYCTRL.

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
  `USE_FINE_PS` output by 1/56 VCO period per request; the shift wraps
  round-robin without limit (UG472), so continuous stepping is a frequency
  offset. With VCO 960 MHz (18.6 ps per step, ≥12 PSCLK cycles per step) and
  PSCLK = 240 MHz (limit 500 MHz on -2) the pull range is about ±370 ppm;
  saturation is reported and the design keeps working un-disciplined because
  the CDR still absorbs the offset.
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

- `sim/usbhs.py` produces sample streams from byte packets: SYNC length,
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

- **Vivado**: Amaranth `XilinxPlatform(toolchain="Vivado")`; build
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
| IDELAY tap drift or missing openXC7 IDELAY support | two-phase variant with per-ISERDES aligned CLK/CLKDIV pairs (UG471 rule kept); both measured on hardware |
| Arty A7 is speed grade -1 | dedicated -1 clock plan (3x, 720 MHz VCO), selected by platform |
| Zero-differential idle makes the comparator chatter | digital squelch = activity timeout + mandatory SYNC; tested with noisy-idle wire model and on HDMI with tri-stated TX |
| 720 MHz over-spec clocks (3x single) | default 4x/480 MHz plan; 3x variant is measurement-only |
| nextpnr-xilinx lacks BUFIO/BUFR | default plan uses BUFGs only |
| Host chirp/FS levels impossible on HDMI | platform `LineStateSynthesiser` replays reset+chirp to LUNA; real FS/chirp validated in simulation and on set-ups 2/3 |
| HS/chirp levels below 7-series receiver common-mode range | common-mode shift is a stated requirement of the adaptor network |
| Board access: four cross-connected NeTV2s need `pi` key authorisation | flagged to user; P1 proceeds on rpi3-netv2/rpi5-netv2 (shared with other sessions; check `w` and recent files first, volatile loads only, never flash) |
| Two NeTV2 dev boards are different parts (35T vs 100T) | platform `variant` parameter; chipdbs exist for both |
| Machine memory pressure from other sessions | one Vivado run at a time, ≤2 sub-agents, builds under `build/` not `/tmp` |

## 10. Out of scope (v1)

USB low speed, host mode, hub repeater, SOF long-EOP generation, VBUS
sensing, suspend/resume power states beyond what LUNA already implements, a
soft CPU.
