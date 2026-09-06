# P6 TX Clock Discipline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Slave the device's whole fabric clock tree (480/240/120/60 MHz from the MMCM) to the host's bit clock, using the CDR's slip strobes as the frequency-error signal and the MMCM's dynamic fine phase shift as the actuator, so that once locked the CDR phase pointer stands still and every bit the device transmits is frequency-locked to the host. Demonstrated in simulation (behavioural MMCM, ramping offset) and on rpi5-netv2 against an offset source inside the pull range (−391 / +434 ppm) and against one outside it (+3788 ppm, where the loop saturates and reports it).

**Architecture:** A dynamic fine phase shift on a 7-series MMCM moves *every* `USE_FINE_PS` output by 1/56 of a VCO period per step (18.6 ps at 960 MHz), wrapping round-robin without limit (UG472), so a steady stepping rate is a frequency offset of the entire tree. `MMCMPhaseSlewer` runs in the un-shifted 300 MHz `idelay_ref` domain (PSCLK; ≤ 500 MHz on -2), issues PSEN/PSINCDEC and waits for PSDONE (12 PSCLK cycles), driven by a signed rate word through a phase accumulator (NCO): steps/s = 300 MHz × rate / 2^16, capped at one per 12 cycles, so the pull range is 18.6 ps × 25 M/s = **±465 ppm** and one LSB of `rate` is 0.085 ppm. `FrequencyEstimator` (rx_cdr domain) integrates `slip_down − slip_up` while `in_packet` (acquisition steps outside packets carry no frequency information) into the rate word: since slips are the derivative of CDR phase, i.e. proportional to the frequency error, a pure integrator gives a first-order loop, time constant ≈ 24 ms / packet duty cycle, no overshoot; `rate` saturates at ±5461 and a sticky `saturated` flag is exposed. The rate word crosses to `idelay_ref` with a `BusSynchronizer`. `NeTV2PhyClocks` gains `discipline=True` (PSCLK = `idelay_ref`, PSEN/PSINCDEC/PSDONE routed out) and, for the hardware test, `offset="+434"|"-391"` (a second **MMCME2** with fractional multiplier/divider: D=3, M=57.625, O=8.0 → 120.05208 MHz; D=3, M=63.875, O=8.875 → 119.95305 MHz; no integer PLLE2 ratio lands within ±450 ppm of 120 MHz).

**Tech Stack:** Amaranth 0.5.9, Vivado 2025.2, rpi5-netv2. Branch `p6-clock-discipline` from `main` after PR #5.

## Physics the tests rely on

- Slip rate while tracking = |ppm| × 480e6 × duty (measured in P4/P5: 1.69 M/s at +3788 ppm, duty 0.93).
- One step = 18.6 ps = 1/112 UI; 465 ppm pull range at 300 MHz PSCLK; ±372 ppm if PSCLK were 240 MHz.
- Loop: θ̇ = 480e6·Δf (UI/s, slips), Δf = Δf₀ − 0.085e-6·rate, rate = Σslips → θ̇ = 480e6·Δf₀ − 40.8·θ → τ ≈ 24 ms at 100 % duty; residual steady-state slips = 0 (integral action); rate settles at Δf₀ / 0.085 ppm LSB (e.g. +434 ppm → 5106).

## File structure

| File | Responsibility |
|---|---|
| `usb2soft/clock/discipline.py` | `FrequencyEstimator(rate_bits=16, saturate=True)` (rx_cdr domain; inputs slip_up/slip_down/in_packet; outputs `rate`, `saturated`, `net_slips` diagnostic), `MMCMPhaseSlewer(domain="idelay_ref")` (NCO + PSEN/PSDONE handshake; inputs `rate`, outputs `ps_en`, `ps_incdec`, `steps` counter, `busy`), `ClockDiscipline` glue (BusSynchronizer for `rate`, enable, hold) |
| `usb2soft/clock/netv2.py` | `NeTV2PhyClocks(discipline=False, offset=None)`: PSCLK = `idelay_ref` when disciplining; `offset` builds the second MMCM (fractional) for the `tx_async` domain instead of the integer PLL |
| `usb2soft/clock/xilinx.py` | `MMCME2` accepts fractional `CLKFBOUT_MULT_F` / `CLKOUT0_DIVIDE_F` (verify the solver/wrapper render `57.625`, `8.0`); `ClockSolution` constructed directly for the offset MMCM |
| `usb2soft/sim/mmcm_model.py` | Python behavioural model: consumes `ps_en`/`ps_incdec` per PSCLK tick, asserts `ps_done` 12 ticks later, accumulates phase; exposes `correction_ppm` = steps/s × 18.6 ps for the wire model |
| `usb2soft/applets/link_test.py` | `--discipline` flag (any mode) and `--offset +434|-391` (new offset source); report line gains `<rate> <steps> <sat>` |
| `tests/test_discipline.py` | estimator/slewer unit tests; closed loop with the MMCM model and `LineSampler` at constant and ramping offsets |
| `docs/results/2026-09-0X-p6-clock-discipline.md`, `docs/clocking.md` | measured lock, residual slips, rate word vs offset, saturation behaviour, resources |

---

### Task 1: `MMCMPhaseSlewer` (NCO + handshake)

- [ ] Tests: with `rate = 0` no PSEN; with `rate = +5461` (max) PSEN pulses exactly every 12 PSCLK cycles with PSINCDEC = 1 and never while PSDONE is pending; with `rate = −2730` the average interval is 24 cycles and PSINCDEC = 0; a rate change mid-run takes effect without a glitch (no two PSEN within 12 cycles). Simulated with a tiny PSDONE responder (12 cycles).
- [ ] Implement: 16-bit accumulator `acc += |rate|` per PSCLK cycle when `~busy`; when `acc` overflows (carry) and `~busy`: `ps_en` for one cycle, `busy` until `ps_done`; `steps` (signed 32) counter. Commit.

