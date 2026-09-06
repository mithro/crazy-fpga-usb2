"""``SoftUTMIPHY``: the UTMI face of the soft PHY (spec §4.6).

It owns an ``RxPath`` and a ``TxPath`` and exposes the attribute set LUNA's ``GatewarePHY``
does, so ``USBDevice(bus=phy)`` works unchanged (see ``usb2soft.luna.SoftPHYUSBDevice`` for the
one thing LUNA assumes about bare UTMI objects). The control plane:

- ``op_mode`` 0 (normal): bytes go through the packet encoder (SYNC, stuffing, NRZI, EOP).
- ``op_mode`` 1 (non-driving): output enables forced off, ``tx_ready`` low.
- ``op_mode`` 2 (chirp / raw, "disable bit stuffing and NRZI"): while ``tx_valid`` the line is
  driven to a constant K, bypassing the encoder — this is how LUNA sends its 2 ms device chirp.
- ``op_mode`` 3: treated as normal (LUNA never uses it).
- ``line_state``: from the ``LineStateSynthesiser`` when one is attached and active, otherwise the
  squelch-derived ``HSLineState``.
- ``session_end`` = 0 (VBUS assumed present, as luna-boards does for the NeTV2), ``vbus_valid`` = 1.

The wire side is ``samples[16]`` (in, ``rx_cdr`` domain) and ``line[4]``/``oe[4]`` (out, tx
domain). On the loopback platforms the wires do not echo our own transmission, so no half-duplex
masking of ``rx_active`` is needed; a real bus would need it.
"""
from amaranth import Elaboratable, Module, Signal, Const, Cat, Mux
from amaranth.lib.cdc import FFSynchronizer

from ..rx import RxPath
from ..tx import TxPath
from .linestate import HSLineState

__all__ = ["SoftUTMIPHY", "OP_NORMAL", "OP_NON_DRIVING", "OP_CHIRP"]

OP_NORMAL, OP_NON_DRIVING, OP_CHIRP = 0, 1, 2


class SoftUTMIPHY(Elaboratable):
    def __init__(self, *, samples_per_ui=4, usb_domain="usb", cdr_domain="rx_cdr", tx_domain="tx_cdr",
                 synthesiser=None):
        self.usb_domain, self.cdr_domain, self.tx_domain = usb_domain, cdr_domain, tx_domain
        self.rx = RxPath(samples_per_ui=samples_per_ui, samples_per_word=4 * samples_per_ui,
                         cdr_domain=cdr_domain, usb_domain=usb_domain)
        self.tx = TxPath(tx_domain=tx_domain, usb_domain=usb_domain)
        self.hs_line_state = HSLineState(cdr_domain=cdr_domain, usb_domain=usb_domain)
        self.synthesiser = synthesiser

        # UTMI, LUNA attribute names (see luna.gateware.interface.utmi.UTMIInterface).
        self.tx_data = Signal(8)
        self.tx_valid = Signal()
        self.tx_ready = Signal()
        self.rx_data = self.rx.rx_data
        self.rx_valid = self.rx.rx_valid
        self.rx_active = self.rx.rx_active
        self.rx_error = self.rx.rx_error
        self.line_state = Signal(2)
        self.vbus_valid = Const(1)
        self.session_valid = Const(1)
        self.session_end = Const(0)
        self.host_disconnect = Const(0)
        self.id_digital = Const(0)
        self.xcvr_select = Signal(2)
        self.term_select = Signal()
        self.op_mode = Signal(2)
        self.suspend = Signal()
        self.id_pullup = Signal()
        self.dm_pulldown = Signal()
        self.dp_pulldown = Signal()
        self.chrg_vbus = Signal()
        self.dischrg_vbus = Signal()
        self.use_external_vbus_indicator = Signal()

        # Wire side.
        self.samples = self.rx.samples
        self.line = Signal(4)
        self.oe = Signal(4)

    def elaborate(self, platform):
        m = Module()
        m.submodules.rx = self.rx
        m.submodules.tx = self.tx
        m.submodules.hs_ls = self.hs_line_state

        normal = (self.op_mode == OP_NORMAL) | (self.op_mode == 3)
        chirp = self.op_mode == OP_CHIRP
        m.d.comb += [
            self.tx.tx_data.eq(self.tx_data),
            self.tx.tx_valid.eq(self.tx_valid & normal),
            self.tx_ready.eq(Mux(normal, self.tx.tx_ready, chirp)),
        ]

        # Raw chirp drive in the tx domain: constant K (line low) while op_mode 2 and tx_valid.
        ctl = Signal(2)
        m.submodules.ctl_sync = FFSynchronizer(Cat(chirp & self.tx_valid, self.op_mode == OP_NON_DRIVING),
                                               ctl, o_domain=self.tx_domain)
        drive_k, non_driving = ctl[0], ctl[1]
        with m.If(drive_k):
            m.d.comb += [self.line.eq(0), self.oe.eq(0xF)]
        with m.Elif(non_driving):
            m.d.comb += [self.line.eq(0xF), self.oe.eq(0)]
        with m.Else():
            m.d.comb += [self.line.eq(self.tx.line), self.oe.eq(self.tx.oe)]

        # Line state.
        W = len(self.rx.samples)
        m.d.comb += [
            self.hs_line_state.activity.eq(self.rx.cdr.activity),
            self.hs_line_state.level.eq(self.rx.samples[W - 1]),
        ]
        if self.synthesiser is not None:
            m.submodules.synth = synth = self.synthesiser
            m.d.comb += [
                synth.op_mode.eq(self.op_mode),
                synth.tx_valid.eq(self.tx_valid),
                synth.squelch_line_state.eq(self.hs_line_state.line_state),
                self.line_state.eq(synth.line_state),
            ]
        else:
            m.d.comb += self.line_state.eq(self.hs_line_state.line_state)
        return m
