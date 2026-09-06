import re
from amaranth import Module, Signal, Elaboratable
from amaranth.back import rtlil

from usb2soft.clock.params import solve
from usb2soft.clock.xilinx import PLLE2, MMCME2


class _Top(Elaboratable):
    def __init__(self, kind):
        self.kind = kind
        self.clkin = Signal()
        self.out = Signal()

    def elaborate(self, platform):
        m = Module()
        if self.kind == "pll":
            sol = solve(kind="pll", speed="-2", fin=50e6, vco=1200e6, outputs={"usb": 60e6, "idelay_ref": 300e6})
            m.submodules.pll = pll = PLLE2(sol, clkin=self.clkin)
            m.d.comb += self.out.eq(pll.clocks["usb"] ^ pll.clocks["idelay_ref"] ^ pll.locked)
        else:
            sol = solve(kind="mmcm", speed="-2", fin=60e6, vco=960e6,
                        outputs={"rx_io": 480e6, "rx_io90": (480e6, 90.0), "usb": 60e6})
            m.submodules.mmcm = mmcm = MMCME2(sol, clkin=self.clkin, fine_ps=True, ps_clk=self.clkin)
            m.d.comb += self.out.eq(mmcm.clocks["rx_io"] ^ mmcm.clocks["rx_io90"] ^ mmcm.clocks["usb"] ^ mmcm.locked)
        return m


def _rtlil(kind):
    top = _Top(kind)
    return rtlil.convert(top, ports=[top.clkin, top.out])


def test_pll_parameters_and_bufgs():
    text = _rtlil("pll")
    assert "PLLE2_ADV" in text
    assert re.search(r"CLKFBOUT_MULT.*24\b", text)
    assert re.search(r"CLKOUT0_DIVIDE.*20\b", text)
    assert re.search(r"CLKOUT1_DIVIDE.*4\b", text)
    assert re.search(r"CLKIN1_PERIOD", text)
    assert text.count("\\BUFG") >= 2


def test_mmcm_fine_ps_and_phase():
    text = _rtlil("mmcm")
    assert "MMCME2_ADV" in text
    assert re.search(r"CLKFBOUT_MULT_F", text)
    assert re.search(r"CLKOUT1_PHASE.*90", text)
    assert "USE_FINE_PS" in text and "TRUE" in text
    # Feedback goes through a BUFG so fabric clocks are phase-aligned to the input.
    assert text.count("\\BUFG") >= 4
