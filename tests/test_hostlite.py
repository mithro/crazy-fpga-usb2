"""HostLite <-> host SoftUTMIPHY <-> wires <-> device SoftUTMIPHY <-> LUNA device (scaled timings)."""
import pytest
from amaranth import Module, Elaboratable, ClockDomain, Signal

from usb2soft.hostlite import HostLite, enumeration_script
from usb2soft.phy import SoftUTMIPHY, LineStateSynthesiser
from usb2soft.luna import SoftPHYUSBDevice, standard_descriptors
from usb2soft.sim.expander import SampleExpander
from tests.luna_harness import make_sim, scale_sequencer, SYNTH_SCALED


class LinkHarness(Elaboratable):
    """Two PHYs back to back through SampleExpander wires (all in the same 120 MHz domain)."""
    def __init__(self, *, sof_period=1500, start_delay=1200, reply_timeout=256):
        self.descriptors = standard_descriptors()
        self.dev_phy = SoftUTMIPHY(synthesiser=LineStateSynthesiser(**SYNTH_SCALED))
        self.device = SoftPHYUSBDevice(bus=self.dev_phy)
        self.device.add_standard_control_endpoint(self.descriptors)
        self.host_phy = SoftUTMIPHY()
        self.host = HostLite(enumeration_script(self.descriptors.get_descriptor_bytes(1)),
                             sof_period=sof_period, start_delay=start_delay, restart_delay=100,
                             reply_timeout=reply_timeout)
        self.h2d = SampleExpander(phase_shift=1, domain="rx_cdr")
        self.d2h = SampleExpander(phase_shift=2, domain="rx_cdr")
        self.inject = Signal(16)

    def elaborate(self, platform):
        m = Module()
        for d in ("usb", "rx_cdr", "tx_cdr"):
            m.domains += ClockDomain(d)
        m.submodules += [self.dev_phy, self.device, self.host_phy, self.host, self.h2d, self.d2h]
        m.d.comb += [
            self.device.connect.eq(1),
            # host-lite UTMI <-> host PHY
            self.host_phy.tx_data.eq(self.host.tx_data), self.host_phy.tx_valid.eq(self.host.tx_valid),
            self.host.tx_ready.eq(self.host_phy.tx_ready),
            self.host.rx_data.eq(self.host_phy.rx_data), self.host.rx_valid.eq(self.host_phy.rx_valid),
            self.host.rx_active.eq(self.host_phy.rx_active), self.host.rx_error.eq(self.host_phy.rx_error),
            # wires
            self.h2d.line.eq(self.host_phy.line), self.h2d.oe.eq(self.host_phy.oe),
            self.dev_phy.samples.eq(self.h2d.samples ^ self.inject),
            self.d2h.line.eq(self.dev_phy.line), self.d2h.oe.eq(self.dev_phy.oe),
            self.host_phy.samples.eq(self.d2h.samples),
        ]
        return m


def _stats(ctx, h):
    return {n: ctx.get(getattr(h.host, n)) for n in ("loops", "ok", "bad", "timeouts", "naks", "restarts", "sofs")}


def test_hostlite_enumerates_luna_device(monkeypatch):
    scale_sequencer(monkeypatch)
    dut = LinkHarness()
    result = {}

    async def tb(ctx):
        for _ in range(9000):
            await ctx.tick("usb")
        result.update(_stats(ctx, dut))
        result["speed"] = ctx.get(dut.device.speed)

    sim = make_sim(dut)
    sim.add_testbench(tb)
    sim.run()
    assert result["speed"] == 0, result                      # USBSpeed.HIGH
    assert result["loops"] >= 3, result
    assert result["bad"] == 0 and result["timeouts"] == 0 and result["restarts"] == 0, result
    assert result["sofs"] >= 3, result
    print("\nhost-lite:", result)


def test_hostlite_recovers_from_a_corrupted_packet(monkeypatch):
    scale_sequencer(monkeypatch)
    dut = LinkHarness()
    before, after = {}, {}

    async def tb(ctx):
        for _ in range(4500):
            await ctx.tick("usb")
        before.update(_stats(ctx, dut))
        # corrupt the host->device wire: one inverted sample word every 8 usb cycles for 160 cycles
        # (several host packets are hit; the device sees stuffing/CRC errors or nothing)
        for _ in range(20):
            ctx.set(dut.inject, 0xFFFF)
            await ctx.tick("rx_cdr")
            ctx.set(dut.inject, 0)
            for _ in range(8):
                await ctx.tick("usb")
        for _ in range(4500):
            await ctx.tick("usb")
        after.update(_stats(ctx, dut))

    sim = make_sim(dut)
    sim.add_testbench(tb)
    sim.run()
    assert before["loops"] >= 1 and before["bad"] + before["timeouts"] == 0, before
    faults = (after["bad"] + after["timeouts"]) - (before["bad"] + before["timeouts"])
    assert faults >= 1, (before, after)
    assert after["loops"] >= before["loops"] + 5, (before, after)   # kept going afterwards
    assert after["restarts"] <= 1, after
