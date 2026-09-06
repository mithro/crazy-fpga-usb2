import pytest
from amaranth import Module, Elaboratable
from amaranth.sim import Simulator

from usb2soft.applets.link_test import LinkTestCore
from usb2soft.sim.expander import SampleExpander, AsyncResampler
from usb2soft.sim import usbhs


def test_expander_matches_model_sampling():
    dut = SampleExpander(phase_shift=1)
    bits = [1, 0, 1, 1, 0, 0, 1, 0, 1, 1, 1, 1, 0, 1, 0, 0]
    out = []

    async def tb(ctx):
        for i in range(0, len(bits), 4):
            ctx.set(dut.line, sum(b << k for k, b in enumerate(bits[i:i + 4])))
            ctx.set(dut.oe, 0xF)
            w = ctx.get(dut.samples)          # what the CDR registers at the coming edge
            out.extend((w >> k) & 1 for k in range(16))
            await ctx.tick()

    sim = Simulator(dut)
    sim.add_clock(1 / 120e6)
    sim.add_testbench(tb)
    sim.run()
    expected = [b for b in bits for _ in range(4)]
    # rotated by one sample: the first output sample is the carry (initially 0)
    assert out[1:] == expected[:-1]


class _Harness(Elaboratable):
    def __init__(self, phase_shift, async_tx=False):
        # all three gateware domains run at the same 120/60 MHz relationship as in hardware
        self.core = LinkTestCore(internal=True, phase_shift=phase_shift, gap=4, async_tx=async_tx,
                                 report_period=4000, console_divisor=4)

    def elaborate(self, platform):
        m = Module()
        m.submodules.core = self.core
        return m


@pytest.mark.parametrize("phase_shift", [0, 1, 2, 3])
def test_internal_loopback_packets_flow(phase_shift):
    dut = _Harness(phase_shift)
    result = {}

    async def tb(ctx):
        # run long enough for the 8/64/512-byte cycle to complete a few times
        for _ in range(9000):
            await ctx.tick("usb")
        result["tx"] = ctx.get(dut.core.gen.packets)
        result["good"] = ctx.get(dut.core.chk.good)
        result["bad"] = ctx.get(dut.core.chk.bad)
        result["errors"] = ctx.get(dut.core.chk.errors)
        result["gaps"] = ctx.get(dut.core.chk.gaps)

    sim = Simulator(dut)
    sim.add_clock(1 / 60e6, domain="usb")
    sim.add_clock(1 / 120e6, domain="rx_cdr")
    sim.add_clock(1 / 120e6, domain="tx_cdr")
    sim.add_testbench(tb)
    sim.run()
    assert result["tx"] >= 6
    assert result["good"] >= result["tx"] - 1          # the last packet may be in flight
    assert result["bad"] == 0 and result["errors"] == 0 and result["gaps"] == 0


def test_checker_flags_corruption():
    """Corrupt one payload byte on the wire: exactly one bad packet, and the sequence keeps up."""
    dut = _Harness(0)
    result = {}

    async def tb(ctx):
        for _ in range(3000):
            await ctx.tick("usb")
        # flip a sample word mid-run for one cycle (a burst of wrong bits inside some packet)
        for _ in range(4):
            await ctx.tick("rx_cdr")
        ctx.set(dut.core.inject, 0xFFFF)         # invert one whole sample word
        await ctx.tick("rx_cdr")
        ctx.set(dut.core.inject, 0)
        for _ in range(6000):
            await ctx.tick("usb")
        for name in ("good", "bad", "errors", "gaps"):
            result[name] = ctx.get(getattr(dut.core.chk, name))
        result["tx"] = ctx.get(dut.core.gen.packets)

    sim = Simulator(dut)
    sim.add_clock(1 / 60e6, domain="usb")
    sim.add_clock(1 / 120e6, domain="rx_cdr")
    sim.add_clock(1 / 120e6, domain="tx_cdr")
    sim.add_testbench(tb)
    sim.run()
    # Deterministic: the inverted word lands inside one packet's payload, which the decoder
    # flags (stuffing violation -> error event) and the checker counts as one bad packet and one
    # sequence gap; every other packet is good.
    assert result["good"] > 0
    assert result["bad"] == 1 and result["errors"] == 1 and result["gaps"] == 1, result
    assert result["good"] + result["bad"] >= result["tx"] - 1, result


