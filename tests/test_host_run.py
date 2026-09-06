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


def test_pre_capture_commands_stop_getty_and_pm2_without_masking():
    cmds = nr.pre_capture_commands(nr.PROFILES["rpi3-netv2"])
    assert any("systemctl stop serial-getty@ttyS0.service" in c for c in cmds)
    assert any("pm2 stop netv2-status" in c for c in cmds)
    assert not any("mask" in c for c in cmds)
    cmds5 = nr.pre_capture_commands(nr.PROFILES["rpi5-netv2"])
    assert not any("pm2" in c for c in cmds5)


def test_restore_only_what_was_active():
    p3 = nr.PROFILES["rpi3-netv2"]
    assert nr.restore_commands(p3, {"getty": False, "pm2": False}) == []
    both = nr.restore_commands(p3, {"getty": True, "pm2": True})
    assert any("systemctl start serial-getty@ttyS0.service" in c for c in both)
    assert any("pm2 start netv2-status" in c for c in both)
    p5 = nr.PROFILES["rpi5-netv2"]
    assert nr.restore_commands(p5, {"getty": True, "pm2": True}) == ["sudo systemctl start serial-getty@ttyAMA0.service || true"]


def test_parse_probe():
    assert nr.parse_probe("getty inactive\npm2 active\n") == {"getty": False, "pm2": True}


PI5_LOGINCTL = """   1  109 rpi-first-boot-wizard seat0 1315   user          - no -
1182 1000 tim                   -     289625 user          - no -
1183 1000 tim                   -     289630 manager       - no -
1416 1000 tim                   -     355801 user          - no -
   2  109 rpi-first-boot-wizard -     1325   manager-early - no -
"""
PI3_LOGINCTL = """        c3       1000 pi               seat0                            
       c21       1000 pi                                                
       c25       1000 pi                                                
"""


def test_other_sessions_from_loginctl():
    others = nr.other_sessions(PI5_LOGINCTL, own_id="1416")
    assert len(others) == 1 and others[0].startswith("1182")      # the other ssh session, not desktop/manager
    others3 = nr.other_sessions(PI3_LOGINCTL, own_id="c21")
    assert len(others3) == 1 and others3[0].startswith("c25")


def test_parse_session_probe():
    own, text, recent = nr.parse_session_probe("OWN=1416\n" + PI5_LOGINCTL + "===RECENT\n/home/tim/litevideo\n")
    assert own == "1416" and "1182" in text and recent == ["/home/tim/litevideo"]
    own, text, recent = nr.parse_session_probe("OWN=c21\n" + PI3_LOGINCTL + "===RECENT\n")
    assert recent == []


def test_bitstream_variant_guard():
    assert nr.bitstream_matches(nr.PROFILES["rpi5-netv2"], "build/hello-netv2-a7-100/vivado/top.bit")
    assert not nr.bitstream_matches(nr.PROFILES["rpi5-netv2"], "build/hello-netv2-a7-35/vivado/top.bit")


def test_remote_dir_matches_user():
    assert nr.remote_dir(nr.PROFILES["rpi3-netv2"]) == "/home/pi/usb2soft"
    assert nr.remote_dir(nr.PROFILES["rpi5-netv2"]) == "/home/tim/usb2soft"


def test_openocd_cfgs_source_xc7_tap():
    for name in ("netv2-jtag-bcm2835.cfg", "netv2-jtag-gpiod.cfg"):
        text = (pathlib.Path(nr.HERE) / "openocd" / name).read_text()
        assert "xilinx-xc7.cfg" in text and "xc7.cfg" in text, name