### Task 2: `FrequencyEstimator`

- [ ] Tests: slips only while `in_packet` count; `rate` integrates `slip_down − slip_up` (sign: a *down* slip means the pointer wrapped down, i.e. the host is **faster** than us → we must speed up → positive rate; confirm the sign against P4 hardware data: +3788 ppm gave slips *down*); saturates at ±5461 with `saturated` sticky; `hold` freezes it; `clear` resets.
- [ ] Implement (rx_cdr domain, 120 MHz; the ±1 per-cycle integration is trivial). Commit.

### Task 3: `ClockDiscipline` glue + behavioural MMCM model + closed loop

- [ ] `sim/mmcm_model.py`: testbench coroutine on a `ps` clock domain (300 MHz) that answers PSDONE and integrates steps into `phase_steps`; `correction_ppm(window)` for the wire model. Closed-loop test harness: `RxPath` (as in P2 tests) fed by the Python `LineSampler` whose `ppm` is `ppm_host − correction_ppm` re-evaluated per packet; packets of 512 random bytes back to back with 16-UI gaps (duty ≈ 0.95).
- [ ] Tests: (a) +300 ppm constant: within 60 ms simulated (≈ 3.6 M usb cycles is too long for the Python simulator — scale the loop gain for simulation: `rate_shift` parameter making one slip worth 16 LSB, τ ≈ 1.5 ms, and simulate 6 ms ≈ 720 k rx_cdr cycles… still ~10 min. Reduce further: `LineSampler` at 4 samples/UI but packets of 64 bytes and `rate_shift=8` with a 3 ms window; target ≤ 90 s per test) the slip rate in the last 20 % of the window is < 5 % of the initial rate and `rate` ≈ 300/0.085 × gain within 10 %; (b) ramp +100 → −100 ppm over the window: `rate` follows with the expected lag, no saturation; (c) +600 ppm: `saturated` asserts, the CDR still decodes every packet (zero errors), slip rate reduced by ≈ 465/600.
- [ ] Mark (a)–(c) `slow` if > 60 s each; keep one fast smoke test (short window, checks the sign of the correction only) in the default suite. Commit.

### Task 4: Clocks and applet

- [ ] `MMCME2`/solver: fractional M and O rendering; `NeTV2PhyClocks(offset=...)`: second MMCME2 (D=3, M=57.625 → VCO 960.4 MHz, O=8.0 → 120.05208 MHz, +434 ppm; or M=63.875 → 1064.6 MHz, O=8.875 → 119.95305 MHz, −391 ppm) on `clk50`, reset by `~mmcm.locked`, `tx_async` domain and reset from its LOCKED as today; RTLIL tests for both. `discipline=True`: PSCLK = `idelay_ref` (from `pll.clocks`), PSEN/PSINCDEC/PSDONE exposed; RTLIL test.
- [ ] `link-test --discipline [--offset ±]`: instantiate `ClockDiscipline` between `core.rx.cdr` and the clocks; report line adds `rate` (hex, two's complement 16), `steps` (32), `sat`. Also `--mode async-fast --discipline` for the saturation case. Vivado builds (a7-100): `internal --discipline` (expect rate ≈ 0, steps ≈ 0), `--offset +434 --discipline`, `--offset -391 --discipline`, `--offset +434` without discipline (baseline slip rate), `async-fast --discipline`. Timing at 300 MHz for the slewer; utilisation of `discipline` (< 150 LUTs expected). Commit applet+tests, then results.

### Task 5: Hardware (rpi5-netv2, announced, own coordination row)

- [ ] Baseline `+434` without discipline: slips/s ≈ 434e-6 × 480e6 × 0.93 ≈ 194 k/s, phase pointer wandering.
- [ ] `+434 --discipline`: slips/s falls to ≈ 0 (expect < 1 % of baseline after the first second), `rate` settles near +5106 ± 5 %, `steps` grows at ≈ 25 M/s × (434/465), packets still error-free; same for `−391`. `internal --discipline`: rate stays within ±20 LSB. `async-fast --discipline`: `sat = 1`, slips/s ≈ (3788 − 465)/3788 of the P4 figure (≈ 1.48 M/s), zero packet errors. 30 s captures each, logs under `docs/results/logs/`.
- [ ] Results page with the table above; `docs/clocking.md` section on the discipline loop (pull range, PSCLK choice, why the whole tree moves, jitter note on fractional MMCM sources); spec §4.8 revision. Commit.

### Task 6: Review and PR

- [ ] Code review (subagent): loop sign/stability reasoning, CDC of `rate` (BusSynchronizer), PSEN/PSDONE protocol vs UG472 (PSEN one cycle, wait for PSDONE, ≥ 12 cycles), fractional MMCM legality (CLKOUT0 only supports `_F`; VCO range 600–1440 on -2), test strength; fixes; PR "P6: TX clock discipline — CDR-slip-driven MMCM phase slewing locks the device clock tree to the host".

## Open questions to settle during execution

- PSDONE latency is 12 PSCLK cycles nominal; confirm in UG472 and leave margin (wait for PSDONE, never a fixed count).
- Whether a continuously wrapping fine phase shift is glitch-free across the 56-step boundary (UG472 says the shift is continuous; the P4/P5 hardware runs with the loop closed are the proof).
- The UART console's baud drifts with the disciplined tree (±465 ppm max): harmless at 115200.
