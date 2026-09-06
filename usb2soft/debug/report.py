"""Render fixed-format text lines from gateware signals onto a byte stream.

Segments are ``bytes`` literals or ``Hex(signal)`` (width = ceil(len/4) nibbles, MSB first).
The rendered line is produced one byte per accepted transfer; the field signals are sampled
when the trigger is accepted, so a line is internally consistent.
"""
from math import ceil
from amaranth import Elaboratable, Module, Signal, Array
from luna.gateware.stream import StreamInterface

__all__ = ["Hex", "TextReporter"]

_HEX = b"0123456789ABCDEF"


class Hex:
    def __init__(self, signal):
        self.signal = signal
        self.nibbles = ceil(len(signal) / 4)


class TextReporter(Elaboratable):
    def __init__(self, segments, domain="sync"):
        self.domain = domain
        self.segments = list(segments)
        self.trigger = Signal()
        self.busy = Signal()
        self.stream = StreamInterface()
        # Flatten to a list of "byte sources": ("lit", value) or ("nib", signal_index, nibble_index)
        self._plan = []
        self._fields = []
        for seg in self.segments:
            if isinstance(seg, (bytes, bytearray)):
                self._plan.extend(("lit", b) for b in seg)
            elif isinstance(seg, Hex):
                idx = len(self._fields)
                self._fields.append(seg.signal)
                self._plan.extend(("nib", idx, n) for n in reversed(range(seg.nibbles)))
            else:
                raise TypeError(f"unsupported segment {seg!r}")
        if not self._plan:
            raise ValueError("empty report")

    def elaborate(self, platform):
        m = Module()
        latched = [Signal(len(s), name=f"field{i}") for i, s in enumerate(self._fields)]
        pos = Signal(range(len(self._plan) + 1))
        hex_lut = Array(_HEX)

        # Byte for every plan position, as a mux over pos.
        byte_at = []
        for entry in self._plan:
            if entry[0] == "lit":
                byte_at.append(entry[1])
            else:
                _, idx, nib = entry
                # word_select above the MSB is zero-filled for unsigned values (amaranth 0.5),
                # so a 57-bit or 1-bit field needs no padding.
                byte_at.append(hex_lut[latched[idx].word_select(nib, 4)])
        table = Array(byte_at)

        m.d.comb += self.stream.payload.eq(table[pos])
        with m.FSM(domain=self.domain):
            with m.State("IDLE"):
                with m.If(self.trigger):
                    m.d[self.domain] += [l.eq(s) for l, s in zip(latched, self._fields)]
                    m.d[self.domain] += pos.eq(0)
                    m.next = "SEND"
            with m.State("SEND"):
                m.d.comb += [self.busy.eq(1), self.stream.valid.eq(1)]
                with m.If(self.stream.ready):
                    with m.If(pos == len(self._plan) - 1):
                        m.next = "IDLE"
                    with m.Else():
                        m.d[self.domain] += pos.eq(pos + 1)
        return m
