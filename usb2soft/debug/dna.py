"""Read the 57-bit device DNA once after reset.

``DNAReader`` is the FSM (simulable); ``DNAPort`` instantiates the ``DNA_PORT`` primitive and
wires it to a reader. Clock the port from a domain at or below 100 MHz (DS181 FDNA_CLK).

Protocol (UG470 "Device DNA", DNA_PORT timing): with READ high on a CLK edge the DNA is loaded
and DOUT presents bit 56 from the next cycle on; each CLK edge with SHIFT high presents the
next lower bit. DIN is shifted in at the LSB end and is tied to 0.
"""
from amaranth import Elaboratable, Module, Signal, Instance, Cat, Const, ClockSignal

__all__ = ["DNAReader", "DNAPort", "DNA_BITS"]

DNA_BITS = 57


class DNAReader(Elaboratable):
    def __init__(self, domain="sync"):
        self.domain = domain
        # Primitive-facing ports.
        self.port_dout = Signal()
        self.port_read = Signal()
        self.port_shift = Signal()
        # Result.
        self.dna = Signal(DNA_BITS)
        self.valid = Signal()

    def elaborate(self, platform):
        m = Module()
        count = Signal(range(DNA_BITS + 1))
        with m.FSM(domain=self.domain):
            with m.State("READ"):
                m.d.comb += self.port_read.eq(1)
                m.d[self.domain] += count.eq(0)
                m.next = "SHIFT"
            with m.State("SHIFT"):
                # DOUT already shows bit 56 after READ; capture it, then shift for the rest.
                m.d[self.domain] += [
                    self.dna.eq(Cat(self.port_dout, self.dna[:-1])),
                    count.eq(count + 1),
                ]
                with m.If(count == DNA_BITS - 1):
                    m.next = "DONE"
                with m.Else():
                    m.d.comb += self.port_shift.eq(1)
            with m.State("DONE"):
                m.d[self.domain] += self.valid.eq(1)
        return m


class DNAPort(Elaboratable):
    """DNA_PORT primitive plus reader; exposes ``dna`` and ``valid``."""
    def __init__(self, domain="sync"):
        self.domain = domain
        self.reader = DNAReader(domain=domain)
        self.dna = self.reader.dna
        self.valid = self.reader.valid

    def elaborate(self, platform):
        m = Module()
        m.submodules.reader = self.reader
        m.submodules.port = Instance("DNA_PORT",
            p_SIM_DNA_VALUE=Const(0, DNA_BITS),
            i_CLK=ClockSignal(self.domain),
            i_DIN=Const(0),
            i_READ=self.reader.port_read,
            i_SHIFT=self.reader.port_shift,
            o_DOUT=self.reader.port_dout,
        )
        return m
