"""Internal-loopback "wire": expand the encoder's 4 line bits per cycle into the 16 samples per
cycle the CDR expects (each bit x4, idle = J), with a fixed sub-sample phase rotation so the
CDR's phase pointer is exercised at every offset. Synthesisable; runs in the shared 120 MHz
domain of the link-test applet."""
from amaranth import Elaboratable, Module, Signal, Cat, Mux

__all__ = ["SampleExpander"]


class SampleExpander(Elaboratable):
    def __init__(self, *, samples_per_ui=4, phase_shift=0, domain="sync"):
        self.S = samples_per_ui
        self.phase_shift = phase_shift % samples_per_ui
        self.domain = domain
        self.line = Signal(4)
        self.oe = Signal(4)
        self.samples = Signal(4 * samples_per_ui)

    def elaborate(self, platform):
        m = Module()
        S = self.S
        W = 4 * S
        # Level per bit: driven bit or idle J.
        levels = [Mux(self.oe[i], self.line[i], 1) for i in range(4)]
        expanded = Cat(*[lvl for lvl in levels for _ in range(S)])      # bit 0 earliest
        k = self.phase_shift
        if k == 0:
            m.d.comb += self.samples.eq(expanded)
        else:
            carry = Signal(k)
            m.d[self.domain] += carry.eq(expanded[W - k:])
            m.d.comb += self.samples.eq(Cat(carry, expanded[:W - k]))
        return m
