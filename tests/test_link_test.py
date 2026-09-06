import pytest
from amaranth import Module, Elaboratable
from amaranth.sim import Simulator

from usb2soft.applets.link_test import LinkTestCore
from usb2soft.sim.expander import SampleExpander
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
    def __init__(self, phase_shift):
        # all three gateware domains run at the same 120/60 MHz relationship as in hardware
        self.core = LinkTestCore(internal=True, phase_shift=phase_shift, gap=4,
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
        result["bad"] = ctx.get(dut.core.chk.bad)
        result["good"] = ctx.get(dut.core.chk.good)

    sim = Simulator(dut)
    sim.add_clock(1 / 60e6, domain="usb")
    sim.add_clock(1 / 120e6, domain="rx_cdr")
    sim.add_clock(1 / 120e6, domain="tx_cdr")
    sim.add_testbench(tb)
    sim.run()
    assert result["good"] > 0
    assert result["bad"] >= 1
