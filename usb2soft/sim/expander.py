"""Internal-loopback "wires" for the link-test applet (synthesisable).

``SampleExpander``: expand the encoder's 4 line bits per cycle into the 16 samples per cycle the
CDR expects (each bit x4, idle = J), with a fixed sub-sample phase rotation so the CDR's phase
pointer is exercised at every offset; runs in the shared 120 MHz domain.

``AsyncResampler``: the same, but from a transmit clock with a real frequency offset (the P4 plan's
"slip_period" expander was replaced by this: a second PLL plus single-sample drop/repeat gives the
CDR a genuine ±4000 ppm source instead of a synthetic periodic slip)."""
from amaranth import Elaboratable, Module, Signal, Cat, Mux

__all__ = ["SampleExpander", "AsyncResampler"]


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


class AsyncResampler(Elaboratable):
    """Bridge the encoder's 4-bit words from an asynchronous transmit clock into the receiver's
    16-samples-per-cycle stream by dropping or repeating *single samples*. Seen from the CDR this
    is indistinguishable from a host whose bit clock is offset by the ratio of the two clocks
    (e.g. +3788 ppm when the TX PLL runs at 120.4545 MHz against 120 MHz).

    Two transmit cycles (32 samples) form one crossing-FIFO entry, so the 120 MHz reader can pop
    up to 32 samples per cycle and always keeps up with a faster writer. The reader keeps a
    96-sample shift buffer; the regulated quantity is the total backlog (buffer fill + 32 x FIFO
    level), primed to ``TARGET``. Each cycle consumes 16 samples normally, 17 (drop one) when the
    backlog runs high and 15 (repeat one) when it runs low. Steps are at least ``HOLDOFF`` cycles
    apart (one sample per 32 UI = up to +/-7800 ppm) so they are spread like a real frequency
    offset rather than bunched. Synthesisable."""
    BUF = 96
    TARGET = 96        # keeps >= 24 valid samples in the buffer at the bottom of the band
    HYST = 24
    HOLDOFF = 8

    def __init__(self, *, samples_per_ui=4, in_domain="tx_async", out_domain="rx_cdr"):
        self.S = samples_per_ui
        self.in_domain = in_domain
        self.out_domain = out_domain
        self.line = Signal(4)          # in_domain
        self.oe = Signal(4)
        self.samples = Signal(4 * samples_per_ui, init=(1 << (4 * samples_per_ui)) - 1)   # out_domain, registered
        self.drops = Signal()          # strobes (out_domain) for statistics
        self.dups = Signal()
        self.overflow = Signal()       # sticky: crossing FIFO overran (must never happen)

    def elaborate(self, platform):
        from amaranth.lib.fifo import AsyncFIFOBuffered
        m = Module()
        S = self.S
        W = 4 * S
        E = 2 * W                      # samples per FIFO entry
        m.submodules.fifo = fifo = AsyncFIFOBuffered(width=E, depth=8, w_domain=self.in_domain,
                                                     r_domain=self.out_domain)
        levels = [Mux(self.oe[i], self.line[i], 1) for i in range(4)]
        word = Cat(*[lvl for lvl in levels for _ in range(S)])
        half = Signal()
        first = Signal(W)
        m.d[self.in_domain] += [half.eq(~half), first.eq(word)]
        m.d.comb += [fifo.w_data.eq(Cat(first, word)), fifo.w_en.eq(half)]
        with m.If(half & ~fifo.w_rdy):
            m.d[self.in_domain] += self.overflow.eq(1)

        out = m.d[self.out_domain]
        buf = Signal(self.BUF)
        fill = Signal(range(self.BUF + 1))
        running = Signal()
        pop = Signal()
        consume = Signal(range(W + 2))
        # Pipelined so that nothing combinational sits between this block and the CDR: the backlog
        # (one cycle stale) feeds registered drop/dup decisions, and the output word is registered.
        total = Signal(range(self.BUF + E * fifo.depth + 1))
        m.d.comb += [pop.eq(fifo.r_rdy & (fill <= self.BUF - E)), fifo.r_en.eq(pop)]
        out += total.eq(fill + (fifo.r_level * E))

        holdoff = Signal(range(self.HOLDOFF))
        armed = holdoff == 0
        drop_next = running & armed & (total >= self.TARGET + self.HYST)
        dup_next = running & armed & (total <= self.TARGET - self.HYST)
        drop = Signal()
        dup = Signal()
        out += [drop.eq(drop_next), dup.eq(dup_next)]
        with m.If(drop_next | dup_next):
            out += holdoff.eq(self.HOLDOFF - 1)
        with m.Elif(~armed):
            out += holdoff.eq(holdoff - 1)
        with m.If(~running):
            m.d.comb += consume.eq(0)
            out += self.samples.eq((1 << W) - 1)
            with m.If(total >= self.TARGET):
                out += running.eq(1)
        with m.Elif(drop):
            m.d.comb += consume.eq(W + 1)
            out += self.samples.eq(buf[1:W + 1])
        with m.Elif(dup):
            m.d.comb += consume.eq(W - 1)
            out += self.samples.eq(Cat(buf[0], buf[0:W - 1]))
        with m.Else():
            m.d.comb += consume.eq(W)
            out += self.samples.eq(buf[0:W])
        appended = Signal(self.BUF + E)
        m.d.comb += appended.eq(buf | Mux(pop, fifo.r_data << fill, 0))
        out += [
            buf.eq((appended >> consume)[:self.BUF]),
            fill.eq(fill + Mux(pop, E, 0) - consume),
            self.drops.eq(drop), self.dups.eq(dup),
        ]
        return m
