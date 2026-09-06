"""USB HS packet encoder producing 4 line bits + 4 output-enable bits per cycle (120 MHz).

Byte entries arrive through ``byte_valid``/``byte``/``byte_end`` (``byte_end`` marks the end of
the packet and carries no data); ``byte_ready`` strobes when an entry is consumed.

All bits pass through one data-bit queue in wire order: the 32-bit SYNC (data 000...01), the
bit-stuffed payload, and finally the HS EOP as raw data bits 0 1111111 (no stuffing). Each cycle
up to four bits leave the queue through a parallel NRZI stage (a data 0 toggles the level); the
matching ``oe`` bits let the serialiser turn the driver off on the exact bit after the EOP.

If the source starves mid-packet the encoder closes the packet with an EOP (UTMI semantics:
tx_valid low means end of packet) and sets the sticky ``underrun`` flag.
"""
from amaranth import Elaboratable, Module, Signal, Cat, Const, Mux

__all__ = ["PacketEncoder"]

SYNC_BITS = 32
STUFF_RUN = 6
EOP_DATA = 0b11111110          # bit 0 first: 0 then seven 1s
QUEUE_BITS = 24                # >= 14 (refill threshold) + 10 (worst-case stuffed byte)
REFILL_AT = QUEUE_BITS - 10


class PacketEncoder(Elaboratable):
    def __init__(self, *, domain="sync"):
        self.domain = domain
        self.byte_valid = Signal()
        self.byte = Signal(8)
        self.byte_end = Signal()
        self.byte_ready = Signal()

        self.line = Signal(4, init=0b1111)
        self.oe = Signal(4)
        self.busy = Signal()
        self.underrun = Signal()

    def elaborate(self, platform):
        m = Module()
        sync = m.d[self.domain]

        queue = Signal(QUEUE_BITS)          # data bits, bit 0 leaves first
        fill = Signal(range(QUEUE_BITS + 1))
        ones_run = Signal(range(STUFF_RUN + 1))
        level = Signal(init=1)              # NRZI level after the last emitted bit
        sync_left = Signal(range(SYNC_BITS // 4 + 1))
        ending = Signal()                   # EOP has been appended; drain then idle

        # --- stuff one byte given the carried ones run: up to 10 bits ------------------------
        stuffed = Signal(10)
        stuffed_n = Signal(range(11))
        run_after = Signal(range(STUFF_RUN + 1))
        out_bits = []
        run = ones_run
        for i in range(8):
            b = self.byte[i]
            out_bits.append((b, Const(1)))
            run_i = Signal(range(STUFF_RUN + 1), name=f"run{i}")
            m.d.comb += run_i.eq(Mux(b, run + 1, 0))
            stuff_here = run_i == STUFF_RUN
            out_bits.append((Const(0), stuff_here))
            run_n = Signal(range(STUFF_RUN + 1), name=f"runn{i}")
            m.d.comb += run_n.eq(Mux(stuff_here, 0, run_i))
            run = run_n
        m.d.comb += run_after.eq(run)
        # Compact the (bit, present) pairs into a dense vector: a small prefix-sum over 16 slots.
        pos = Const(0, 4)
        vec = Const(0, 10)
        for b, present in out_bits:
            vec = (vec | Mux(present, (b << pos), 0))[:10]
            pos = (pos + present)[:4]           # keep the shift amount 4 bits wide (max 10)
        m.d.comb += [stuffed.eq(vec), stuffed_n.eq(pos)]

        # --- per-cycle queue bookkeeping ------------------------------------------------------
        take_data = Signal()      # consume a data entry into the queue
        take_end = Signal()       # consume the end marker: append EOP
        add_bits = Signal(10)
        add_n = Signal(range(11))
        drain = Signal(range(5))  # bits leaving this cycle
        m.d.comb += drain.eq(Mux(fill >= 4, 4, fill))

        with m.FSM(domain=self.domain) as fsm:
            m.d.comb += self.busy.eq(~fsm.ongoing("IDLE"))
            with m.State("IDLE"):
                sync += [level.eq(1), fill.eq(0), queue.eq(0), ones_run.eq(0), ending.eq(0)]
                with m.If(self.byte_valid & ~self.byte_end):
                    sync += sync_left.eq(SYNC_BITS // 4)
                    m.next = "SYNC"
                with m.Elif(self.byte_valid & self.byte_end):
                    m.d.comb += self.byte_ready.eq(1)      # stray end marker: swallow it
            with m.State("SYNC"):
                # 31 zeros then a one; emitted straight from the FSM, four bits per cycle.
                m.d.comb += [add_n.eq(4), add_bits.eq(Mux(sync_left == 1, 0b1000, 0))]
                sync += sync_left.eq(sync_left - 1)
                with m.If(sync_left == 1):
                    sync += ones_run.eq(1)                   # the SYNC's final one counts
                    m.next = "DATA"
            with m.State("DATA"):
                with m.If(~ending & (fill <= REFILL_AT)):
                    with m.If(self.byte_valid & ~self.byte_end):
                        m.d.comb += [take_data.eq(1), add_bits.eq(stuffed), add_n.eq(stuffed_n)]
                        sync += ones_run.eq(run_after)
                    with m.Elif(self.byte_valid & self.byte_end):
                        m.d.comb += take_end.eq(1)
                    with m.Elif(fill < 4):
                        # starved: close the packet as if tx_valid had dropped
                        m.d.comb += take_end.eq(1)
                        sync += self.underrun.eq(1)
                with m.If(take_end):
                    m.d.comb += [add_bits.eq(EOP_DATA), add_n.eq(8)]
                    sync += ending.eq(1)
                with m.If(ending & (fill <= 4)):
                    m.next = "IDLE"
                m.d.comb += self.byte_ready.eq(take_data | (take_end & self.byte_valid & self.byte_end))

        # Queue update: append add_n bits at position fill, then drop the drained bits.
        appended = Signal(QUEUE_BITS + 10)
        m.d.comb += appended.eq(queue | (add_bits << fill))
        with m.If(fsm.ongoing("SYNC") | fsm.ongoing("DATA")):
            sync += [queue.eq((appended >> drain)[:QUEUE_BITS]), fill.eq(fill + add_n - drain)]

        # --- NRZI output of up to four queued bits ------------------------------------------
        lvl = level
        line_bits, oe_bits = [], []
        for i in range(4):
            valid = drain > i
            d = queue[i]
            nxt = Signal(name=f"lvl{i}")
            m.d.comb += nxt.eq(Mux(valid, Mux(d, lvl, ~lvl), lvl))
            line_bits.append(Mux(valid, nxt, 1))
            oe_bits.append(valid)
            lvl = nxt
        with m.If(fsm.ongoing("SYNC") | fsm.ongoing("DATA")):
            sync += [self.line.eq(Cat(*line_bits)), self.oe.eq(Cat(*oe_bits)), level.eq(lvl)]
        with m.Else():
            sync += [self.line.eq(0b1111), self.oe.eq(0)]
        return m
