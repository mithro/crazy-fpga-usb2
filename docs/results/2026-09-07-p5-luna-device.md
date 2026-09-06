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

## Hardware

(appended below once the rpi5-netv2 slot is available)
