"""LUNA's USBDevice on the soft PHY: the reset/chirp walk to high speed and recovery after idle."""
import pytest

from luna.gateware.usb.usb2 import USBSpeed

from usb2soft.sim.usb_crc import sof
from tests.luna_harness import (LunaDeviceHarness, make_sim, scale_sequencer, packet_words,
                                play_words, SCALED)


def _runs(seq):
    out = []
    for v in seq:
        if out and out[-1][0] == v:
            out[-1][1] += 1
        else:
            out.append([v, 1])
    return [tuple(r) for r in out]


def test_reset_walk_reaches_high_speed_and_recovers_after_idle(monkeypatch):
    scale_sequencer(monkeypatch)
    dut = LunaDeviceHarness()
    trace = []                     # (op_mode, tx_valid, speed, term_select) per usb cycle
    state = {"traffic": True}
    sof_words = packet_words(sof(0x001))

    async def traffic(ctx):
        # a host keeps SOFs flowing; period well under the (scaled) 3 ms squelch limit
        while True:
            for _ in range(1500):
                await ctx.tick("usb")
            if state["traffic"]:
                await play_words(ctx, dut.phy, sof_words)

    async def watch(ctx):
        while True:
            trace.append((ctx.get(dut.phy.op_mode), ctx.get(dut.phy.tx_valid),
                          ctx.get(dut.device.speed), ctx.get(dut.phy.term_select)))
            await ctx.tick("usb")

    async def tb(ctx):
        for _ in range(5000):          # SE0 -> chirp -> K/J -> HS, then hold with traffic
            await ctx.tick("usb")
        assert (ctx.get(dut.device.speed), ctx.get(dut.phy.op_mode), ctx.get(dut.phy.term_select)) == (USBSpeed.HIGH, 0, 0)
        state["traffic"] = False
        # no traffic: 3 ms of squelch -> FS -> 200 us -> still SE0 -> bus reset -> chirp again
        for _ in range(SCALED["_CYCLES_3_MILLISECONDS"] + SCALED["_CYCLES_200_MICROSECONDS"] + 1500):
            await ctx.tick("usb")
        state["traffic"] = True
        for _ in range(3000):
            await ctx.tick("usb")
        assert (ctx.get(dut.device.speed), ctx.get(dut.phy.op_mode)) == (USBSpeed.HIGH, 0)

    sim = make_sim(dut)
    sim.add_testbench(tb)
    sim.add_testbench(traffic, background=True)
    sim.add_testbench(watch, background=True)
    sim.run()

    op_runs = _runs([t[0] for t in trace])
    chirps = [r for r in op_runs if r[0] == 2]
    assert len(chirps) == 2, op_runs                       # power-up chirp and the re-chirp
    # each chirp lasted the (scaled) 2 ms with tx_valid held high, and op_mode returned to NORMAL
    for start in (i for i, t in enumerate(trace) if t[0] == 2 and (i == 0 or trace[i - 1][0] != 2)):
        seg = trace[start:start + SCALED["_CYCLES_2_MILLISECONDS"] - 2]
        assert all(t[1] == 1 for t in seg[2:]), "tx_valid must stay high through the chirp"
    # speed was FULL at power-up, HIGH after the first walk, FULL again during the idle drop, HIGH at the end
    speeds = [r[0] for r in _runs([t[2] for t in trace])]
    assert speeds[0] == USBSpeed.FULL and speeds[-1] == USBSpeed.HIGH
    assert speeds.count(USBSpeed.HIGH) >= 2, speeds
    assert dut.phy.synthesiser is not None


from usb2soft.sim.host import HSHostModel, HostError, GET_DESCRIPTOR_DEVICE, SET_ADDRESS, HS_DEVICE_RESPONSE_BITS
from usb2soft.sim.usb_crc import PID, token


async def _walk_to_high_speed(ctx, dut, host):
    """Power-up SE0 -> chirp -> K/J -> HS (scaled), keeping SOFs flowing at transaction gaps."""
    for _ in range(6):
        for _ in range(500):
            await ctx.tick("usb")
        await host.send_sof(ctx)
    assert ctx.get(dut.device.speed) == USBSpeed.HIGH and ctx.get(dut.phy.op_mode) == 0


@pytest.mark.parametrize("ppm", [0, 500, -500])
def test_enumeration_over_the_soft_phy(monkeypatch, ppm):
    scale_sequencer(monkeypatch)
    dut = LunaDeviceHarness()
    host = HSHostModel(dut.phy, ppm=ppm)
    result = {}
    expected_dev = bytes(dut.descriptors.get_descriptor_bytes(1))

    async def tb(ctx):
        await _walk_to_high_speed(ctx, dut, host)
        desc = await host.control_transfer(ctx, 0, GET_DESCRIPTOR_DEVICE(64), read_length=64)
        assert desc == expected_dev, (desc.hex(), expected_dev.hex())
        await host.send_sof(ctx)
        await host.control_transfer(ctx, 0, SET_ADDRESS(5))
        await host.send_sof(ctx)
        desc5 = await host.control_transfer(ctx, 5, GET_DESCRIPTOR_DEVICE(18), read_length=18)
        assert desc5 == expected_dev
        # the old address no longer answers
        await host.send(ctx, token(PID.IN, 0, 0))
        assert await host.receive(ctx) is None
        result["latencies"] = list(host.latencies)
        result["speed"] = ctx.get(dut.device.speed)

    sim = make_sim(dut)
    sim.add_testbench(tb)
    sim.add_testbench(host.collector, background=True)
    sim.run()
    lat = result["latencies"]
    assert lat and max(lat) <= HS_DEVICE_RESPONSE_BITS, lat
    assert result["speed"] == USBSpeed.HIGH
    result["max_latency_bits"] = max(lat)
    print(f"\nppm={ppm}: {len(lat)} responses, latency min/max {min(lat)}/{max(lat)} bit times")


def test_plain_usbdevice_stays_full_speed(monkeypatch):
    """Guards the SoftPHYUSBDevice rationale: LUNA's USBDevice on a bare UTMI object never
    attempts high-speed detection (always_fs=True)."""
    from luna.gateware.usb.usb2.device import USBDevice
    from usb2soft.luna import standard_descriptors
    from tests.luna_harness import SYNTH_SCALED
    from usb2soft.phy import SoftUTMIPHY, LineStateSynthesiser
    from amaranth import Module, Elaboratable, ClockDomain

    scale_sequencer(monkeypatch)

    class Plain(Elaboratable):
        def __init__(self):
            self.phy = SoftUTMIPHY(synthesiser=LineStateSynthesiser(**SYNTH_SCALED))
            self.device = USBDevice(bus=self.phy)
            self.device.add_standard_control_endpoint(standard_descriptors())

        def elaborate(self, platform):
            m = Module()
            for d in ("usb", "rx_cdr", "tx_cdr"):
                m.domains += ClockDomain(d)
            m.submodules.phy = self.phy
            m.submodules.device = self.device
            m.d.comb += self.device.connect.eq(1)
            return m

    dut = Plain()
    seen = set()

    async def tb(ctx):
        for _ in range(1500):
            seen.add((ctx.get(dut.phy.op_mode), ctx.get(dut.device.speed)))
            await ctx.tick("usb")

    sim = make_sim(dut)
    sim.add_testbench(tb)
    sim.run()
    assert all(op == 0 for op, _ in seen), seen           # never chirps
    assert all(sp == USBSpeed.FULL for _, sp in seen)
