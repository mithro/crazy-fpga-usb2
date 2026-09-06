"""8N1 UART receiver.

``rx`` is the raw line; this module adds a 2-flop synchroniser because the discovery applet
feeds it straight from an IBUFDS. A start bit is accepted only if the line is still low at
mid-bit, which rejects glitches shorter than half a bit. ``valid`` strobes for one cycle with
``data``; ``error`` strobes on a framing error (stop bit not high) and the byte is discarded.
"""
from amaranth import Elaboratable, Module, Signal, Cat
from amaranth.lib.cdc import FFSynchronizer

__all__ = ["UARTReceiver"]


class UARTReceiver(Elaboratable):
    def __init__(self, *, divisor, domain="sync"):
        if divisor < 4:
            raise ValueError("divisor must be at least 4")
        self.divisor = divisor
        self.domain = domain
        self.rx = Signal(init=1)
        self.data = Signal(8)
        self.valid = Signal()
        self.error = Signal()

    def elaborate(self, platform):
        m = Module()
        rx = Signal(init=1)
        m.submodules.sync_rx = FFSynchronizer(self.rx, rx, o_domain=self.domain, init=1)

        baud = Signal(range(self.divisor))
        bit_index = Signal(range(9))
        shift = Signal(8)
        half = (self.divisor // 2) - 1

        # valid/error are registered together with data so a consumer sampling on valid sees the
        # matching byte.
        m.d[self.domain] += [self.valid.eq(0), self.error.eq(0)]

        with m.FSM(domain=self.domain):
            with m.State("IDLE"):
                with m.If(~rx):
                    m.d[self.domain] += baud.eq(half)
                    m.next = "START"
            with m.State("START"):
                with m.If(baud == 0):
                    with m.If(rx):          # glitch: line went back high before mid-bit
                        m.next = "IDLE"
                    with m.Else():
                        m.d[self.domain] += [baud.eq(self.divisor - 1), bit_index.eq(0)]
                        m.next = "DATA"
                with m.Else():
                    m.d[self.domain] += baud.eq(baud - 1)
            with m.State("DATA"):
                with m.If(baud == 0):
                    m.d[self.domain] += [
                        shift.eq(Cat(shift[1:], rx)),
                        baud.eq(self.divisor - 1),
                        bit_index.eq(bit_index + 1),
                    ]
                    with m.If(bit_index == 7):
                        m.next = "STOP"
                with m.Else():
                    m.d[self.domain] += baud.eq(baud - 1)
            with m.State("STOP"):
                with m.If(baud == 0):
                    with m.If(rx):
                        m.d[self.domain] += [self.valid.eq(1), self.data.eq(shift)]
                    with m.Else():
                        m.d[self.domain] += self.error.eq(1)
                    m.next = "IDLE"
                with m.Else():
                    m.d[self.domain] += baud.eq(baud - 1)
        return m
