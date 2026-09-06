#!/usr/bin/env python3
"""Upload a bitstream to a NeTV2 host Pi, load it volatilely over GPIO JTAG, capture the UART.

Volatile loads only (``pld load``). Never flashes. Refuses to run if another interactive
session is logged in unless ``--force``.

Usage:
  uv run python host/netv2_run.py --host rpi5-netv2 --bit build/hello-netv2-a7-100/vivado/top.bit --seconds 6
"""
import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent


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


PROFILES = {
    "rpi3-netv2": Profile("rpi3-netv2", "pi@rpi3-netv2.welland.mithis.com", "pi", "/dev/ttyS0",
                          "a7-35", "netv2-jtag-bcm2835.cfg", sudo=True,
                          extra_pre=("~/n/bin/node ~/n/lib/node_modules/pm2/bin/pm2 stop netv2-status || true",)),
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


def load_command(profile, remote_bit):
    # Both cfg files source the Series-7 TAP/PLD definition themselves (see host/openocd/).
    cfg = f"{remote_dir(profile)}/{profile.openocd_cfg}"
    sudo = "sudo " if profile.sudo else ""
    return f'{sudo}openocd -f {cfg} -c "init; scan_chain; pld load 0 {remote_bit}; exit"'


def pre_capture_commands(profile):
    getty = profile.serial.split("/")[-1]
    cmds = [f"sudo systemctl stop serial-getty@{getty}.service || true"]
    cmds += list(profile.extra_pre)
    return cmds


def ssh(profile, command, check=True, capture=False):
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", profile.ssh, command]
    return subprocess.run(argv, check=check, text=True, capture_output=capture)


def upload(profile, local, remote):
    with open(local, "rb") as f:
        subprocess.run(["ssh", "-o", "BatchMode=yes", profile.ssh,
                        f"mkdir -p {remote_dir(profile)} && cat > {remote}"],
                       check=True, stdin=f)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", choices=sorted(PROFILES), required=True)
    ap.add_argument("--bit", required=True)
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--log", default=None, help="save captured UART text here")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    profile = PROFILES[args.host]

    who = ssh(profile, "w -h", capture=True).stdout
    # Ignore our own non-interactive ssh (shows as "sshd-session: <user> [priv]") and the local
    # desktop session (tty7); anything else is another person or session.
    others = [l for l in who.splitlines()
              if l.strip() and "sshd-session" not in l and "lightdm" not in l]
    print(f"[{profile.name}] logged-in sessions:\n{who}")
    if others and not args.force:
        print("other sessions present; re-run with --force if you have checked they are idle", file=sys.stderr)
        return 2

    rdir = remote_dir(profile)
    remote_bit = f"{rdir}/{Path(args.bit).name}"
    upload(profile, args.bit, remote_bit)
    upload(profile, HERE / "openocd" / profile.openocd_cfg, f"{rdir}/{profile.openocd_cfg}")
    upload(profile, HERE / "pi" / "console_capture.py", f"{rdir}/console_capture.py")

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
