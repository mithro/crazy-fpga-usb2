import random
import pytest
from amaranth.sim import Simulator

from usb2soft.rx.cdr import OversamplingCDR
from usb2soft.sim import usbhs


def run_cdr(samples, *, S, W, in_packet_after=None, track_threshold=3):
    """Feed sample words, return the recovered line bits and the slip counts."""
    dut = OversamplingCDR(samples_per_ui=S, samples_per_word=W, track_threshold=track_threshold)
    bits, slips = [], {"up": 0, "down": 0}
    wds = usbhs.words(samples, W)

    async def tb(ctx):
        for n, w in enumerate(wds):
            ctx.set(dut.samples, w)
            if in_packet_after is not None:
                ctx.set(dut.in_packet, n >= in_packet_after)
            await ctx.tick()
            cnt = ctx.get(dut.count)
            val = ctx.get(dut.bits)
            bits.extend((val >> i) & 1 for i in range(cnt))
            slips["up"] += ctx.get(dut.slip_up)
            slips["down"] += ctx.get(dut.slip_down)
        for _ in range(3):
            await ctx.tick()
            cnt = ctx.get(dut.count)
            val = ctx.get(dut.bits)
            bits.extend((val >> i) & 1 for i in range(cnt))

    sim = Simulator(dut)
    sim.add_clock(1 / 120e6)
    sim.add_testbench(tb)
    sim.run()
    return bits, slips


def contains(recovered, sent, *, max_skip=4):
    """True if ``sent`` appears contiguously in ``recovered`` (acquisition may eat a few bits)."""
    s = "".join(map(str, sent[max_skip:]))
    r = "".join(map(str, recovered))
    return s in r


def line_for(payload, sync=32):
    return [1] * 8 + usbhs.packet_line_bits(payload, sync_bits=sync) + [1] * 8


@pytest.mark.parametrize("S,W", [(4, 16), (3, 12)])
@pytest.mark.parametrize("phase", [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5])
def test_static_phase_recovers_exact_bits(S, W, phase):
    if phase >= S:
        pytest.skip("phase outside one UI")
    payload = bytes(random.Random(int(phase * 10)).randrange(256) for _ in range(32))
    line = line_for(payload)
    samples = usbhs.LineSampler(samples_per_ui=S, phase=phase).sample(line)
    bits, _ = run_cdr(samples, S=S, W=W)
    assert contains(bits, line)


@pytest.mark.parametrize("S,W", [(4, 16), (3, 12)])
@pytest.mark.parametrize("ppm", [-2000, -1000, -500, -100, 100, 500, 1000, 2000])
def test_frequency_offset_tracked_over_long_packet(S, W, ppm):
    payload = bytes(random.Random(ppm).randrange(256) for _ in range(1024))   # ~8200 line bits
    line = line_for(payload)
    samples = usbhs.LineSampler(samples_per_ui=S, ppm=ppm, phase=0.3).sample(line)
    bits, slips = run_cdr(samples, S=S, W=W, in_packet_after=3)
    assert contains(bits, line)
    # a slip is a whole-UI wrap of the pick phase, so the count is the drift in UI
    expected_slips = len(line) * abs(ppm) * 1e-6
    total = slips["up"] + slips["down"]
    assert abs(total - expected_slips) <= 2
    # a faster transmitter (+ppm) means fewer samples per bit: the pick phase must move earlier
    if expected_slips >= 2:
        assert (slips["down"] > slips["up"]) == (ppm > 0)


