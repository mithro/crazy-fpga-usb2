"""Clock domains for NeTV2 bring-up applets: 50 MHz crystal -> 60 MHz ``usb`` (aliased as
``sync``) and 300 MHz ``idelay_ref``. The PHY applets (P2+) add the MMCM stage from
docs/clocking.md on top of the 60 MHz output.
"""
from amaranth import Elaboratable, Module, Signal, ClockDomain
from amaranth.lib import io
from amaranth.lib.cdc import ResetSynchronizer

from .params import solve
from .xilinx import PLLE2

__all__ = ["NeTV2BringupClocks"]


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
