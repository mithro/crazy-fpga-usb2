"""Clock domains for NeTV2 bring-up applets: 50 MHz crystal -> 60 MHz ``usb`` (aliased as
``sync``) and 300 MHz ``idelay_ref``. The PHY applets (P2+) add the MMCM stage from
docs/clocking.md on top of the 60 MHz output.
"""
from amaranth import Elaboratable, Module, Signal, ClockDomain
from amaranth.lib import io
from amaranth.lib.cdc import ResetSynchronizer

from .params import solve
from .xilinx import PLLE2, MMCME2

__all__ = ["NeTV2BringupClocks", "NeTV2PhyClocks"]


class NeTV2BringupClocks(Elaboratable):
    """Creates the ``sync``/``usb``/``idelay_ref`` ClockDomain objects; the *top-level* module
    must attach them (``m.domains += clocks.domains``) so they are defined where they are used
    (Amaranth 0.6 will reject domains defined in a submodule and used in the parent)."""
    OUTPUTS = {"usb": 60e6, "idelay_ref": 300e6}
    VCO = 1200e6          # docs/clocking.md

    def __init__(self):
        self.locked = Signal()
        self.cd_sync = ClockDomain("sync")
        self.cd_usb = ClockDomain("usb")
        self.cd_idelay_ref = ClockDomain("idelay_ref")
        self.domains = [self.cd_sync, self.cd_usb, self.cd_idelay_ref]

    def elaborate(self, platform):
        m = Module()
        clk50_port = platform.request("clk50", 0, dir="-")
        m.submodules.clk50_buf = clk50_buf = io.Buffer("i", clk50_port)

        sol = solve(kind="pll", speed=f"-{platform.speed}", fin=50e6, vco=self.VCO, outputs=self.OUTPUTS)
        m.submodules.pll = pll = PLLE2(sol, clkin=clk50_buf.i)

        m.d.comb += [
            self.cd_usb.clk.eq(pll.clocks["usb"]),
            self.cd_sync.clk.eq(pll.clocks["usb"]),
            self.cd_idelay_ref.clk.eq(pll.clocks["idelay_ref"]),
            self.locked.eq(pll.locked),
        ]
        m.submodules.rst_usb = ResetSynchronizer(~pll.locked, domain="usb")
        m.submodules.rst_idelay = ResetSynchronizer(~pll.locked, domain="idelay_ref")
        m.submodules.rst_sync = ResetSynchronizer(~pll.locked, domain="sync")
        return m


class NeTV2PhyClocks(Elaboratable):
    """Full PHY clock plan (docs/clocking.md): PLL 50 -> 60 MHz + 300 MHz, then MMCM 60 -> 960 MHz
    VCO -> 480 (``rx_io``), 240 (``tx_io``), 120 (``rx_cdr``/``tx_cdr``), 60 (``usb``/``sync``).
    All MMCM outputs carry USE_FINE_PS so P6 can slew the whole tree; until then PSEN is 0.
    Attach ``domains`` at the top level."""
    PLL_OUTPUTS = {"mmcm_ref": 60e6, "idelay_ref": 300e6}
    PLL_VCO = 1200e6
    MMCM_OUTPUTS = {"rx_io": 480e6, "tx_io": 240e6, "cdr": 120e6, "usb": 60e6}
    MMCM_VCO = 960e6

    # Optional second PLL giving a transmit clock offset from the 120 MHz receive clock, for the
    # asynchronous internal loopback test: 50 MHz * mult / (divclk * divide).
    ASYNC_TX = {
        "fast": dict(divclk=2, mult=53, divide=11),   # 1325 MHz / 11 = 120.4545 MHz, +3788 ppm
        "slow": dict(divclk=2, mult=43, divide=9),    # 1075 MHz / 9  = 119.4444 MHz, -4630 ppm
    }

    def __init__(self, *, async_tx=None):
        self.async_tx = async_tx
        self.locked = Signal()
        self.ps_en = Signal()
        self.ps_incdec = Signal()
        self.ps_done = Signal()
        names = ["sync", "usb", "rx_cdr", "tx_cdr", "rx_io", "tx_io", "idelay_ref"]
        if async_tx:
            names.append("tx_async")
        self.cd = {n: ClockDomain(n) for n in names}
        self.domains = list(self.cd.values())

    @classmethod
    def async_tx_ppm(cls, key):
        cfg = cls.ASYNC_TX[key]
        f = 50e6 * cfg["mult"] / (cfg["divclk"] * cfg["divide"])
        return (f / 120e6 - 1) * 1e6

    def elaborate(self, platform):
        m = Module()
        clk50_port = platform.request("clk50", 0, dir="-")
        m.submodules.clk50_buf = clk50_buf = io.Buffer("i", clk50_port)
        speed = f"-{platform.speed}"

        pll_sol = solve(kind="pll", speed=speed, fin=50e6, vco=self.PLL_VCO, outputs=self.PLL_OUTPUTS)
        m.submodules.pll = pll = PLLE2(pll_sol, clkin=clk50_buf.i)
        mmcm_sol = solve(kind="mmcm", speed=speed, fin=60e6, vco=self.MMCM_VCO, outputs=self.MMCM_OUTPUTS)
        m.submodules.mmcm = mmcm = MMCME2(mmcm_sol, clkin=pll.clocks["mmcm_ref"], reset=~pll.locked,
                                          fine_ps=True, ps_clk=self.cd["tx_io"].clk,
                                          ps_en=self.ps_en, ps_incdec=self.ps_incdec, ps_done=self.ps_done)
        m.d.comb += [
            self.cd["rx_io"].clk.eq(mmcm.clocks["rx_io"]),
            self.cd["tx_io"].clk.eq(mmcm.clocks["tx_io"]),
            self.cd["rx_cdr"].clk.eq(mmcm.clocks["cdr"]),
            self.cd["tx_cdr"].clk.eq(mmcm.clocks["cdr"]),
            self.cd["usb"].clk.eq(mmcm.clocks["usb"]),
            self.cd["sync"].clk.eq(mmcm.clocks["usb"]),
            self.cd["idelay_ref"].clk.eq(pll.clocks["idelay_ref"]),
            self.locked.eq(pll.locked & mmcm.locked),
        ]
        locked_all = self.locked
        if self.async_tx:
            from .params import ClockSolution, OutputSetting
            cfg = self.ASYNC_TX[self.async_tx]
            vco = 50e6 * cfg["mult"] / cfg["divclk"]
            sol2 = ClockSolution(kind="pll", fin=50e6, divclk=cfg["divclk"], mult=cfg["mult"], vco=vco,
                                 outputs={"tx_async": OutputSetting(frequency=vco / cfg["divide"],
                                                                    divide=cfg["divide"])})
            m.submodules.pll2 = pll2 = PLLE2(sol2, clkin=clk50_buf.i)
            m.d.comb += self.cd["tx_async"].clk.eq(pll2.clocks["tx_async"])
            locked_all = self.locked & pll2.locked
        for name in self.cd:
            arst = ~pll.locked if name == "idelay_ref" else ~locked_all
            m.submodules[f"rst_{name}"] = ResetSynchronizer(arst, domain=name)
        return m
