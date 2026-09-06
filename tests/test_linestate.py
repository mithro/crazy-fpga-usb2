from amaranth import Module, Elaboratable, ClockDomain
from amaranth.sim import Simulator

from usb2soft.phy.linestate import HSLineState, LineStateSynthesiser, SE0, J, K

CHIRP = 2


class _LS(Elaboratable):
    def __init__(self):
        self.ls = HSLineState(cdr_domain="rx_cdr", usb_domain="usb")

    def elaborate(self, platform):
        m = Module()
        m.domains.rx_cdr = ClockDomain("rx_cdr")
        m.domains.usb = ClockDomain("usb")
        m.submodules.ls = self.ls
        return m


def test_hs_line_state_follows_activity_and_level():
    dut = _LS()
    trace = []

    async def tb(ctx):
        for _ in range(6):
            await ctx.tick("usb")
        assert ctx.get(dut.ls.line_state) == SE0
        ctx.set(dut.ls.activity, 1)
        ctx.set(dut.ls.level, 1)
        for _ in range(6):
            await ctx.tick("usb")
        assert ctx.get(dut.ls.line_state) == J
        ctx.set(dut.ls.level, 0)
        for _ in range(6):
            await ctx.tick("usb")
        assert ctx.get(dut.ls.line_state) == K
        ctx.set(dut.ls.activity, 0)
        for i in range(5):
            await ctx.tick("usb")
            trace.append(ctx.get(dut.ls.line_state))
        assert trace[-1] == SE0 and SE0 in trace[:4], trace     # back to squelch within 4 usb cycles

    sim = Simulator(dut)
    sim.add_clock(1 / 120e6, domain="rx_cdr")
    sim.add_clock(1 / 60e6, domain="usb")
    sim.add_testbench(tb)
    sim.run()


class _Synth(Elaboratable):
    def __init__(self, **kw):
        self.s = LineStateSynthesiser(**kw)

    def elaborate(self, platform):
        m = Module()
        m.domains.usb = ClockDomain("usb")
        m.submodules.s = self.s
        return m


def _runs(seq):
    out = []
    for v in seq:
        if out and out[-1][0] == v:
            out[-1][1] += 1
        else:
            out.append([v, 1])
    return [tuple(r) for r in out]


def test_synthesiser_sequence_and_rearm():
    dut = _Synth(se0_cycles=20, gap_cycles=4, hold_cycles=6, pairs=3)
    trace = []

    async def chirp(ctx, cycles):
        ctx.set(dut.s.op_mode, CHIRP)
        ctx.set(dut.s.tx_valid, 1)
        for _ in range(cycles):
            await ctx.tick("usb")
        ctx.set(dut.s.tx_valid, 0)
        await ctx.tick("usb")
        ctx.set(dut.s.op_mode, 0)

    async def tb(ctx):
        ctx.set(dut.s.squelch_line_state, J)        # "packet on the wire" while not synthesising
        for _ in range(30):                          # SE0 window (20) then pass-through J
            trace.append(ctx.get(dut.s.line_state))
            await ctx.tick("usb")
        await chirp(ctx, 10)
        for _ in range(4 + 6 * 6 + 5):
            trace.append(ctx.get(dut.s.line_state))
            await ctx.tick("usb")
        # second chirp: sequence replays (re-arm)
        await chirp(ctx, 10)
        for _ in range(4 + 6 * 6 + 5):
            trace.append(ctx.get(dut.s.line_state))
            await ctx.tick("usb")
        assert ctx.get(dut.s.chirps) == 2

    sim = Simulator(dut)
    sim.add_clock(1 / 60e6, domain="usb")
    sim.add_testbench(tb)
    sim.run()
    runs = _runs(trace)
    # power-up SE0, pass-through J, then (after each chirp) gap of pass-through, K/J x3, pass-through
    assert runs[0] == (SE0, 20), runs[:3]
    assert runs[1][0] == J
    kj = [(K, 6), (J, 6), (K, 6), (J, 6), (K, 6)]
    # first replay: the final J hold merges with the pass-through J that follows it
    i = 2
    assert runs[i:i + 5] == kj, runs[i:i + 8]
    assert runs[i + 5][0] == J and runs[i + 5][1] >= 6
    rest = runs[i + 6:]
    j = next(n for n, r in enumerate(rest) if r[0] == K)
    assert rest[j:j + 5] == kj, rest[j:j + 8]
    assert rest[j + 5][0] == J and rest[j + 5][1] >= 6


def test_synthesiser_chirp_during_se0_still_replays():
    dut = _Synth(se0_cycles=40, gap_cycles=2, hold_cycles=3, pairs=3)
    trace = []

    async def tb(ctx):
        ctx.set(dut.s.squelch_line_state, SE0)
        for _ in range(5):
            await ctx.tick("usb")
        ctx.set(dut.s.op_mode, CHIRP)
        ctx.set(dut.s.tx_valid, 1)
        for _ in range(8):
            await ctx.tick("usb")
        ctx.set(dut.s.tx_valid, 0)
        for _ in range(60):
            trace.append(ctx.get(dut.s.line_state))
            await ctx.tick("usb")

    sim = Simulator(dut)
    sim.add_clock(1 / 60e6, domain="usb")
    sim.add_testbench(tb)
    sim.run()
    runs = _runs(trace)
    assert any(r == (K, 3) for r in runs) and sum(1 for r in runs if r == (K, 3)) == 3, runs
    assert sum(1 for r in runs if r == (J, 3)) == 3, runs
    assert runs[-1][0] == SE0                        # pass-through squelch afterwards
