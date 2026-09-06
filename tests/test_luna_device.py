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
