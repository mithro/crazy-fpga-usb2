from amaranth import Module, Signal, Elaboratable

from usb2soft.platforms.netv2 import NeTV2Platform
from usb2soft.clock.netv2 import NeTV2PhyClocks


class _Top(Elaboratable):
    def elaborate(self, platform):
        m = Module()
        m.submodules.clocks = clocks = NeTV2PhyClocks()
        m.domains += clocks.domains
        for d in ("usb", "rx_cdr", "tx_cdr", "rx_io", "tx_io", "idelay_ref"):
            c = Signal(2, name=f"c_{d}")
            m.d[d] += c.eq(c + 1)
        return m


def test_phy_clocks_generate_pll_and_mmcm(tmp_path):
    plat = NeTV2Platform(variant="a7-35")
    plan = plat.build(_Top(), name="t", build_dir=str(tmp_path), do_build=False)
    v = plan.files["t.v"]
    assert v.count("PLLE2_ADV") >= 1 and v.count("MMCME2_ADV") >= 1
    assert ".CLKFBOUT_MULT_F(16.0)" in v.replace(" ", "") or "CLKFBOUT_MULT_F" in v
    assert "USE_FINE_PS" in v and "TRUE" in v
    # 480 MHz output is CLKOUT0 -> divide 2 in the fractional slot
    assert "CLKOUT0_DIVIDE_F" in v
    assert v.count("BUFG") >= 7        # 2 fb + 2 pll outs + 4 mmcm outs
