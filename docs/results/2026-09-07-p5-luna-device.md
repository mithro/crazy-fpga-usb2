# P5 result: LUNA device enumerating over the soft PHY (2026-09-07)

Branch `p5-utmi-luna`. Components: `usb2soft/phy/utmi.py` (`SoftUTMIPHY`), `usb2soft/phy/linestate.py`
(`HSLineState`, `LineStateSynthesiser`), `usb2soft/luna/device.py` (`SoftPHYUSBDevice`),
`usb2soft/sim/host.py` (bit-level HS host model), `usb2soft/hostlite.py` (gateware script host),
`usb2soft/applets/device_test.py`.

## Simulation

All with LUNA's reset-sequencer constants scaled (test-only monkeypatch of class attributes:
5 µs → 30 cycles, 2 ms chirp → 120, 2.5 ms → 400, 2.5 µs → 12, 200 µs → 600, 3 ms → 3000) and
the synthesiser scaled to match; LUNA sources unchanged.

| Test | Result |
|---|---|
| `test_luna_device.py::test_reset_walk_reaches_high_speed_and_recovers_after_idle` | power-up SE0 → device chirp (tx_valid held for the whole 2 ms) → host K/J ×3 → `speed == HIGH`, `op_mode == NORMAL`, `term_select == 0`; without traffic: 3 ms squelch → FS → re-chirp → HS regained once SOFs resume (2 chirps total) |
| `test_enumeration_over_the_soft_phy[0 / +500 / −500 ppm]` | GET_DESCRIPTOR(64) at address 0 returns LUNA's 18-byte device descriptor; SET_ADDRESS(5); GET_DESCRIPTOR at 5; IN to the old address gets no reply. **Device response latency 72–116 bit times** (host EOP → first driven bit; USB limit 192; PHY alone was 64–68 in P3) |
| `test_plain_usbdevice_stays_full_speed` | LUNA's `USBDevice(bus=phy)` never chirps (`always_fs`), which is why `SoftPHYUSBDevice` exists |
| `test_hostlite.py` | host-lite through host PHY → wire → device PHY → LUNA: 33 steady-state loops, 106 matched replies, 0 bad / timeouts / restarts in 9000 usb cycles; with 20 corrupted sample words injected it logs the faults and keeps looping |
| `test_device_test.py[internal, +3788 ppm, −4630 ppm]` | the applet core: 1 chirp, HS, ≥ 29 loops, zero faults; device CDR slips 55 down / 60 up in the offset modes |
| `test_soft_utmi.py`, `test_linestate.py`, `test_usb_crc.py` | op-modes (chirp = continuous K, non-driving = oe off), squelch-derived line state, synthesiser timing/re-arm/short-pulse rejection, CRC5/CRC16 accepted by LUNA's own token detector and data receiver |

Whole suite: 210 passed, 2 skipped, 8 slow deselected (2 min 26 s).

## Vivado builds (XC7A100T)

| Build | WNS / WHS | Whole design LUT / FF | `dev_phy` | `host_phy` | LUNA `device` | `host` (host-lite) | `console` |
|---|---|---|---|---|---|---|---|
| `device-test-internal` | 0.147 / 0.057 ns | 2224 / 1850 | 529 / 292 (rx 250/129, tx 240/106, line state + glue 39/57) | 491 / 240 | 532 / 525 | 202 / 291 | 459 / 338 |
| `device-test-async-fast` | 0.236 / 0.054 ns | 3080 / 2332 | 519 / 293 | 498 / 240 | 532 / 525 | 202 / 291 | 458 / 338 (+ two AsyncResamplers 430/240 each) |

The soft PHY with its UTMI control plane is ≈ 520 LUTs / 290 FFs (budget ≤ 700 / ≤ 600, spec §8);
LUNA's device core, host-lite and the console are test payload. No LUTAR-1, no critical warnings.

## Hardware: rpi5-netv2 (NeTV2 XC7A100T), 2026-09-07 05:03–05:04

Logs `logs/2026-09-07-devicetest-internal-rpi5.log` and `logs/2026-09-07-devicetest-async-fast-rpi5.log`
(30 report lines each, one per second, real LUNA timings: 2 ms chirp, 125 µs SOFs):

| Mode | Steady-state loops (first → last line) | ok replies | bad / timeouts / naks / restarts | SOFs/s | chirps | hs | device CDR slips/s |
|---|---|---|---|---|---|---|---|
| internal | 1 616 487 → 9 433 424 (269 550 /s) | 28.3 M | 0 / 0 / 0 / 0 on every line | 8000 | 1 | 1 on every line | 0 |
| async-fast (host at +3788 ppm, both directions) | 950 594 → 7 847 594 (237 828 /s) | 23.5 M | 0 / 0 / 0 / 0 on every line | 8000 | 1 | 1 on every line | 395 k down, 80 k up |

```
D 008FF150 01AFD3F5 00000000 00000000 00000000 00000000 000445A8 0001 1 00000000 00000001   internal, last line
D 0077BEAA 01673C04 00000000 00000000 00000000 00000000 00040728 0001 1 00282674 00C71D69   async-fast, last line
```

- One device chirp, then high speed for the whole capture; host-lite never restarted, so the
  device kept address 5 throughout (a re-chirp would have reset it and forced a restart).
- Each steady-state loop is a complete GET_DESCRIPTOR control transfer (SETUP+DATA0/ACK,
  IN/DATA1(18 bytes)/ACK, OUT+ZLP/ACK): ≈ 9.4 M control transfers with 28 M byte-exact replies
  and no error, at ≈ 270 k transfers/s; SOFs at exactly 8000/s.
- In the offset mode the device CDR slips 395 k/s down (+3788 ppm × 480 Mbit/s × the
  host-packet duty cycle ≈ 22 %) plus 80 k/s of acquisition steps at packet starts (short
  tokens, ~7 packets per loop), and the host PHY receives the device's replies offset the other
  way through the second resampler; still zero faults.
- What this proves on silicon: LUNA's unmodified high-speed device stack runs on the soft PHY
  (reset/chirp handshake, tokens, CRC5/CRC16, data toggles, address filtering, control transfers)
  at real clock rates, with a genuine 7.6× out-of-spec frequency offset in both directions.
  What it does not: the SelectIO front-end (needs a physical channel) and full-speed line
  states / a real host (needs set-up 2 or 3 and a level-based line state).

## HDMI variant (built, untested: no channel)

`device-test --mode hdmi --role device` (device PHY on HDMI RX0 lane 0 / TX0 lane 0, with the
synthesiser, no host-lite): `build/device-test-hdmi-device-rx0-netv2-a7-100/vivado`, WNS 0.299 ns,
WHS 0.050 ns, WPWS 0.491 ns (480 MHz BUFG), 1272 LUTs / 1097 FFs, 2× ISERDESE2 + OSERDESE2 placed.
The `--role host` build puts host-lite behind the pins for the other board. Two-board procedure:
load `--role host` on board A and `--role device` on board B with A.TX0 → B.RX0 and B.TX0 → A.RX0
cables; board A's UART line shows the enumeration counters, board B's the chirp count and `hs`.
Blocked on cabling / `pi` key on the cross-connected units, as in P4.
