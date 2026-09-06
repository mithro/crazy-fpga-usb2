# P1 result: `hdmi-discovery` applet on NeTV2 hardware (2026-09-06)

Purpose: measure, rather than assume, which HDMI lanes are cabled between
boards. Every HDMI TX lane (2 ports × 4 pairs) transmits a 6-byte beacon
frame at 1 Mbaud (`A5 id2 id1 id0 port|lane checksum`, id = DNA[23:0]);
every HDMI RX lane listens with true and inverted polarity; the GPIO UART
reports one line per RX lane every 0.5 s (`D <own> R<port><lane> <peer>
<peer port|lane> <flags>`; flags bit0 = seen, bit1 = inverted).
Applet: `usb2soft/applets/hdmi_discovery.py`; parser: `host/discovery_report.py`.

## Build (Vivado 2025.2)

| Variant | WNS | WHS | WPWS | LUTs | FFs | IOB | IBUFDS | BUFG | PLL |
|---------|-----|-----|------|------|-----|-----|--------|------|-----|
| a7-100 (`c441601`) | 9.203 ns | 0.072 ns | 6.667 ns | 1430 | 2669 | 34 | 8 | 2 | 1 |

The applet is a test instrument (16 UART receivers, 8 transmitters, two text
reporters); its size is not part of the PHY budget.

## rpi5-netv2 (NeTV2 XC7A100T, DNA `0742C4E63B9085C`, id `B9085C`)

Log: `logs/2026-09-06-discovery-rpi5.log` (8 s capture, four full reports).

```
board B9085C
rx0.clk  -
rx0.d0   -
rx0.d1   -
rx0.d2   -
rx1.clk  -
rx1.d0   -
rx1.d1   -
rx1.d2   -
```

No RX lane hears any beacon: **no HDMI loopback cable is fitted on this
board**. The litevideo session working on the same board confirmed the
cabling from its side: one HDMI output (believed to be `hdmi_out 0`) goes to
a Magewell XI100DUSB-HDMI capture device on the Pi's USB, both HDMI inputs
are bare, and the three HPD sense lines were measured unplugged earlier the
same day. Nothing on the FPGA side needs enabling for the input path (plain
IBUFDS on TMDS_33 with the board's pull-ups); a source cable into `hdmi_in 0`
is all that is missing.

One artefact in the raw log: `rx0.d0` reported peer `008080`, lane `00`,
flags `00`. That is a stale value from a single chance match of the 6-byte
window on a floating lane (`A5 00 80 80 00 00` has a valid XOR checksum);
`seen` had already timed out. The listener now requires two consecutive
frames that agree on id, lane and polarity before reporting a lane as seen
(`test_single_or_disagreeing_frames_are_ignored`). The bitstream that
produced this log predates that change.

## rpi3-netv2 (NeTV2 XC7A35T)

Not run: the board is on hold at another session's request (an armed HDCP
receiver bitstream awaiting a live handshake test). Its HDMI in 0 is fed by a
Raspberry Pi Zero and its HDMI out 0 feeds a capture dongle, so no loopback is
expected there either.

## Cross-connected boards (pi-sw1-p10/12/14/16)

Not run: `pi` account access on those hosts is pending. This applet is the
first thing to load there; its report fills in the cabling matrix in
`docs/hardware-setups.md` §1.

## Conclusion

The TX/RX/UART machinery works end to end (headers and all eight lane lines
arrive well-formed every period, the frame decoder's false-positive mode was
observed and closed), but no HDMI channel between two FPGAs is currently
available on the reachable boards. P4 (HDMI link at 480 Mbit/s) therefore
needs either a loopback cable on rpi5-netv2 or access to the cross-connected
units.
