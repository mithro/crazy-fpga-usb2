"""UTMI transmit side: bytes from the 60 MHz ``usb`` domain into the encoder's domain.

A shallow ``AsyncFIFOBuffered`` (depth 4) carries ``(byte, end)`` entries so ``tx_ready`` still
follows the real line rate instead of accepting a whole packet ahead of transmission (LUNA's
inter-packet timers start when it drops ``tx_valid``). The end of a packet is a separate entry
written when ``tx_valid`` falls; if the FIFO is full at that moment the marker is held pending
and written as soon as there is room (``tx_ready`` stays low meanwhile).
"""
from amaranth import Elaboratable, Module, Signal, Cat, Const
from amaranth.lib.fifo import AsyncFIFOBuffered

__all__ = ["TxUTMIBridge"]


class TxUTMIBridge(Elaboratable):
    def __init__(self, *, encoder, usb_domain, tx_domain, depth=4):
        self.encoder = encoder
        self.usb_domain = usb_domain
        self.tx_domain = tx_domain
        self.depth = depth
        self.tx_data = Signal(8)
        self.tx_valid = Signal()
        self.tx_ready = Signal()

    def elaborate(self, platform):
        m = Module()
        enc = self.encoder
        m.submodules.fifo = fifo = AsyncFIFOBuffered(width=9, depth=self.depth,
                                                     w_domain=self.usb_domain, r_domain=self.tx_domain)
        usb = m.d[self.usb_domain]
        prev_valid = Signal()
        pending_end = Signal()
        usb += prev_valid.eq(self.tx_valid)
        ended = (prev_valid & ~self.tx_valid) | pending_end

        with m.If(ended):
            # The end marker has priority over new data; a new packet cannot start before it.
            m.d.comb += [fifo.w_data.eq(Cat(Const(0, 8), Const(1))), fifo.w_en.eq(fifo.w_rdy)]
            usb += pending_end.eq(~fifo.w_rdy)
        with m.Else():
            m.d.comb += [
                fifo.w_data.eq(Cat(self.tx_data, Const(0))),
                fifo.w_en.eq(self.tx_valid & fifo.w_rdy),
                self.tx_ready.eq(self.tx_valid & fifo.w_rdy),
            ]

        m.d.comb += [
            enc.byte_valid.eq(fifo.r_rdy),
            enc.byte.eq(fifo.r_data[0:8]),
            enc.byte_end.eq(fifo.r_data[8]),
            fifo.r_en.eq(enc.byte_ready),
        ]
        return m
