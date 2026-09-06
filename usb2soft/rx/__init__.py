"""USB high-speed receive path: oversampling CDR, packet decoder, UTMI bridge."""
from amaranth import Elaboratable, Module

from .cdr import OversamplingCDR
from .decoder import PacketDecoder, Event
from .bridge import RxUTMIBridge

__all__ = ["RxPath", "OversamplingCDR", "PacketDecoder", "RxUTMIBridge", "Event"]


class RxPath(Elaboratable):
    """samples (cdr domain) -> UTMI rx signals (usb domain)."""
    def __init__(self, *, samples_per_ui=4, samples_per_word=16, cdr_domain="rx_cdr",
                 usb_domain="usb", track_threshold=3):
        self.cdr = OversamplingCDR(samples_per_ui=samples_per_ui, samples_per_word=samples_per_word,
                                   track_threshold=track_threshold, domain=cdr_domain)
        self.decoder = PacketDecoder(max_bits=self.cdr.B + 1, domain=cdr_domain)
        self.bridge = RxUTMIBridge(decoder=self.decoder, cdr_domain=cdr_domain, usb_domain=usb_domain)
        self.samples = self.cdr.samples
        self.rx_active = self.bridge.rx_active
        self.rx_valid = self.bridge.rx_valid
        self.rx_data = self.bridge.rx_data
        self.rx_error = self.bridge.rx_error

    def elaborate(self, platform):
        m = Module()
        m.submodules.cdr = self.cdr
        m.submodules.decoder = self.decoder
        m.submodules.bridge = self.bridge
        m.d.comb += [
            self.decoder.bits.eq(self.cdr.bits),
            self.decoder.count.eq(self.cdr.count),
            self.decoder.activity.eq(self.cdr.activity),
            self.cdr.in_packet.eq(self.decoder.in_packet),
        ]
        return m
