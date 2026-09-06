"""SoftUTMIPHY: op-modes, chirp drive, line state."""
from amaranth import Module, Elaboratable, ClockDomain
from amaranth.sim import Simulator

from usb2soft.phy.utmi import SoftUTMIPHY, OP_NORMAL, OP_NON_DRIVING, OP_CHIRP
from usb2soft.phy.linestate import LineStateSynthesiser, SE0, J, K
from usb2soft.sim import usbhs
from tests.test_tx_path import utmi_source, segments


class _Harness(Elaboratable):
    def __init__(self, synth=False):
        s = LineStateSynthesiser(se0_cycles=20, gap_cycles=4, hold_cycles=6, min_chirp_cycles=5) if synth else None
        self.phy = SoftUTMIPHY(usb_domain="usb", cdr_domain="rx_cdr", tx_domain="tx_cdr", synthesiser=s)

    def elaborate(self, platform):
        m = Module()
        for d in ("usb", "rx_cdr", "tx_cdr"):
            m.domains += ClockDomain(d)
        m.submodules.phy = self.phy
        return m


def _sim(dut):
    sim = Simulator(dut)
    sim.add_clock(1 / 60e6, domain="usb")
    sim.add_clock(1 / 120e6, domain="rx_cdr")
    sim.add_clock(1 / 120e6, domain="tx_cdr")
    return sim


def _collect_line(dut, line, oe):
    async def sampler(ctx):
        while True:
            l, o = ctx.get(dut.phy.line), ctx.get(dut.phy.oe)
            for i in range(4):
                line.append((l >> i) & 1)
                oe.append((o >> i) & 1)
            await ctx.tick("tx_cdr")
    return sampler


def test_normal_mode_packets_are_encoded():
    dut = _Harness()
    line, oe = [], []
    payloads = [bytes([0xD2]), bytes(range(1, 12))]

    async def src(ctx):
        ctx.set(dut.phy.op_mode, OP_NORMAL)
        await utmi_source(ctx, dut.phy, payloads, 20, {"cycles": 0, "ready": 0})

    sim = _sim(dut)
    sim.add_testbench(src)
    sim.add_testbench(_collect_line(dut, line, oe), background=True)
    sim.run()
    assert segments(line, oe) == [usbhs.packet_line_bits(p) for p in payloads]


def test_chirp_mode_drives_constant_k():
    dut = _Harness()
    line, oe = [], []
    result = {}

    async def src(ctx):
        ctx.set(dut.phy.op_mode, OP_CHIRP)
        ctx.set(dut.phy.tx_valid, 1)
        ctx.set(dut.phy.tx_data, 0)
        for _ in range(40):
            await ctx.tick("usb")
        result["ready"] = ctx.get(dut.phy.tx_ready)
        ctx.set(dut.phy.tx_valid, 0)
        for _ in range(10):
            await ctx.tick("usb")

    sim = _sim(dut)
    sim.add_testbench(src)
    sim.add_testbench(_collect_line(dut, line, oe), background=True)
    sim.run()
    segs = segments(line, oe)
    assert len(segs) == 1 and set(segs[0]) == {0}, segs           # one continuous K burst
    assert 4 * 76 <= len(segs[0]) <= 4 * 84                       # ~40 usb cycles = 80 tx cycles (+ sync latency)
    assert result["ready"] == 1
    # driver goes off within 3 tx cycles of tx_valid falling: burst ends before the trailing idle
    assert oe[-4 * 8:] == [0] * 32


def test_non_driving_keeps_output_off():
    dut = _Harness()
    line, oe = [], []

    async def src(ctx):
        ctx.set(dut.phy.op_mode, OP_NON_DRIVING)
        ctx.set(dut.phy.tx_valid, 1)
        ctx.set(dut.phy.tx_data, 0x5A)
        for _ in range(60):
            assert ctx.get(dut.phy.tx_ready) == 0
            await ctx.tick("usb")

    sim = _sim(dut)
    sim.add_testbench(src)
    sim.add_testbench(_collect_line(dut, line, oe), background=True)
    sim.run()
    assert not any(oe)


def _drive_samples(dut, words):
    async def drv(ctx):
        for w in words:
            ctx.set(dut.phy.samples, w)
            await ctx.tick("rx_cdr")
        ctx.set(dut.phy.samples, 0xFFFF)
    return drv


def test_line_state_is_squelch_derived():
    dut = _Harness()
    pkt = usbhs.packet_line_bits(bytes([0xA5, 0x3C, 0x00, 0x01]))
    samples = usbhs.LineSampler(samples_per_ui=4).sample(pkt)
    words = [0xFFFF] * 40 + list(usbhs.words(samples, 16)) + [0xFFFF] * 60
    trace = []

    async def watch(ctx):
        for _ in range(len(words) // 2 + 10):
            trace.append((ctx.get(dut.phy.line_state), ctx.get(dut.phy.rx_active)))
            await ctx.tick("usb")

    sim = _sim(dut)
    sim.add_testbench(_drive_samples(dut, words))
    sim.add_testbench(watch, background=True)
    sim.run()
    states = [s for s, _ in trace]
    assert states[4:15] == [SE0] * 11                             # idle: squelch (after the reset blip)
    assert any(s in (J, K) for s in states)                       # non-SE0 while the packet is present
    assert states[-10:] == [SE0] * 10                             # squelch again after the packet
    # line state is non-SE0 while rx_active is high (LUNA's idle timers must not run mid-packet);
    # squelch closes 8 UI after the last edge, a few usb cycles before the END event drains through
    # the elastic FIFO and drops rx_active, so allow that tail.
    active = [s for s, a in trace if a]
    assert active and all(s != SE0 for s in active[:-6]), trace


def test_synthesiser_takes_precedence_then_hands_over():
    dut = _Harness(synth=True)
    trace = []

    async def tb(ctx):
        for _ in range(30):
            trace.append(ctx.get(dut.phy.line_state))
            await ctx.tick("usb")
        ctx.set(dut.phy.op_mode, OP_CHIRP)
        ctx.set(dut.phy.tx_valid, 1)
        for _ in range(8):
            await ctx.tick("usb")
        ctx.set(dut.phy.tx_valid, 0)
        ctx.set(dut.phy.op_mode, OP_NORMAL)
        for _ in range(60):
            trace.append(ctx.get(dut.phy.line_state))
            await ctx.tick("usb")

    sim = _sim(dut)
    sim.add_testbench(tb)
    sim.run()
    assert trace[:20] == [SE0] * 20                               # synthesised SE0 window
    assert K in trace[30:] and J in trace[30:]                    # host K/J replay after the chirp
    assert trace[-5:] == [SE0] * 5                                # squelch pass-through afterwards
