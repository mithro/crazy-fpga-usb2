#!/usr/bin/env python3
"""Upload a bitstream to a NeTV2 host Pi, load it volatilely over GPIO JTAG, capture the UART.

Volatile loads only (``pld load``). Never flashes. Refuses to run if another interactive
session is logged in unless ``--force``. Services it stops for the capture (serial getty, the
rpi3 ``netv2-status`` pm2 app) are restarted afterwards if they were running before.

Usage:
  uv run python host/netv2_run.py --host rpi5-netv2 --bit build/hello-netv2-a7-100/vivado/top.bit --seconds 6
"""
import argparse
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent

PM2 = "~/n/bin/node ~/n/lib/node_modules/pm2/bin/pm2"


@dataclass(frozen=True)
class Profile:
    name: str
    ssh: str
    user: str
    serial: str
    variant: str
    openocd_cfg: str      # file under host/openocd/
    sudo: bool
    extra_pre: tuple = ()
    pm2_app: str | None = None      # pm2 application that holds the serial port, if any


PROFILES = {
    "rpi3-netv2": Profile("rpi3-netv2", "pi@rpi3-netv2.welland.mithis.com", "pi", "/dev/ttyS0",
                          "a7-35", "netv2-jtag-bcm2835.cfg", sudo=True, pm2_app="netv2-status"),
    "rpi5-netv2": Profile("rpi5-netv2", "tim@rpi5-netv2.welland.mithis.com", "tim", "/dev/ttyAMA0",
                          "a7-100", "netv2-jtag-gpiod.cfg", sudo=True,
                          extra_pre=("sudo pinctrl set 14 a4; sudo pinctrl set 15 a4",
                                     # A stale /sys/class/gpio export (from an old sysfsgpio OpenOCD run)
                                     # blocks libgpiod from claiming the JTAG lines; release them.
                                     # sysfs numbers are chip base + line; the 40-pin header controller
                                     # is the 54-line chip (pinctrl-rp1 on a Pi 5, bcm2835 on Pi 3/4).
                                     "for c in /sys/class/gpio/gpiochip*; do "
                                     "[ \"$(cat $c/ngpio)\" = 54 ] || continue; b=$(cat $c/base); "
                                     "for n in 4 17 22 24 27; do g=$((b+n)); [ -e /sys/class/gpio/gpio$g ] && "
                                     "echo $g | sudo tee /sys/class/gpio/unexport; done; done; true")),
}


def remote_dir(profile):
    return f"/home/{profile.user}/usb2soft"


def getty_unit(profile):
    return f"serial-getty@{profile.serial.split('/')[-1]}.service"


def load_command(profile, remote_bit):
    # Both cfg files source the Series-7 TAP/PLD definition themselves (see host/openocd/).
    cfg = f"{remote_dir(profile)}/{profile.openocd_cfg}"
    sudo = "sudo " if profile.sudo else ""
    return f'{sudo}openocd -f {shlex.quote(cfg)} -c "init; scan_chain; pld load 0 {shlex.quote(remote_bit)}; exit"'


def probe_command(profile):
    """Prints one line per service we may stop: '<name> active|inactive'."""
    cmds = [f"echo getty $(systemctl is-active {getty_unit(profile)})"]
    if profile.pm2_app:
        cmds.append(f"echo pm2 $({PM2} jlist 2>&1 | grep -q '\"name\":\"{profile.pm2_app}\".*\"status\":\"online\"' "
                    f"&& echo active || echo inactive)")
    return "; ".join(cmds)


def pre_capture_commands(profile):
    cmds = [f"sudo systemctl stop {getty_unit(profile)} || true"]
    if profile.pm2_app:
        cmds.append(f"{PM2} stop {profile.pm2_app} || true")
    cmds += list(profile.extra_pre)
    return cmds


def restore_commands(profile, was_active):
    """Restart what we stopped, based on the probe result (dict name -> bool)."""
    cmds = []
    if was_active.get("getty"):
        cmds.append(f"sudo systemctl start {getty_unit(profile)} || true")
    if profile.pm2_app and was_active.get("pm2"):
        cmds.append(f"{PM2} start {profile.pm2_app} || true")
    return cmds


SESSION_PROBE = ("echo OWN=$XDG_SESSION_ID; loginctl list-sessions --no-legend; echo ===RECENT; "
                 "find ~ -maxdepth 1 -mmin -5 -not -name usb2soft -not -name '.*' 2>&1 | head -20")