# Gaussian jitter on every edge. With the S=4 dead-zone vote rule the pick self-centres 0.5 UI
# after the mean edge, so ~0.08 UI rms (3 sigma = 0.24 UI, plus 0.125 UI sampling quantisation)
# is the knee: measured 6/6 packets intact at 0.08 and 5/6 at 0.10 (tests/test_rx_margins.py
# records the full curve). The USB HS receiver eye assumes far less jitter than that.
@pytest.mark.parametrize("rj_ui", [0.0, 0.03, 0.06, 0.08])
def test_random_jitter_tolerance(rj_ui):
    payload = bytes(random.Random(5).randrange(256) for _ in range(256))
    line = line_for(payload)
    samples = usbhs.LineSampler(samples_per_ui=4, ppm=300, rj_ui=rj_ui, seed=11).sample(line)
    bits, _ = run_cdr(samples, S=4, W=16, in_packet_after=3)
    assert contains(bits, line)


def test_deterministic_jitter_plus_offset():
    payload = bytes(random.Random(9).randrange(256) for _ in range(512))
    line = line_for(payload)
    samples = usbhs.LineSampler(samples_per_ui=4, ppm=-500, dj_ui=0.15, dj_period_ui=37).sample(line)
    bits, _ = run_cdr(samples, S=4, W=16, in_packet_after=3)
    assert contains(bits, line)


def test_phase_step_between_packets_reacquires():
    p1 = bytes(range(16))
    p2 = bytes(range(16, 48))
    a = usbhs.LineSampler(samples_per_ui=4, phase=0.0).sample(line_for(p1))
    b = usbhs.LineSampler(samples_per_ui=4, phase=2.0).sample(line_for(p2))
    samples = a + [1] * 40 + b
    bits, _ = run_cdr(samples, S=4, W=16)
    assert contains(bits, usbhs.packet_line_bits(p1)) and contains(bits, usbhs.packet_line_bits(p2))


def test_activity_flag_follows_edges():
    dut = OversamplingCDR(samples_per_ui=4, samples_per_word=16)
    seen = []

    async def tb(ctx):
        ctx.set(dut.samples, 0xFFFF)
        for _ in range(12):
            await ctx.tick()
        seen.append(ctx.get(dut.activity))          # flat for 48 UI -> no activity
        ctx.set(dut.samples, 0x0F0F)                 # edges every 4 samples
        await ctx.tick()
        await ctx.tick()
        seen.append(ctx.get(dut.activity))
        ctx.set(dut.samples, 0x0000)
        for _ in range(3):                           # 12 UI without an edge > idle_ui=8
            await ctx.tick()
        seen.append(ctx.get(dut.activity))

    sim = Simulator(dut)
    sim.add_clock(1 / 120e6)
    sim.add_testbench(tb)
    sim.run()
    assert seen == [0, 1, 0]


@pytest.mark.parametrize("S,W", [(4, 16), (3, 12)])
def test_all_edges_word_does_not_slip(S, W):
    """A word with an edge at every sample (noise) gives equal up/down votes: no phase steps.
    Regression: a signed reinterpretation of the popcount (16 read as -16) stepped every cycle."""
    dut = OversamplingCDR(samples_per_ui=S, samples_per_word=W)
    slips = 0
    phases = set()

    async def tb(ctx):
        nonlocal slips
        alt = sum(1 << i for i in range(0, W, 2))
        for _ in range(40):
            ctx.set(dut.samples, alt)
            await ctx.tick()
            slips += ctx.get(dut.slip_up) + ctx.get(dut.slip_down)
            phases.add(ctx.get(dut.phase))

    sim = Simulator(dut)
    sim.add_clock(1 / 120e6)
    sim.add_testbench(tb)
    sim.run()
    assert slips == 0 and len(phases) <= 2


def test_noisy_idle_then_packet_locks():
    payload = bytes(random.Random(3).randrange(256) for _ in range(64))
    noise = usbhs.idle_noise(n_ui=200, samples_per_ui=4, toggle_prob=0.25, seed=5)
    samples = noise + usbhs.LineSampler(samples_per_ui=4, ppm=200, phase=1.2).sample(line_for(payload))
    bits, _ = run_cdr(samples, S=4, W=16)
    assert contains(bits, usbhs.packet_line_bits(payload))
