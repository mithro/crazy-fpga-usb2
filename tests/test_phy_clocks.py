from amaranth import Module, Signal, Elaboratable

from usb2soft.platforms.netv2 import NeTV2Platform
from usb2soft.clock.netv2 import NeTV2PhyClocks


class _Top(Elaboratable):
    def __init__(self, async_tx=None):
        self.async_tx = async_tx

    def elaborate(self, platform):
        m = Module()
        m.submodules.clocks = clocks = NeTV2PhyClocks(async_tx=self.async_tx)
        m.domains += clocks.domains
        for d in ("usb", "rx_cdr", "tx_cdr", "rx_io", "tx_io", "idelay_ref") + (("tx_async",) if self.async_tx else ()):
            c = Signal(2, name=f"c_{d}")
            m.d[d] += c.eq(c + 1)
        return m


def test_phy_clocks_generate_pll_and_mmcm(tmp_path):
    plat = NeTV2Platform(variant="a7-35")
    plan = plat.build(_Top(), name="t", build_dir=str(tmp_path), do_build=False)
    v = plan.files["t.v"]
    assert v.count("PLLE2_ADV") == 1 and v.count("MMCME2_ADV") == 1
    assert ".CLKFBOUT_MULT_F(16.0)" in v          # 60 MHz x 16 = 960 MHz VCO
    assert ".CLKOUT0_DIVIDE_F(2.0)" in v          # 480 MHz in the fractional slot
    assert '.CLKOUT0_USE_FINE_PS("TRUE")' in v and '.CLKOUT1_USE_FINE_PS("TRUE")' in v
    assert v.count("BUFG") >= 7        # 2 fb + 2 pll outs + 4 mmcm outs


def test_phy_clocks_async_tx_adds_second_pll(tmp_path):
    for key, mult, div in (("fast", 53, 11), ("slow", 43, 9)):
        plat = NeTV2Platform(variant="a7-35")
        plan = plat.build(_Top(async_tx=key), name="t", build_dir=str(tmp_path / key), do_build=False)
        v = plan.files["t.v"]
        assert v.count("PLLE2_ADV") == 2 and v.count("MMCME2_ADV") == 1
        assert f".CLKFBOUT_MULT(32'd{mult})" in v and f".CLKOUT0_DIVIDE(32'd{div})" in v
        assert v.count("BUFG") >= 10        # 3 fb + 2 pll + 4 mmcm + 1 pll2
    assert round(NeTV2PhyClocks.async_tx_ppm("fast")) == 3788
    assert round(NeTV2PhyClocks.async_tx_ppm("slow")) == -4630
