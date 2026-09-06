# CLAUDE.md — working rules for crazy-fpga-usb2

Re-read this file at the start of every work session and before every hardware
test session.

## What this project is

USB 2.0 high-speed (480 Mbit/s) signalling recovered and transmitted on plain
Xilinx 7-series SelectIO pins, in native Amaranth, exposed to LUNA as a
virtual UTMI PHY. Design spec: `docs/superpowers/specs/2026-09-06-usb2-soft-phy-design.md`.
Plans live in `docs/superpowers/plans/`.

## Non-negotiable rules

- **The hardware is never broken.** Every failure is our gateware, our build,
  our constraints, or how we drive the board. Diagnose; do not blame the board.
- **Native Amaranth.** Vendor primitives via `Instance`. No VHDL. Only tiny,
  justified Verilog snippets if a primitive cannot be expressed otherwise.
- **Volatile loads only** (`pld load` / openFPGALoader without `--write-flash`).
  Never write SPI flash on any NeTV2; `rpi3-netv2` is a golden reference unit.
- **No patches, PRs or issues to Greg Davill's or upstream repos.** Work
  around LUNA limitations by subclassing/wrapping in this repo.
- **Small commits, feature branches, worktrees under `.worktrees/`.** Main is
  protected; land work through merge-commit PRs.
- **`uv` for all Python.** `uv run pytest`, `uv run usb2soft ...`. Never bare
  `python`/`pip`. Inline `python -c` is blocked by the sandbox: write scripts
  to files under `tmp/`.
- **Resources.** At most two sub-agents at a time; one Vivado run at a time;
  keep build outputs under `build/` (never `/tmp`, which is a small tmpfs);
  check `free -g` before heavy jobs — other sessions share this machine.
- **Evidence before claims.** Quote test output, utilisation numbers and
  hardware logs. Record hardware results under `docs/results/`.
- **Dates** in ISO 8601 (YYYY-MM-DD). Licence: Apache-2.0.

## Toolchains on this machine

- Vivado 2025.2: `/opt/Xilinx/2025.2/Vivado/bin/vivado` (xc7a35t and xc7a100t
  need no licence).
- openXC7 0.8.2 wrappers + chipdbs:
  `/home/tim/github/mithro/fpgas-online-test-designs/.venv/toolchains/openxc7/`
  (`bin/`, `chipdb/xc7a35t-fgg484.bin`, prjxray-db under
  `squashfs-root/opt/nextpnr-xilinx/external/prjxray-db`). Amaranth's
  `Xray` toolchain needs `DB_DIR`, `CHIPDB_DIR` and those `bin/` tools on PATH.
  nextpnr-xilinx has no BUFIO/BUFR: the default clock plan uses BUFGs only.
- System `yosys` at `/usr/bin/yosys`.
- LUNA: Greg Davill's fork, pinned as a git dependency in `pyproject.toml`
  (local clone at `/home/tim/github/gregdavill/luna`).

## Hardware at Welland (verified 2026-09-06)

- Gateway `tweed` = `ssh welland.fpgas.online` (user `tim`, sudo). Four
  cross-connected NeTV2 Pis are `10.21.1.10/.12/.14/.16` (+ `.18`), user `pi`,
  reachable only once the user's key is authorised there (blocked at time of
  writing).
- `pi@rpi3-netv2.welland.mithis.com`: RPi 3, NeTV2 **XC7A35T**, UART
  `/dev/ttyS0`, JTAG via openocd `bcm2835gpio` (`~/netv2/alphamax-rpi.cfg`,
  TCK4/TMS17/TDI27/TDO22). Golden unit: never flash.
- `tim@rpi5-netv2.welland.mithis.com`: RPi 5, NeTV2 **XC7A100T**, UART
  `/dev/ttyAMA0`, openocd 0.12 `linuxgpiod` (`~/netv2-phase4/openocd/netv2-jtag.cfg`,
  loader `~/netv2-phase4/netv2_update.py load <bit>`).
- Both dev boards are shared with other sessions: run `w` and `ls -lt ~ | head`
  first, keep sessions short, leave the UART getty as you found it.
- Before using a UART: stop `serial-getty@<port>`; on rpi3 also
  `pm2 stop netv2-status`; on Pi 5 restore the UART mux with
  `pinctrl set 14 a4; pinctrl set 15 a4` after stopping the getty.
- NeTV2 pins: HDMI RX0 K21/K22 (lane0, inverted), J20/J21, J22/H22, clk
  L19/L20 (bank 15); HDMI TX0 W21/W22, U20/V20, T21/U21, clk W19/W20
  (bank 14); HDMI TX1 E22/D22, C22/B22, B21/A21, clk G21/G22; HDMI RX1
  AA18/AB18, AA19/AB20, AB21/AB22, clk Y18/Y19. All TMDS_33, see
  litex-boards `kosagi_netv2.py` for inversions. Direct USB pair via the
  PCIe SMBus pins: D19 (N) / E19 (P), bank 16. UART E14 (TX) / E13 (RX).
  50 MHz clock J19. LEDs M21 N20 L21 AA21 R19 M16 (active low).

## Process

- Follow the superpowers skills: brainstorm → spec → plan → TDD implementation
  → code review → finish branch. Tests first for every gateware module.
- Every gateware milestone records `report_utilization -hierarchical` and
  timing summary into `docs/results/`.
- Keep `docs/hardware-setups.md` current whenever a new way to validate on
  hardware is found or used.

## Daily commands

```
uv run pytest -q                                   # all simulation/unit tests (must be green before any commit)
uv run usb2soft list                               # applets
uv run usb2soft build <applet> --variant a7-35|a7-100 [--no-build]   # Vivado build -> build/<applet>-netv2-<variant>/vivado/
uv run python host/netv2_run.py --host rpi5-netv2|rpi3-netv2 --bit <top.bit> --seconds 6 --log docs/results/logs/<date>-<what>-<host>.log
uv run --no-project python host/discovery_report.py < <log>      # cabling table from hdmi-discovery output
```

- After any change to platform pins or clocking, build and run `hello` on hardware before anything else.
- Other Claude sessions share rpi5-netv2 and rpi3-netv2. Before a load: check `w`/recent files, and
  announce the load to the sessions "litevideo HDMI upgrade" and "netv2 firmware upgrade and hdcp work"
  (ListAgents/SendMessage); they announce theirs too. rpi3-netv2 may hold an armed HDCP test bitstream: ask first.
- Measured 2026-09-06: no HDMI loopback cable on rpi5-netv2 (`docs/results/2026-09-06-p1-hdmi-discovery.md`).