def other_sessions(loginctl_output, own_id):
    """Remote user sessions other than ours: rows of `loginctl list-sessions --no-legend` whose
    class is 'user' (or unknown, on old systemd), that have no seat (i.e. not the local desktop)
    and whose id differs from ``own_id``. `w -h` misses non-tty ssh sessions, loginctl does not."""
    others = []
    for line in loginctl_output.splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[0] == own_id:
            continue
        seat = parts[3] if len(parts) > 3 and parts[3] != "-" and not parts[3].isdigit() else ""
        cls = parts[5] if len(parts) > 5 else "user"
        if seat.startswith("seat") or cls != "user":
            continue
        others.append(line.strip())
    return others


def parse_session_probe(text):
    """Split the SESSION_PROBE output into (own_id, loginctl_text, recent_files)."""
    own, body = text.split("\n", 1) if "\n" in text else (text, "")
    own_id = own.replace("OWN=", "").strip()
    loginctl_text, _, recent = body.partition("===RECENT\n")
    recent_files = [l.strip() for l in recent.splitlines() if l.strip() and not l.startswith("find:")]
    return own_id, loginctl_text, recent_files


def bitstream_matches(profile, bit_path):
    return profile.variant in str(bit_path)


def parse_probe(text):
    result = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            result[parts[0]] = parts[1] == "active"
    return result


def ssh(profile, command, check=True, capture=False):
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", profile.ssh, command]
    return subprocess.run(argv, check=check, text=True, capture_output=capture)


def upload(profile, local, remote):
    with open(local, "rb") as f:
        subprocess.run(["ssh", "-o", "BatchMode=yes", profile.ssh,
                        f"mkdir -p {shlex.quote(remote_dir(profile))} && cat > {shlex.quote(remote)}"],
                       check=True, stdin=f)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", choices=sorted(PROFILES), required=True)
    ap.add_argument("--bit", required=True)
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--log", default=None, help="save captured UART text here")
    ap.add_argument("--force", action="store_true",
                    help="proceed despite other sessions or a bitstream path that does not name the variant")
    args = ap.parse_args(argv)
    profile = PROFILES[args.host]

    if not bitstream_matches(profile, args.bit) and not args.force:
        print(f"{args.bit} does not look like a {profile.variant} bitstream for {profile.name}; "
              f"re-run with --force if it really is", file=sys.stderr)
        return 3

    own_id, loginctl_text, recent = parse_session_probe(ssh(profile, SESSION_PROBE, check=False, capture=True).stdout)
    others = other_sessions(loginctl_text, own_id)
    print(f"[{profile.name}] other user sessions: {len(others)}")
    for o in others:
        print(f"    {o}")
    if recent:
        print(f"[{profile.name}] files changed in the last 5 min: {recent}")
    # Idle lingering ssh sessions from other agents are normal; recent file activity is not.
    if recent and not args.force:
        print("recent activity on the host; coordinate with the other session or re-run with --force",
              file=sys.stderr)
        return 2

    rdir = remote_dir(profile)
    remote_bit = f"{rdir}/{Path(args.bit).name}"
    upload(profile, args.bit, remote_bit)
    upload(profile, HERE / "openocd" / profile.openocd_cfg, f"{rdir}/{profile.openocd_cfg}")
    upload(profile, HERE / "pi" / "console_capture.py", f"{rdir}/console_capture.py")

    was_active = parse_probe(ssh(profile, probe_command(profile), check=False, capture=True).stdout)
    print(f"[{profile.name}] services before: {was_active}")
    try:
        for c in pre_capture_commands(profile):
            ssh(profile, c, check=False)
        t0 = time.time()
        ssh(profile, load_command(profile, remote_bit))
        print(f"[{profile.name}] loaded in {time.time()-t0:.1f}s")
        cap = ssh(profile, f"python3 {rdir}/console_capture.py --port {profile.serial} --seconds {args.seconds}",
                  capture=True)
        print(cap.stdout)
        if args.log:
            Path(args.log).parent.mkdir(parents=True, exist_ok=True)
            Path(args.log).write_text(cap.stdout)
    finally:
        for c in restore_commands(profile, was_active):
            ssh(profile, c, check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
