import random
import pytest
from amaranth.sim import Simulator
from usb2soft.tx.encoder import PacketEncoder
from usb2soft.sim import usbhs


def drive(payloads, *, gap_cycles=8, stall_every=None):
    """Feed packets as byte entries (+ end markers); return (dut, line_bits, oe_bits, ready_cycles)."""
    dut = PacketEncoder()
    line, oe, ready_at = [], [], []
    entries = []
    for p in payloads:
        entries += [(b, 0) for b in p] + [(0, 1)]

    async def feeder(ctx):
        # valid/ready idiom: present the entry, read the combinational ready, then tick.
        idx = 0
        cycle = 0
        while idx < len(entries):
            b, end = entries[idx]
            stalled = bool(stall_every) and (cycle % stall_every == 0)
            ctx.set(dut.byte_valid, 0 if stalled else 1)
            ctx.set(dut.byte, b)
            ctx.set(dut.byte_end, end)
            consumed = (not stalled) and ctx.get(dut.byte_ready)
            await ctx.tick()
            cycle += 1
            if consumed:
                ready_at.append(cycle)
                idx += 1
                if end:
                    ctx.set(dut.byte_valid, 0)
                    for _ in range(gap_cycles):
                        await ctx.tick()
                        cycle += 1
        ctx.set(dut.byte_valid, 0)
        for _ in range(12):
            await ctx.tick()
        flags["underrun"] = ctx.get(dut.underrun)

    async def sampler(ctx):
        while True:
            l, o = ctx.get(dut.line), ctx.get(dut.oe)
            for i in range(4):
                line.append((l >> i) & 1)
                oe.append((o >> i) & 1)
            await ctx.tick()

    flags = {}
    sim = Simulator(dut)
    sim.add_clock(1 / 120e6)
    sim.add_testbench(feeder)
    sim.add_testbench(sampler, background=True)
    sim.run()
    return flags, line, oe, ready_at


def driven_segments(line, oe):
    """Split the sampled stream into the line bits emitted while oe was high."""
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


@pytest.mark.parametrize("payload", [b"\x5a", bytes(range(16)), b"\xff" * 40,
                                     bytes(random.Random(1).randrange(256) for _ in range(300))])
def test_packet_bits_match_model_exactly(payload):
    flags, line, oe, _ = drive([payload])
    segs = driven_segments(line, oe)
    assert len(segs) == 1
    assert segs[0] == usbhs.packet_line_bits(payload, sync_bits=32)
    assert flags["underrun"] == 0


def test_two_packets_and_reference_decode():
    p1, p2 = bytes(range(8)), b"\x00\xff" * 10
    _, line, oe, _ = drive([p1, p2], gap_cycles=16)
    segs = driven_segments(line, oe)
    assert [usbhs.reference_decode([1] * 8 + s + [1] * 8) for s in segs] == [[p1], [p2]]


def test_oe_is_low_between_packets_and_bit_exact_at_eop():
    payload = bytes([0x0F, 0xF0, 0xAA])
    _, line, oe, _ = drive([payload])
    expected = usbhs.packet_line_bits(payload)
    first = oe.index(1)
    assert oe[first:first + len(expected)] == [1] * len(expected)
    assert oe[first + len(expected)] == 0          # driver off on the very next bit


def test_source_every_other_cycle_sustains_line_rate():
    # 8 bits every two cycles is exactly the 4 bits/cycle line rate: no truncation, no underrun
    payload = bytes(range(64))
    flags, line, oe, _ = drive([payload], stall_every=2)
    segs = driven_segments(line, oe)
    assert len(segs) == 1 and segs[0] == usbhs.packet_line_bits(payload)
    assert flags["underrun"] == 0


def test_starved_source_closes_packet_contiguously_and_flags_underrun():
    """The source stops after 16 bytes without an end marker: one contiguous oe segment, a
    well-formed strict prefix of the payload, and the sticky underrun flag."""
    payload = bytes(range(64))
    dut = PacketEncoder()
    line, oe = [], []

    async def feeder(ctx):
        for b in payload[:16]:
            ctx.set(dut.byte_valid, 1)
            ctx.set(dut.byte, b)
            while not ctx.get(dut.byte_ready):
                await ctx.tick()
            await ctx.tick()
        ctx.set(dut.byte_valid, 0)
        for _ in range(60):
            await ctx.tick()
        flags["underrun"] = ctx.get(dut.underrun)

    async def sampler(ctx):
        while True:
            l, o = ctx.get(dut.line), ctx.get(dut.oe)
            for i in range(4):
                line.append((l >> i) & 1)
                oe.append((o >> i) & 1)
            await ctx.tick()

    flags = {}
    sim = Simulator(dut)
    sim.add_clock(1 / 120e6)
    sim.add_testbench(feeder)
    sim.add_testbench(sampler, background=True)
    sim.run()
    assert flags["underrun"] == 1
    segs = driven_segments(line, oe)
    assert len(segs) == 1                                   # no oe hole before the EOP
    decoded = usbhs.reference_decode([1] * 8 + segs[0] + [1] * 8)
    assert decoded and 0 < len(decoded[0]) < len(payload) and payload.startswith(decoded[0])


@pytest.mark.parametrize("payload", [b"\x00\xfc", b"\x7f\x01", b"\xff\xff", b"\xfe\xff\x01"])
def test_stuffing_boundary_cases(payload):
    flags, line, oe, _ = drive([payload])
    assert driven_segments(line, oe) == [usbhs.packet_line_bits(payload)]
    assert flags["underrun"] == 0


def test_underrun_flag():
    dut = PacketEncoder()
    seen = []

    async def tb(ctx):
        ctx.set(dut.byte_valid, 1)
        ctx.set(dut.byte, 0x3C)
        while not ctx.get(dut.byte_ready):
            await ctx.tick()
        await ctx.tick()                          # the byte is consumed on this edge
        ctx.set(dut.byte_valid, 0)                # starve it mid-packet
        for _ in range(40):
            await ctx.tick()
        seen.append(ctx.get(dut.underrun))
        seen.append(ctx.get(dut.busy))

    sim = Simulator(dut)
    sim.add_clock(1 / 120e6)
    sim.add_testbench(tb)
    sim.run()
    assert seen == [1, 0]
