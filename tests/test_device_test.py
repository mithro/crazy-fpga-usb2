"""DeviceTestCore (the device-test applet's core) in internal and async modes, scaled timings."""
import pytest
from amaranth import Module, Elaboratable, ClockDomain
from amaranth.sim import Simulator

from usb2soft.applets.device_test import DeviceTestCore
from tests.luna_harness import scale_sequencer, SYNTH_SCALED

HOST_SCALED = dict(sof_period=1500, start_delay=1200, restart_delay=100, reply_timeout=256)


class _Top(Elaboratable):
    def __init__(self, async_tx):
        self.core = DeviceTestCore(role="both", async_tx=async_tx, synth=SYNTH_SCALED, host=HOST_SCALED,
                                   report_period=4000, console_divisor=4)

    def elaborate(self, platform):
        m = Module()
        for d in ("usb", "sync", "rx_cdr", "tx_cdr", "tx_async"):
            m.domains += ClockDomain(d)
        m.submodules.core = self.core
        return m


def _stats(ctx, core):
    h = core.host
    return {n: ctx.get(getattr(h, n)) for n in ("loops", "ok", "bad", "timeouts", "naks", "restarts", "sofs")} | {
        "hs": ctx.get(core.hs), "chirps": ctx.get(core.dev_phy.synthesiser.chirps),
        "up": ctx.get(core.slips_up), "dn": ctx.get(core.slips_down)}


@pytest.mark.parametrize("tx_mhz", [None, 120.4545, 119.4444])
def test_device_test_core(monkeypatch, tx_mhz):
    scale_sequencer(monkeypatch)
    dut = _Top(async_tx=tx_mhz is not None)
    result = {}

    async def tb(ctx):
        for _ in range(9000):
            await ctx.tick("usb")
        result.update(_stats(ctx, dut.core))

    sim = Simulator(dut)
    sim.add_clock(1 / 60e6, domain="usb")
    sim.add_clock(1 / 60e6, domain="sync")
    sim.add_clock(1 / 120e6, domain="rx_cdr")
    sim.add_clock(1 / 120e6, domain="tx_cdr")
    sim.add_clock(1 / ((tx_mhz or 120.0) * 1e6), domain="tx_async")
    sim.add_testbench(tb)
    sim.run()
    print("\ndevice-test", tx_mhz, result)
    assert result["hs"] == 1 and result["chirps"] == 1, result
    assert result["loops"] >= 3 and result["bad"] == 0 and result["timeouts"] == 0 and result["restarts"] == 0, result
    if tx_mhz is not None:
        major = result["dn"] if tx_mhz > 120 else result["up"]
        assert major > 50, result          # the device CDR tracks the host's offset


def test_device_test_build_tags():
    import argparse
    from usb2soft.applets.device_test import DeviceTest
    p = argparse.ArgumentParser()
    DeviceTest.add_arguments(p)
    for argv, tag in (([], "-internal"), (["--mode", "async-slow"], "-async-slow"),
                      (["--mode", "hdmi", "--role", "host", "--hdmi-rx", "1"], "-hdmi-host-rx1")):
        args = p.parse_args(argv)
        assert DeviceTest.build_tag(args) == tag
        a = DeviceTest(args)
        a._MustUse__silence = True
        assert bool(a.vivado_constraints()) == args.mode.startswith("async-")
