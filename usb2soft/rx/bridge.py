"""Move decoder events into the UTMI clock domain and drive the UTMI receive signals.

One FIFO entry is written per decoder cycle that produced anything: ``data[8]``, ``data_valid``,
``start``, ``end``, ``error``. On the UTMI side START raises ``rx_active``; a data entry pulses
``rx_valid`` with ``rx_data``; END/ERROR lower ``rx_active`` (ERROR also pulses ``rx_error``).
UTMI requires ``rx_active`` to be high before the first ``rx_valid``; that holds because START is
always a separate, earlier entry (at least nine line bits precede the first complete byte).
The 16-entry FIFO is also the elastic buffer that absorbs a faster host's byte rate.
"""
from amaranth import Elaboratable, Module, Signal, Cat
from amaranth.lib.fifo import AsyncFIFOBuffered

from .decoder import Event

__all__ = ["RxUTMIBridge"]


class RxUTMIBridge(Elaboratable):
    def __init__(self, *, decoder, cdr_domain, usb_domain, depth=16):
        self.decoder = decoder
        self.cdr_domain = cdr_domain
        self.usb_domain = usb_domain
        self.depth = depth
        self.rx_active = Signal()
        self.rx_valid = Signal()
        self.rx_data = Signal(8)
        self.rx_error = Signal()
        self.overflow = Signal()     # sticky, for debug counters

    def elaborate(self, platform):
        m = Module()
        dec = self.decoder
        m.submodules.fifo = fifo = AsyncFIFOBuffered(width=12, depth=self.depth,
                                                     w_domain=self.cdr_domain, r_domain=self.usb_domain)
        start = dec.event_valid & (dec.event == Event.START)
        end = dec.event_valid & (dec.event == Event.END)
        error = dec.event_valid & (dec.event == Event.ERROR)
        m.d.comb += [
            fifo.w_data.eq(Cat(dec.data, dec.data_valid, start, end, error)),
            fifo.w_en.eq(dec.event_valid | dec.data_valid),
        ]
        with m.If(fifo.w_en & ~fifo.w_rdy):
            m.d[self.cdr_domain] += self.overflow.eq(1)

        r_data = fifo.r_data[0:8]
        r_dv, r_start, r_end, r_err = fifo.r_data[8], fifo.r_data[9], fifo.r_data[10], fifo.r_data[11]
        m.d.comb += fifo.r_en.eq(1)
        usb = m.d[self.usb_domain]
        usb += [self.rx_valid.eq(0), self.rx_error.eq(0)]
        with m.If(fifo.r_rdy):
            with m.If(r_start):
                usb += self.rx_active.eq(1)
            with m.If(r_dv):
                usb += [self.rx_valid.eq(1), self.rx_data.eq(r_data)]
            with m.If(r_end | r_err):
                usb += self.rx_active.eq(0)
            with m.If(r_err):
                usb += self.rx_error.eq(1)
        return m
