"""USB high-speed transmit path: packet encoder, UTMI bridge."""
from amaranth import Elaboratable, Module

from .encoder import PacketEncoder
from .bridge import TxUTMIBridge

__all__ = ["TxPath", "PacketEncoder", "TxUTMIBridge"]


class TxPath(Elaboratable):
    """UTMI tx_* (usb domain) -> 4 line bits + 4 output enables per cycle (tx domain)."""
    def __init__(self, *, tx_domain="tx_cdr", usb_domain="usb"):
        self.encoder = PacketEncoder(domain=tx_domain)
        self.bridge = TxUTMIBridge(encoder=self.encoder, usb_domain=usb_domain, tx_domain=tx_domain)
        self.tx_data = self.bridge.tx_data
        self.tx_valid = self.bridge.tx_valid
        self.tx_ready = self.bridge.tx_ready
        self.line = self.encoder.line
        self.oe = self.encoder.oe
        self.busy = self.encoder.busy
        self.underrun = self.encoder.underrun

    def elaborate(self, platform):
        m = Module()
        m.submodules.encoder = self.encoder
        m.submodules.bridge = self.bridge
        return m
