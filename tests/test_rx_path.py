import random
import pytest
from amaranth import Module, Elaboratable
from amaranth.sim import Simulator

from usb2soft.rx import RxPath
from usb2soft.sim import usbhs


class _Harness(Elaboratable):
    def __init__(self, S=4, W=16):
        self.rx = RxPath(samples_per_ui=S, samples_per_word=W, cdr_domain="cdr", usb_domain="usb")

    def elaborate(self, platform):
        m = Module()
        m.submodules.rx = self.rx
        return m


def run_path(samples, *, S=4, W=16, extra_cycles=200):
    dut = _Harness(S, W)
    wds = usbhs.words(samples, W)
    got, errors = [], 0
    cur = None

    async def feeder(ctx):
        for w in wds:
            ctx.set(dut.rx.samples, w)
            await ctx.tick("cdr")
        ctx.set(dut.rx.samples, (1 << W) - 1)
        for _ in range(extra_cycles):
            await ctx.tick("cdr")
        assert ctx.get(dut.rx.bridge.overflow) == 0, "event FIFO overflowed"

    async def utmi(ctx):
        nonlocal cur, errors
        prev_active = 0
        while True:
            active = ctx.get(dut.rx.rx_active)
            valid = ctx.get(dut.rx.rx_valid)
            if valid:
                assert active, "rx_valid without rx_active"
                assert prev_active, "rx_valid in the same cycle rx_active rose"
                cur.append(ctx.get(dut.rx.rx_data))
            if active and not prev_active:
                cur = bytearray()
            if prev_active and not active and cur is not None:
                got.append(bytes(cur))
                cur = None
            errors += ctx.get(dut.rx.rx_error)
            prev_active = active
            await ctx.tick("usb")

    sim = Simulator(dut)
    sim.add_clock(1 / 120e6, domain="cdr")
    sim.add_clock(1 / 60e6, domain="usb")
    sim.add_testbench(feeder)
    sim.add_testbench(utmi, background=True)
    sim.run()
    return got, errors


def stream(payloads, *, ppm, S=4, gap_ui=16, phase=0.7, seed=0, rj_ui=0.0):
    line = [1] * 40
    for p in payloads:
        line += usbhs.packet_line_bits(p) + [1] * gap_ui
    return usbhs.LineSampler(samples_per_ui=S, ppm=ppm, phase=phase, rj_ui=rj_ui, seed=seed).sample(line)


@pytest.mark.parametrize("ppm", [-1000, -500, 0, 500, 1000])
def test_random_packets_at_offset(ppm):
    rng = random.Random(ppm)
    payloads = [bytes(rng.randrange(256) for _ in range(rng.choice([3, 8, 64, 512]))) for _ in range(4)]
    got, errors = run_path(stream(payloads, ppm=ppm))
    assert got == payloads and errors == 0


def test_elastic_buffer_all_zero_payload_fast_host():
    # all-zero data has no stuffing, so bytes arrive at the maximum rate; +500 ppm on top
    payloads = [bytes(1024)]
    got, errors = run_path(stream(payloads, ppm=500), extra_cycles=600)
    assert got == payloads and errors == 0


def test_three_x_variant():
    payloads = [bytes(range(32)), bytes(range(100, 130))]
    got, errors = run_path(stream(payloads, ppm=-300, S=3), S=3, W=12)
    assert got == payloads and errors == 0


@pytest.mark.parametrize("shift", range(5))
def test_bad_packet_keeps_utmi_ordering(shift):
    """A byte ending in six ones followed by a flat line (missing stuff zero) completes a byte
    and raises ERROR in the same decoder word; rx_valid must never appear with rx_active low."""
    payload = bytes([0x11, 0xFC])
    line = [1] * (40 + shift) + usbhs.packet_line_bits(payload)[:-8]        # drop the EOP
    line += [line[-1]] * 40                                                  # flat: violation without the EOP zero
    samples = usbhs.LineSampler(samples_per_ui=4).sample(line)
    got, errors = run_path(samples)                                          # run_path asserts the ordering
    assert errors == 1


def test_jitter_and_noise_between_packets():
    rng = random.Random(4)
    payloads = [bytes(rng.randrange(256) for _ in range(64)) for _ in range(3)]
    samples = usbhs.idle_noise(n_ui=80, samples_per_ui=4, toggle_prob=0.3, seed=1)
    for i, p in enumerate(payloads):
        samples += usbhs.LineSampler(samples_per_ui=4, ppm=250, phase=i * 1.3, rj_ui=0.06, seed=i).sample(
            [1] * 8 + usbhs.packet_line_bits(p) + [1] * 8)
        samples += usbhs.idle_noise(n_ui=50, samples_per_ui=4, toggle_prob=0.3, seed=10 + i)
    got, errors = run_path(samples)
    # noise may or may not fabricate junk packets; every real packet must be present and intact
    for p in payloads:
        assert p in got
