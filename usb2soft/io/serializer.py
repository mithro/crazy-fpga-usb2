"""480 Mbit/s serialiser for one differential pair (spec §4.4 revision 6).

OSERDESE2 in 4:1 DDR mode (CLK 240 MHz, CLKDIV 120 MHz) with ``TRISTATE_WIDTH=4`` so the output
enable is applied per bit; ``D1`` is the first bit on the wire. The complementary output goes
through OBUFTDS. The primitive cannot be simulated; tests inspect the RTLIL.
"""
from amaranth import Elaboratable, Module, Signal, Instance, Const, ClockSignal, ResetSignal

__all__ = ["Serializer"]


class Serializer(Elaboratable):
    def __init__(self, port, *, io_domain="tx_io", div_domain="tx_cdr"):
        self.port = port
        self.io_domain = io_domain
        self.div_domain = div_domain
        self.line = Signal(4)     # bit 0 first on the wire
        self.oe = Signal(4)

    def elaborate(self, platform):
        m = Module()
        oq = Signal()
        tq = Signal()
        inv = bool(self.port.invert[0]) if hasattr(self.port, "invert") else False
        data = ~self.line if inv else self.line
        m.submodules.oserdes = Instance("OSERDESE2",
            p_DATA_RATE_OQ="DDR", p_DATA_RATE_TQ="DDR", p_DATA_WIDTH=4, p_TRISTATE_WIDTH=4,
            p_SERDES_MODE="MASTER", p_TBYTE_CTL="FALSE", p_TBYTE_SRC="FALSE",
            p_INIT_OQ=1, p_INIT_TQ=1, p_SRVAL_OQ=1, p_SRVAL_TQ=1,
            i_CLK=ClockSignal(self.io_domain), i_CLKDIV=ClockSignal(self.div_domain),
            i_RST=ResetSignal(self.div_domain), i_OCE=Const(1), i_TCE=Const(1),
            i_D1=data[0], i_D2=data[1], i_D3=data[2], i_D4=data[3],
            i_D5=Const(0), i_D6=Const(0), i_D7=Const(0), i_D8=Const(0),
            i_T1=~self.oe[0], i_T2=~self.oe[1], i_T3=~self.oe[2], i_T4=~self.oe[3],
            i_SHIFTIN1=Const(0), i_SHIFTIN2=Const(0), i_TBYTEIN=Const(0),
            o_OQ=oq, o_TQ=tq,
        )
        m.submodules.obuf = Instance("OBUFTDS", i_I=oq, i_T=tq, o_O=self.port.p, o_OB=self.port.n)
        return m
