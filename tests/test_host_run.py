import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "host"))
import netv2_run as nr  # noqa: E402


def test_profiles():
    p3 = nr.PROFILES["rpi3-netv2"]
    p5 = nr.PROFILES["rpi5-netv2"]
    assert p3.ssh == "pi@rpi3-netv2.welland.mithis.com" and p3.serial == "/dev/ttyS0"
    assert p5.ssh == "tim@rpi5-netv2.welland.mithis.com" and p5.serial == "/dev/ttyAMA0"
    assert p3.variant == "a7-35" and p5.variant == "a7-100"


def test_load_command_pi3_uses_bcm2835_cfg():
    cmd = nr.load_command(nr.PROFILES["rpi3-netv2"], "/home/pi/usb2soft/top.bit")
    assert "openocd" in cmd and "netv2-jtag-bcm2835.cfg" in cmd
    assert "pld load 0 /home/pi/usb2soft/top.bit" in cmd
    assert "write" not in cmd and "flash" not in cmd
    assert cmd.count(" -f ") == 1      # the cfg sources the xc7 TAP definition itself


def test_load_command_pi5_uses_gpiod_cfg():
    cmd = nr.load_command(nr.PROFILES["rpi5-netv2"], "/home/tim/usb2soft/top.bit")
    assert "netv2-jtag-gpiod.cfg" in cmd and "sudo" in cmd
    assert cmd.count(" -f ") == 1


def test_pre_capture_commands_stop_getty_only():
    cmds = nr.pre_capture_commands(nr.PROFILES["rpi3-netv2"])
    assert any("serial-getty@ttyS0" in c for c in cmds)
    assert any("pm2 stop netv2-status" in c for c in cmds)
    assert not any("mask" in c for c in cmds)


def test_remote_dir_matches_user():
    assert nr.remote_dir(nr.PROFILES["rpi3-netv2"]) == "/home/pi/usb2soft"
    assert nr.remote_dir(nr.PROFILES["rpi5-netv2"]) == "/home/tim/usb2soft"


def test_openocd_cfgs_source_xc7_tap():
    for name in ("netv2-jtag-bcm2835.cfg", "netv2-jtag-gpiod.cfg"):
        text = (pathlib.Path(nr.HERE) / "openocd" / name).read_text()
        assert "xilinx-xc7.cfg" in text and "xc7.cfg" in text, name
