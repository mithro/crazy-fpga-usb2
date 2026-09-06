"""Blind-oversampling clock/data recovery (spec §4.2).

Each cycle takes W samples (sample 0 earliest), S per unit interval nominally, and emits the
recovered line bits: B = W/S normally, B-1 when the pick phase wraps upward, B+1 when it wraps
downward (the extra bit being the previous word's last sample). Edges vote on where the pick
should be; a saturating accumulator turns votes into at most one +-1 phase step per cycle.
Out of a packet the threshold is 1 (fast acquisition on SYNC); inside it is ``track_threshold``.
"""
from amaranth import Elaboratable, Module, Signal, Cat, Const, Array, Mux, signed

__all__ = ["OversamplingCDR"]


class OversamplingCDR(Elaboratable):
    def __init__(self, *, samples_per_ui, samples_per_word, track_threshold=3, idle_ui=8,
                 domain="sync"):
        S, W = samples_per_ui, samples_per_word
        if S not in (3, 4):
            raise ValueError("samples_per_ui must be 3 or 4")
        if W % S:
            raise ValueError("samples_per_word must be a multiple of samples_per_ui")
        self.S, self.W, self.B = S, W, W // S
        self.track_threshold = track_threshold
        self.idle_ui = idle_ui
        self.domain = domain

        self.samples = Signal(W)
        self.in_packet = Signal()

        self.bits = Signal(self.B + 1)
        self.count = Signal(range(self.B + 2))
        self.activity = Signal()
        self.slip_up = Signal()
        self.slip_down = Signal()
        self.phase = Signal(range(S))

    def elaborate(self, platform):
        m = Module()
        S, W, B = self.S, self.W, self.B
        sync = m.d[self.domain]

        prev_last = Signal()
        cur = self.samples
        # ext[0] = previous word's last sample, ext[1 + i] = cur[i], zero padded so that every
        # Array index used below is in range.
        ext = Signal(W + S + 2)
        m.d.comb += ext.eq(Cat(prev_last, cur))
        edges = Signal(W)
        m.d.comb += edges.eq(cur ^ Cat(prev_last, cur[:-1]))

        # --- votes -------------------------------------------------------------------------
        # An edge between samples i-1 and i means the bit starting at i is best sampled at
        # i + S//2. Compare (mod S) with the current phase. For S=3 (pick at edge+1, already
        # centred): +1 -> pick too early, 2 -> too late. For S=4 the pick sits at edge+2 and the
        # errors 0 and 1 form a dead zone: with jitter the loop then settles where edges fall
        # equally often one slot early and two slots late, i.e. with the mean edge *on* a sample
        # instant, which centres the pick 0.5 UI after it (symmetric 0.5 UI margins). Voting on
        # error 1 as well would centre the mean edge mid-slot and leave only 0.375 UI to the next
        # edge (measured: bit errors from 0.06 UI rms jitter instead of ~0.1).
        up_set = {2} if S == 4 else {1}
        up_terms, down_terms = [], []
        for i in range(W):
            ideal = (i + S // 2) % S
            up_lut = Array([Const(1 if ((ideal - phi) % S) in up_set else 0, 1) for phi in range(S)])
            down_lut = Array([Const(1 if ((ideal - phi) % S) == S - 1 else 0, 1) for phi in range(S)])
            up_terms.append(edges[i] & up_lut[self.phase])
            down_terms.append(edges[i] & down_lut[self.phase])
        up = Signal(range(W + 1))
        down = Signal(range(W + 1))
        m.d.comb += [up.eq(sum(up_terms)), down.eq(sum(down_terms))]

        # --- loop filter -------------------------------------------------------------------
        T = self.track_threshold
        acc = Signal(signed(8))
        thr = Signal(signed(8))
        m.d.comb += thr.eq(Mux(self.in_packet, T, 1))
        acc_next = Signal(signed(8))
        m.d.comb += acc_next.eq(acc + up.as_signed() - down.as_signed())
        step_up = Signal()
        step_down = Signal()
        m.d.comb += [
            step_up.eq(acc_next >= thr),
            step_down.eq(acc_next <= -thr),
        ]
        with m.If(step_up | step_down):
            sync += acc.eq(0)
        with m.Else():
            # saturate so a burst of noise cannot bank votes
            sync += acc.eq(Mux(acc_next > T, T, Mux(acc_next < -T, -T, acc_next)))

        # --- picks -------------------------------------------------------------------------
        # sel = phase + step + 1 in [0, S+1]: 0 means "phase went to -1", S+1 means "went to S".
        sel = Signal(range(S + 2))
        m.d.comb += sel.eq(self.phase + 1 + step_up - step_down)
        pick = [Signal(name=f"pick{j}") for j in range(B + 1)]
        for j in range(B + 1):
            m.d.comb += pick[j].eq(Array([ext[s + j * S] for s in range(S + 2)])[sel])
        count = Signal(range(B + 2))
        with m.If(sel == 0):
            m.d.comb += count.eq(B + 1)
        with m.Elif(sel == S + 1):
            m.d.comb += count.eq(B - 1)
        with m.Else():
            m.d.comb += count.eq(B)
        # sel == S+1: picks are cur[S + j*S], of which B-1 lie in this word.
        # sel == 0:   picks are prev_last, cur[S-1], cur[2S-1], ...: B+1 valid.
        sync += [
            self.bits.eq(Cat(*pick)),
            self.count.eq(count),
            self.slip_up.eq(sel == S + 1),
            self.slip_down.eq(sel == 0),
            prev_last.eq(cur[W - 1]),
        ]
        with m.If(sel == 0):
            sync += self.phase.eq(S - 1)
        with m.Elif(sel == S + 1):
            sync += self.phase.eq(0)
        with m.Else():
            sync += self.phase.eq(sel - 1)

        # --- activity (digital squelch) ----------------------------------------------------
        limit = self.idle_ui * S
        idle = Signal(range(limit + W + 1))
        with m.If(edges.any()):
            sync += idle.eq(0)
        with m.Elif(idle < limit):
            sync += idle.eq(idle + W)
        m.d.comb += self.activity.eq(idle < limit)
        return m
