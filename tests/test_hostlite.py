"""HostLite <-> host SoftUTMIPHY <-> wires <-> device SoftUTMIPHY <-> LUNA device (scaled timings)."""
import pytest
from amaranth import Module, Elaboratable, ClockDomain, Signal, Mux

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
                             sof_period=sof_period, start_delay=start_delay, restart_delay=4200,
                             reply_timeout=reply_timeout)
        self.h2d = SampleExpander(phase_shift=1, domain="rx_cdr")
        self.d2h = SampleExpander(phase_shift=2, domain="rx_cdr")
        self.inject = Signal(16)
        self.cut = Signal()            # 1: device->host wire replaced by idle J

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
            self.host_phy.samples.eq(Mux(self.cut, 0xFFFF, self.d2h.samples)),
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


def test_hostlite_counts_bad_replies_and_restarts(monkeypatch):
    """A script expecting a wrong descriptor: every IN reply is 'bad', and after max_faults the
    host restarts (goes silent, device re-chirps, script from step 0)."""
    from usb2soft.hostlite import enumeration_script, HostLite
    scale_sequencer(monkeypatch)
    dut = LinkHarness()
    wrong = bytearray(dut.descriptors.get_descriptor_bytes(1)); wrong[8] ^= 0xFF
    dut.host = HostLite(enumeration_script(bytes(wrong)), sof_period=1500, start_delay=1200,
                        restart_delay=4200, reply_timeout=256)
    result = {}

    async def tb(ctx):
        for _ in range(14000):
            await ctx.tick("usb")
        result.update(_stats(ctx, dut))
        result["chirps"] = ctx.get(dut.dev_phy.synthesiser.chirps)

    sim = make_sim(dut)
    sim.add_testbench(tb)
    sim.run()
    assert result["bad"] >= 4 and result["loops"] == 0, result
    assert result["restarts"] >= 1, result
    assert result["chirps"] >= 2, result           # the silence made the device re-enumerate


def test_hostlite_recovers_after_a_dead_link(monkeypatch):
    """Device->host wire cut long enough for max_faults timeouts: the host restarts, stays silent
    until the device has re-chirped, and the steady-state loop resumes at address 5."""
    from amaranth import Signal
    scale_sequencer(monkeypatch)
    dut = LinkHarness()
    result = {}

    async def tb(ctx):
        for _ in range(4500):
            await ctx.tick("usb")
        before = _stats(ctx, dut)
        assert before["loops"] >= 1 and before["timeouts"] == 0, before
        # break device->host: the host PHY sees idle J for 1500 usb cycles
        ctx.set(dut.cut, 1)
        for _ in range(1500):
            await ctx.tick("usb")
        ctx.set(dut.cut, 0)
        for _ in range(12000):
            await ctx.tick("usb")
        after = _stats(ctx, dut)
        result.update(before=before, after=after, chirps=ctx.get(dut.dev_phy.synthesiser.chirps))

    sim = make_sim(dut)
    sim.add_testbench(tb)
    sim.run()
    b, a = result["before"], result["after"]
    assert a["timeouts"] >= 4 and a["restarts"] >= 1, result
    assert result["chirps"] >= 2, result
    assert a["loops"] >= b["loops"] + 5, result


def test_device_counts_hostlite_sofs(monkeypatch):
    """LUNA accepts the gateware SOFs (CRC5 from the linear-term generator): sof_detected == sofs
    and the frame number advances every 8 microframes."""
    scale_sequencer(monkeypatch)
    dut = LinkHarness(sof_period=300)
    result = {"detected": 0, "frames": set()}

    async def watch(ctx):
        while True:
            if ctx.get(dut.device.sof_detected):
                result["detected"] += 1
                result["frames"].add(ctx.get(dut.device.frame_number))
            await ctx.tick("usb")

    async def tb(ctx):
        for _ in range(7000):
            await ctx.tick("usb")
        result["sofs"] = ctx.get(dut.host.sofs)

    sim = make_sim(dut)
    sim.add_testbench(tb)
    sim.add_testbench(watch, background=True)
    sim.run()
    assert result["sofs"] >= 12
    assert result["detected"] == result["sofs"], result
    assert len(result["frames"]) >= 2, result


@pytest.mark.slow
def test_hostlite_enumerates_with_real_luna_timings():
    """Unscaled: 5 us SE0, 2 ms device chirp, 125 us SOFs, 3 ms hold-off (~4 M usb cycles)."""
    from usb2soft.applets.device_test import SYNTH_REAL, HOST_REAL
    from usb2soft.hostlite import HostLite, enumeration_script
    from usb2soft.phy import LineStateSynthesiser
    dut = LinkHarness()
    dut.dev_phy.synthesiser = LineStateSynthesiser(**SYNTH_REAL)
    dut.host = HostLite(enumeration_script(dut.descriptors.get_descriptor_bytes(1)), **HOST_REAL)
    result = {}

    async def tb(ctx):
        for _ in range(240_000):           # 4 ms: SE0, chirp (2 ms), K/J, hold-off ends at 3 ms
            await ctx.tick("usb")
        result.update(_stats(ctx, dut))
        result["chirps"] = ctx.get(dut.dev_phy.synthesiser.chirps)

    sim = make_sim(dut)
    sim.add_testbench(tb)
    sim.run()
    assert result["chirps"] == 1 and result["loops"] >= 10, result
    assert result["bad"] == 0 and result["timeouts"] == 0 and result["restarts"] == 0, result
