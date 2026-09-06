"""Word-parallel USB HS packet decoder (spec §4.3), running in the CDR domain.

Input per cycle: up to ``max_bits`` recovered line bits (bit 0 earliest) and their count, plus
the CDR's ``activity`` flag. Output per cycle: at most one event (START/END/ERROR) and at most
one data byte. The chain below is combinational across the bits of one word, with the running
state (previous level, zero run, ones run, packer) registered between cycles.
"""
import enum
from amaranth import Elaboratable, Module, Signal, Mux

__all__ = ["PacketDecoder", "Event"]


class Event(enum.IntEnum):
    NONE = 0
    START = 1
    END = 2
    ERROR = 3


SYNC_ZEROS = 8       # minimum decoded zeros before the SYNC's terminating one
STUFF_RUN = 6
EOP_BITS = 7         # bits held since the last byte boundary when the violation arrives


class PacketDecoder(Elaboratable):
    def __init__(self, *, max_bits=5, domain="sync"):
        self.max_bits = max_bits
        self.domain = domain
        self.bits = Signal(max_bits)
        self.count = Signal(range(max_bits + 1))
        self.activity = Signal()

        self.event_valid = Signal()
        self.event = Signal(Event)
        self.data_valid = Signal()
        self.data = Signal(8)
        self.in_packet = Signal()

    def elaborate(self, platform):
        m = Module()
        sync = m.d[self.domain]
        N = self.max_bits

        # Registered state carried between words.
        last_level = Signal(init=1)
        zero_run = Signal(range(SYNC_ZEROS + 1))
        ones_run = Signal(range(STUFF_RUN + 1))
        acc = Signal(8)                     # packer, LSB = earliest data bit
        acc_n = Signal(range(9))
        in_packet = Signal()

        # Chain values, updated bit by bit.
        c_level, c_zero, c_ones, c_acc, c_accn, c_in = last_level, zero_run, ones_run, acc, acc_n, in_packet
        event = Signal(Event)
        data_valid = Signal()
        data = Signal(8)

        for i in range(N):
            valid = Signal(name=f"v{i}")
            m.d.comb += valid.eq(self.count > i)
            raw = self.bits[i]
            d = Signal(name=f"d{i}")
            m.d.comb += d.eq(~(raw ^ c_level))

            n_level = Signal(name=f"lvl{i}")
            n_zero = Signal(range(SYNC_ZEROS + 1), name=f"zr{i}")
            n_ones = Signal(range(STUFF_RUN + 1), name=f"or{i}")
            n_acc = Signal(8, name=f"acc{i}")
            n_accn = Signal(range(9), name=f"accn{i}")
            n_in = Signal(name=f"in{i}")

            start = valid & ~c_in & d & (c_zero >= SYNC_ZEROS)
            stuffed = c_ones == STUFF_RUN
            violation = valid & c_in & stuffed & d
            drop = valid & c_in & stuffed & ~d
            take = valid & c_in & ~stuffed
            byte_done = take & (c_accn == 7)
            shifted = (c_acc >> 1) | (d << 7)

            m.d.comb += [
                n_level.eq(Mux(valid, raw, c_level)),
                # zero run only matters outside a packet
                n_zero.eq(Mux(valid & ~c_in,
                              Mux(d, 0, Mux(c_zero == SYNC_ZEROS, c_zero, c_zero + 1)),
                              Mux(valid & c_in, 0, c_zero))),
                n_in.eq(Mux(start, 1, Mux(violation, 0, c_in))),
                n_ones.eq(Mux(start, 1,                     # the SYNC's final one counts
                              Mux(drop | violation, 0,
                                  Mux(take, Mux(d, c_ones + 1, 0), c_ones)))),
                n_accn.eq(Mux(start | violation, 0,
                              Mux(byte_done, 0, Mux(take, c_accn + 1, c_accn)))),
                n_acc.eq(Mux(take, shifted, c_acc)),
            ]
            with m.If(start):
                m.d.comb += event.eq(Event.START)
            with m.If(violation):
                m.d.comb += event.eq(Mux(c_accn == EOP_BITS, Event.END, Event.ERROR))
            with m.If(byte_done):
                m.d.comb += [data_valid.eq(1), data.eq(shifted)]

            c_level, c_zero, c_ones, c_acc, c_accn, c_in = n_level, n_zero, n_ones, n_acc, n_accn, n_in

        # Loss of activity inside a packet ends it with an error.
        lost = in_packet & ~self.activity & (event == Event.NONE)
        sync += [
            last_level.eq(c_level), zero_run.eq(c_zero), ones_run.eq(c_ones),
            acc.eq(c_acc), acc_n.eq(c_accn),
            in_packet.eq(Mux(lost, 0, c_in)),
            self.event.eq(Mux(lost, Event.ERROR, event)),
            self.event_valid.eq(lost | (event != Event.NONE)),
            self.data_valid.eq(data_valid),
            self.data.eq(data),
        ]
        m.d.comb += self.in_packet.eq(in_packet)
        return m
