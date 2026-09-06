from amaranth import Module, Signal

from usb2soft.applets import Applet, APPLETS, register
from usb2soft.build import build_applet, vivado_env
from usb2soft import cli


@register
class _Dummy(Applet):
    name = "dummy-test-applet"
    description = "test only"

    def elaborate(self, platform):
        m = Module()
        c = Signal(4)
        m.d.sync += c.eq(c + 1)
        return m


def test_registry_lists_applet():
    assert APPLETS["dummy-test-applet"] is _Dummy


def test_build_plan_without_toolchain(tmp_path):
    result = build_applet("dummy-test-applet", platform="netv2", variant="a7-35",
                          toolchain="vivado", build_root=tmp_path, do_build=False)
    assert (result.build_dir / "top.v").exists()
    assert result.build_dir == tmp_path / "dummy-test-applet-netv2-a7-35" / "vivado"


def test_vivado_env_points_at_settings():
    env = vivado_env()
    assert env["AMARANTH_ENV_VIVADO"].endswith("settings64.sh")


def test_cli_list(capsys):
    cli.main(["list"])
    out = capsys.readouterr().out
    assert "dummy-test-applet" in out


def test_cli_build_dry(tmp_path):
    rc = cli.main(["build", "dummy-test-applet", "--platform", "netv2", "--variant", "a7-35",
                   "--toolchain", "vivado", "--build-root", str(tmp_path), "--no-build"])
    assert rc == 0
    assert (tmp_path / "dummy-test-applet-netv2-a7-35" / "vivado" / "top.xdc").exists()