@pytest.mark.parametrize("tx_mhz,ppm", [(120.4545, 3788), (119.4444, -4630)])
def test_async_loopback_tracks_offset(tx_mhz, ppm):
    """TX path clocked at +3788 / -4630 ppm against the CDR: packets survive and the CDR slips in
    the expected direction at the expected rate (one sample per 1/(4*ppm) UI)."""
    dut = _Harness(0, async_tx=True)
    result = {"steps": 0, "steps_in_packet": 0}

    async def tb(ctx):
        for _ in range(12000):
            await ctx.tick("usb")
        result["tx"] = ctx.get(dut.core.gen.packets)
        for name in ("good", "bad", "errors", "gaps"):
            result[name] = ctx.get(getattr(dut.core.chk, name))
        result["up"] = ctx.get(dut.core.slips_up)
        result["down"] = ctx.get(dut.core.slips_down)
        result["overflow"] = ctx.get(dut.core.overflow)

    async def count_steps(ctx):
        # resampler drop/dup strobes (one sample each); the CDR only tracks while a packet is present
        exp = dut.core.expander
        while True:
            step = ctx.get(exp.drops) | ctx.get(exp.dups)
            result["steps"] += step
            result["steps_in_packet"] += step & ctx.get(dut.core.rx.cdr.in_packet)
            await ctx.tick("rx_cdr")

    sim = Simulator(dut)
    sim.add_clock(1 / 60e6, domain="usb")
    sim.add_clock(1 / 120e6, domain="rx_cdr")
    sim.add_clock(1 / (tx_mhz * 1e6), domain="tx_async")
    sim.add_testbench(tb)
    sim.add_testbench(count_steps, background=True)
    sim.run()
    assert result["tx"] >= 8
    assert result["good"] >= result["tx"] - 1, result
    assert result["bad"] == 0 and result["errors"] == 0 and result["gaps"] == 0, result
    assert result["overflow"] == 0
    # A slip is one phase-pointer wrap = one UI = 4 dropped/repeated samples, so the CDR must slip
    # about once per 4 resampler steps that happen while it is tracking (in packet), in the
    # direction of the offset; opposite-direction slips are rare acquisition steps.
    major, minor = (result["up"], result["down"]) if ppm < 0 else (result["down"], result["up"])
    expected = result["steps_in_packet"] / 4
    assert expected > 150, result
    assert abs(major - expected) <= 0.25 * expected + 5, (major, expected, result)
    assert minor <= major // 20 + 2, result


def _runs(bits):
    out = []
    for b in bits:
        if out and out[-1][0] == b:
            out[-1][1] += 1
        else:
            out.append([b, 1])
    return out


@pytest.mark.parametrize("tx_mhz", [120.4545, 119.4444, 120.0])
def test_async_resampler_preserves_bit_stream(tx_mhz):
    """Random line bits at the offset clock come out as the same run sequence with every run
    length changed by at most one sample (single-sample drops/repeats only), and the crossing FIFO
    never overruns."""
    import random
    rng = random.Random(1)
    dut = AsyncResampler(in_domain="tx_async", out_domain="rx_cdr")
    n_in = 3000
    line_words = [rng.getrandbits(4) for _ in range(n_in)]
    got = []
    result = {}

    async def drive(ctx):
        for w in line_words:
            ctx.set(dut.line, w)
            ctx.set(dut.oe, 0xF)
            await ctx.tick("tx_async")
        ctx.set(dut.oe, 0)
        for _ in range(64):
            await ctx.tick("tx_async")
        result["overflow"] = ctx.get(dut.overflow)

    async def collect(ctx):
        for _ in range(n_in + 40):
            v = ctx.get(dut.samples)
            got.extend((v >> i) & 1 for i in range(16))
            await ctx.tick("rx_cdr")

    sim = Simulator(dut)
    sim.add_clock(1 / 120e6, domain="rx_cdr")
    sim.add_clock(1 / (tx_mhz * 1e6), domain="tx_async")
    sim.add_testbench(drive)
    sim.add_testbench(collect, background=True)
    sim.run()
    assert result["overflow"] == 0
    sent = [(w >> i) & 1 for w in line_words for i in range(4) for _ in range(4)]
    # strip the leading idle (J = 1) both sides, then the trailing idle
    r_in, r_out = _runs([1] + sent), _runs(got)
    r_out = r_out[:len(r_in)]
    assert len(r_out) == len(r_in)
    assert all(a[0] == b[0] for a, b in zip(r_in, r_out))
    diffs = [b[1] - a[1] for a, b in zip(r_in[1:-1], r_out[1:-1])]
    assert all(abs(d) <= 1 for d in diffs), [d for d in diffs if abs(d) > 1][:5]
    expected = (120.0 / tx_mhz - 1) * len(sent)      # fast TX -> fewer output samples
    # the backlog may sit anywhere in the +/-(HYST + one 32-sample lump) band at start and end
    assert abs(sum(diffs) - expected) <= 80, (sum(diffs), expected)


def test_build_tag_and_modes():
    import argparse
    from usb2soft.applets.link_test import LinkTest
    p = argparse.ArgumentParser()
    LinkTest.add_arguments(p)
    for argv, tag in (([], "-internal"), (["--mode", "hdmi", "--hdmi-rx", "1"], "-hdmi-rx1"),
                      (["--mode", "async-fast"], "-async-fast"), (["--mode", "async-slow"], "-async-slow")):
        args = p.parse_args(argv)
        assert LinkTest.build_tag(args) == tag
        applet = LinkTest(args)
        applet._MustUse__silence = True      # not elaborated here
        xdc = applet.vivado_constraints()
        assert bool(xdc) == args.mode.startswith("async-")
        if xdc:
            assert "set_clock_groups -asynchronous" in xdc[0] and "raw_tx_async" in xdc[0]
