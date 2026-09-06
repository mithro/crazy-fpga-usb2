"""Board UART console: LUNA UARTTransmitter fed by a TextReporter."""
from amaranth import Elaboratable, Module, Signal
from luna.gateware.interface.uart import UARTTransmitter
from .report import TextReporter

__all__ = ["Console"]


class Console(Elaboratable):
    def __init__(self, segments, *, divisor, domain="sync"):
        if domain != "sync":
            raise ValueError("LUNA's UARTTransmitter uses the sync domain")
        self.reporter = TextReporter(segments, domain=domain)
        self.divisor = divisor
        self.domain = domain
        self.trigger = self.reporter.trigger
        self.busy = self.reporter.busy      # a line is being handed to the transmitter
        self.idle = Signal()                # transmitter has finished shifting the last byte
        self.tx = Signal(init=1)

    def elaborate(self, platform):
        m = Module()
        m.submodules.reporter = self.reporter
        m.submodules.uart = uart = UARTTransmitter(divisor=self.divisor)
        m.d.comb += [
            uart.stream.payload.eq(self.reporter.stream.payload),
            uart.stream.valid.eq(self.reporter.stream.valid),
            self.reporter.stream.ready.eq(uart.stream.ready),
            self.tx.eq(uart.tx),
            self.idle.eq(uart.idle),
        ]
        return m
