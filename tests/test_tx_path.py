import random
import pytest
from amaranth import Module, Elaboratable
from amaranth.sim import Simulator

from usb2soft.tx import TxPath
from usb2soft.rx import RxPath
from usb2soft.sim import usbhs


class _TxHarness(Elaboratable):
    def __init__(self):
        self.tx = TxPath(tx_domain="cdr", usb_domain="usb")

    def elaborate(self, platform):
        m = Module()
        m.submodules.tx = self.tx
        return m


async def utmi_source(ctx, tx, payloads, gap_cycles, stats):
    """LUNA-style UTMI transmitter: hold tx_valid with a byte, advance on tx_ready."""
    for p in payloads:
        started = None
        for byte in p:
            ctx.set(tx.tx_valid, 1)
            ctx.set(tx.tx_data, byte)
            while True:
                ready = ctx.get(tx.tx_ready)
                stats["cycles"] += 1
                stats["ready"] += ready
                await ctx.tick("usb")
                if ready:
                    break
        ctx.set(tx.tx_valid, 0)
        for _ in range(gap_cycles):
            await ctx.tick("usb")
    for _ in range(40):
        await ctx.tick("usb")


def run_tx(payloads, *, gap_cycles=20):
    dut = _TxHarness()
    line, oe = [], []
    stats = {"cycles": 0, "ready": 0}

    async def sampler(ctx):
        while True:
            l, o = ctx.get(dut.tx.line), ctx.get(dut.tx.oe)
            for i in range(4):
                line.append((l >> i) & 1)
                oe.append((o >> i) & 1)
            await ctx.tick("cdr")

    async def src(ctx):
        await utmi_source(ctx, dut.tx, payloads, gap_cycles, stats)

    sim = Simulator(dut)
    sim.add_clock(1 / 120e6, domain="cdr")
    sim.add_clock(1 / 60e6, domain="usb")
    sim.add_testbench(src)
    sim.add_testbench(sampler, background=True)
    sim.run()
    return line, oe, stats


def segments(line, oe):
    segs, cur = [], []
    for l, o in zip(line, oe):
        if o:
            cur.append(l)
        elif cur:
            segs.append(cur)
            cur = []
    if cur:
        segs.append(cur)
    return segs


def test_utmi_packets_are_bit_exact():
    rng = random.Random(7)
    payloads = [bytes(rng.randrange(256) for _ in range(n)) for n in (1, 3, 8, 64, 200)]
    line, oe, _ = run_tx(payloads)
    assert segments(line, oe) == [usbhs.packet_line_bits(p) for p in payloads]


def test_tx_ready_cadence():
    # all-zero payload: no stuffing, one byte per 60 MHz cycle is exactly the line rate
    _, _, zeros = run_tx([bytes(512)])
    # all-ones payload: every 6 data bits carry a stuffed zero, so the byte rate drops to 6/7
    _, _, ones = run_tx([b"\xff" * 512])
    duty0 = zeros["ready"] / zeros["cycles"]
    duty1 = ones["ready"] / ones["cycles"]
    assert duty0 > 0.95, duty0
    assert 0.80 < duty1 < 0.90, duty1


class _LoopHarness(Elaboratable):
    """TX in one domain pair, RX in another; the wire is modelled in the testbench."""
    def __init__(self, S=4, W=16):
        self.tx = TxPath(tx_domain="cdr", usb_domain="usb")
        self.rx = RxPath(samples_per_ui=S, samples_per_word=W, cdr_domain="rxcdr", usb_domain="rxusb")

    def elaborate(self, platform):
        m = Module()
        m.submodules.tx = self.tx
        m.submodules.rx = self.rx
        return m


def run_loop(payloads, *, ppm, S=4, W=16, gap_cycles=24):
    """Transmit, capture the driven line bits, resample them at ±ppm, and receive."""
    line, oe, _ = run_tx(payloads, gap_cycles=gap_cycles)
    wire = [l if o else 1 for l, o in zip(line, oe)]           # idle: no transitions
    samples = usbhs.LineSampler(samples_per_ui=S, ppm=ppm, phase=0.4).sample(wire)
    # Receive with the RX harness from test_rx_path (same protocol checks).
    from tests.test_rx_path import run_path
    got, errors = run_path(samples, S=S, W=W)
    return got, errors


@pytest.mark.parametrize("ppm", [-500, 0, 500])
def test_round_trip_tx_to_rx(ppm):
    rng = random.Random(ppm + 3)
    payloads = [bytes(rng.randrange(256) for _ in range(n)) for n in (1, 8, 64, 512)]
    got, errors = run_loop(payloads, ppm=ppm)
    assert got == payloads and errors == 0


def test_round_trip_three_x():
    payloads = [bytes(range(32)), b"\xff" * 20, bytes(range(200, 256))]
    got, errors = run_loop(payloads, ppm=300, S=3, W=12)
    assert got == payloads and errors == 0
