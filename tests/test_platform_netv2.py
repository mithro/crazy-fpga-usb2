import pytest
from amaranth import Elaboratable, Module, Signal
from amaranth.lib import io


class _Blink(Elaboratable):
    """Requests one LED, HDMI RX0 lane 0 and the USB pair, and uses the ``sync`` domain so the
    platform creates it from ``clk50`` (amaranth only instantiates domains that are used)."""
    def elaborate(self, platform):
        m = Module()
        led = platform.request("led", 0, dir="-")
        m.submodules.led = led_buf = io.Buffer("o", led)
        hdmi = platform.request("hdmi_in", 0, dir="-")
        m.submodules.d0 = d0 = io.Buffer("i", hdmi.d0)
        usb = platform.request("usb_hax", 0, dir="-")
        m.submodules.usb = usb_buf = io.Buffer("i", usb.d)
        toggle = Signal()
        m.d.sync += toggle.eq(~toggle)
        m.d.comb += led_buf.o.eq(d0.i ^ usb_buf.i ^ toggle)
        return m


@pytest.mark.parametrize("variant,device", [("a7-35", "xc7a35t"), ("a7-100", "xc7a100t")])
def test_variant_selects_device(variant, device):
    from usb2soft.platforms.netv2 import NeTV2Platform
    plat = NeTV2Platform(variant=variant)
    assert plat.device == device
    assert plat.package == "fgg484"
    assert plat.speed == "2"


def test_unknown_variant_rejected():
    from usb2soft.platforms.netv2 import NeTV2Platform
    with pytest.raises(ValueError):
        NeTV2Platform(variant="a7-200")


def test_xdc_contains_expected_pins(tmp_path):
    from usb2soft.platforms.netv2 import NeTV2Platform
    plat = NeTV2Platform(variant="a7-35")
    plan = plat.build(_Blink(), name="t", build_dir=str(tmp_path), do_build=False)
    xdc = plan.files["t.xdc"]
    for pin in ("M21", "K21", "K22", "E19", "D19", "J19"):
        assert f"LOC {pin}" in xdc, pin
    # amaranth tcl-quotes attribute values: set_property IOSTANDARD "TMDS_33" [...]
    assert 'IOSTANDARD "TMDS_33"' in xdc
    assert 'IOSTANDARD "LVCMOS33"' in xdc
    assert "create_clock" in xdc and "-period 20.0" in xdc  # 50 MHz default clock constraint


def test_hdmi_rx0_lane0_is_inverted_and_usb_pair_is_not():
    from usb2soft.platforms.netv2 import NeTV2Platform
    plat = NeTV2Platform(variant="a7-35")
    hdmi = plat.request("hdmi_in", 0, dir="-")
    assert hdmi.d0.invert == (True,)
    assert hdmi.clk.invert == (True,)
    usb = plat.request("usb_hax", 0, dir="-")
    assert usb.d.invert == (False,)


def test_registry():
    from usb2soft.platforms import get_platform
    plat = get_platform("netv2", variant="a7-100")
    assert plat.device == "xc7a100t"
    with pytest.raises(KeyError):
        get_platform("nonesuch")


def test_vivado_constraints_include_config_voltage_and_compression(tmp_path):
    from usb2soft.platforms.netv2 import NeTV2Platform
    plat = NeTV2Platform(variant="a7-35")
    plan = plat.build(_Blink(), name="t", build_dir=str(tmp_path), do_build=False)
    xdc = plan.files["t.xdc"]
    assert "set_property CFGBVS VCCO [current_design]" in xdc
    assert "set_property CONFIG_VOLTAGE 3.3 [current_design]" in xdc
    assert "BITSTREAM.GENERAL.COMPRESS TRUE" in plan.files["t.tcl"]


def test_xray_toolchain_gets_no_vivado_only_constraints(tmp_path):
    from usb2soft.platforms.netv2 import NeTV2Platform
    plat = NeTV2Platform(variant="a7-35", toolchain="Xray")
    plan = plat.build(_Blink(), name="t", build_dir=str(tmp_path), do_build=False)
    assert "current_design" not in plan.files["t.xdc"]
    assert "LOC K21" in plan.files["t.xdc"]
