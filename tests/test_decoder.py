import random
import pytest
from amaranth.sim import Simulator

from usb2soft.rx.decoder import PacketDecoder, Event
from usb2soft.sim import usbhs


def feed(line_bits, *, chunk=5, activity_drop_at=None):
    """Drive line bits into the decoder ``chunk`` at a time; return the event/byte trace."""
    dut = PacketDecoder(max_bits=5)
    trace = []
    pieces = [line_bits[i:i + chunk] for i in range(0, len(line_bits), chunk)]

    async def tb(ctx):
        ctx.set(dut.activity, 1)
        for n, piece in enumerate(pieces):
            ctx.set(dut.bits, sum(b << i for i, b in enumerate(piece)))
            ctx.set(dut.count, len(piece))
            if activity_drop_at is not None and n >= activity_drop_at:
                ctx.set(dut.activity, 0)
            await ctx.tick()
            _record(ctx, dut, trace)
        ctx.set(dut.count, 0)
        for _ in range(4):
            await ctx.tick()
            _record(ctx, dut, trace)

    sim = Simulator(dut)
    sim.add_clock(1 / 120e6)
    sim.add_testbench(tb)
    sim.run()
    return trace


def _record(ctx, dut, trace):
    # Within one word a completed byte always precedes an END/ERROR (a START cannot share a word
    # with a byte), so log data first when both appear in the same cycle.
    if ctx.get(dut.data_valid):
        trace.append(("data", ctx.get(dut.data)))
    if ctx.get(dut.event_valid):
        trace.append(("evt", Event(ctx.get(dut.event))))


def packets_from(trace):
    pkts, cur = [], None
    for kind, v in trace:
        if kind == "evt" and v == Event.START:
            cur = bytearray()
        elif kind == "data":
            assert cur is not None, "data outside a packet"
            cur.append(v)
        elif kind == "evt" and v == Event.END:
            pkts.append(bytes(cur))
            cur = None
        elif kind == "evt" and v == Event.ERROR:
            pkts.append(None)
            cur = None
    return pkts


@pytest.mark.parametrize("sync", [12, 16, 20, 32])
@pytest.mark.parametrize("chunk", [1, 3, 4, 5])
def test_packet_any_sync_length_and_chunking(sync, chunk):
    payload = bytes(random.Random(sync + chunk).randrange(256) for _ in range(40))
    line = [1] * 10 + usbhs.packet_line_bits(payload, sync_bits=sync) + [1] * 10
    assert packets_from(feed(line, chunk=chunk)) == [payload]


def test_all_ones_payload_is_unstuffed():
    payload = b"\xff" * 32
    line = [1] * 10 + usbhs.packet_line_bits(payload) + [1] * 10
    assert packets_from(feed(line)) == [payload]


def test_zero_payload_and_short_packets():
    for payload in (b"", b"\x5a", b"\x00" * 3, bytes([0x2D, 0x00, 0x10])):   # incl. a token-like one
        line = [1] * 10 + usbhs.packet_line_bits(payload) + [1] * 10
        assert packets_from(feed(line)) == [payload]


def test_back_to_back_packets_with_minimal_gap():
    p1, p2 = bytes(range(8)), bytes(range(8, 24))
    line = [1] * 10 + usbhs.packet_line_bits(p1) + [1] * 8 + usbhs.packet_line_bits(p2) + [1] * 10
    assert packets_from(feed(line)) == [p1, p2]


def test_misaligned_eop_reports_error():
    payload = bytes(range(8))
    line = usbhs.packet_line_bits(payload)
    # corrupt: insert one extra data bit before the EOP so the byte boundary is off by one
    line = [1] * 10 + line[:-8] + [line[-9]] + line[-8:] + [1] * 10
    got = packets_from(feed(line))
    assert got == [None]


def test_activity_loss_mid_packet_reports_error():
    payload = bytes(range(64))
    line = [1] * 10 + usbhs.packet_line_bits(payload)
    trace = feed(line, activity_drop_at=40)
    kinds = [v for k, v in trace if k == "evt"]
    assert kinds[0] == Event.START and kinds[-1] == Event.ERROR


def test_short_zero_run_is_not_a_sync():
    # data 0000001 (six zeros then one) must not start a packet
    line = [1] * 10 + usbhs.nrzi_encode([0] * 6 + [1] + [1] * 20, initial=1)
    assert feed(line) == []
