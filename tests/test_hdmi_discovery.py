from amaranth import Module, Elaboratable
from amaranth.sim import Simulator
from usb2soft.applets.hdmi_discovery import LaneBeacon, LaneListener, frame_bytes, DiscoveryCore

DIV = 4


def test_frame_bytes_checksum():
    assert frame_bytes(0x123456, port=1, lane=2) == bytes([0xA5, 0x12, 0x34, 0x56, 0x12, 0x12 ^ 0x34 ^ 0x56 ^ 0x12])


class _Pair(Elaboratable):
    """Beacon -> (optional inversion) -> listener."""
    def __init__(self, invert):
        self.invert = invert
        self.beacon = LaneBeacon(divisor=DIV, own_id=0xABCDEF, port=1, lane=3)
        self.listener = LaneListener(divisor=DIV, timeout_cycles=400)

    def elaborate(self, platform):
        m = Module()
        m.submodules.b = self.beacon
        m.submodules.l = self.listener
        m.d.comb += self.listener.rx.eq(self.beacon.tx ^ self.invert)
        return m


def _run_pair(invert):
    dut = _Pair(invert)
    result = {}

    async def tb(ctx):
        for _ in range(3000):
            await ctx.tick()
            if ctx.get(dut.listener.seen):
                result["id"] = ctx.get(dut.listener.peer_id)
                result["pl"] = ctx.get(dut.listener.peer_pl)
                result["inv"] = ctx.get(dut.listener.inverted)
                break
        else:
            raise AssertionError("listener never saw a frame")
        # Beacon keeps running: 'seen' stays high.
        for _ in range(1000):
            await ctx.tick()
        assert ctx.get(dut.listener.seen) == 1

    sim = Simulator(dut)
    sim.add_clock(1 / 60e6)
    sim.add_testbench(tb)
    sim.run()
    return result


def test_listener_decodes_true_polarity():
    r = _run_pair(invert=0)
    assert r == {"id": 0xABCDEF, "pl": 0x13, "inv": 0}


def test_listener_decodes_inverted_lane():
    r = _run_pair(invert=1)
    assert r == {"id": 0xABCDEF, "pl": 0x13, "inv": 1}


async def _send_frames(ctx, dut, frames):
    ctx.set(dut.rx, 1)
    for _ in range(4 * DIV):
        await ctx.tick()
    for seq in frames:
        for b in seq:
            for bit in [0] + [(b >> i) & 1 for i in range(8)] + [1]:
                ctx.set(dut.rx, bit)
                for _ in range(DIV):
                    await ctx.tick()
    ctx.set(dut.rx, 1)
    for _ in range(4):
        await ctx.tick()


def test_seen_times_out_when_lane_goes_quiet():
    # timeout must exceed one frame period (6 bytes x 10 bits x DIV = 240 cycles), as on hardware
    dut = LaneListener(divisor=DIV, timeout_cycles=600)
    seq = frame_bytes(0x010203, port=0, lane=0)

    async def tb(ctx):
        await _send_frames(ctx, dut, [seq, seq])   # two agreeing frames are required
        assert ctx.get(dut.seen) == 1
        for _ in range(660):
            await ctx.tick()
        assert ctx.get(dut.seen) == 0

    sim = Simulator(dut)
    sim.add_clock(1 / 60e6)
    sim.add_testbench(tb)
    sim.run()


def test_single_or_disagreeing_frames_are_ignored():
    dut = LaneListener(divisor=DIV, timeout_cycles=600)
    noise = bytes([0xA5, 0x00, 0x80, 0x80, 0x00, 0x00])     # valid checksum, seen on hardware
    other = frame_bytes(0x010203, port=0, lane=1)

    async def tb(ctx):
        await _send_frames(ctx, dut, [noise, other])
        assert ctx.get(dut.seen) == 0

    sim = Simulator(dut)
    sim.add_clock(1 / 60e6)
    sim.add_testbench(tb)
    sim.run()


def test_core_reports_lines():
    core = DiscoveryCore(own_id=0x111111, divisor=DIV, timeout_cycles=400, report_period=2500, n_ports=1)
    text = bytearray()

    async def loop(ctx):
        # Cross-connect port 0 TX lanes to port 0 RX lanes with lane 1 inverted.
        for _ in range(9000):
            for lane in range(4):
                ctx.set(core.rx[0][lane], ctx.get(core.tx[0][lane]) ^ (lane == 1))
            await ctx.tick()

    async def uart_rx(ctx):
        while len(text) < 400:
            while ctx.get(core.uart_tx):
                await ctx.tick()
            for _ in range(DIV + DIV // 2):
                await ctx.tick()
            byte = 0
            for i in range(8):
                byte |= ctx.get(core.uart_tx) << i
                for _ in range(DIV):
                    await ctx.tick()
            text.append(byte)

    sim = Simulator(core)
    sim.add_clock(1 / 60e6)
    sim.add_testbench(loop)
    sim.add_testbench(uart_rx, background=True)
    sim.run_until(9000 / 60e6)
    lines = bytes(text).decode(errors="replace").splitlines()
    assert any(l.startswith("D 111111 HELLO") for l in lines)
    assert "D 111111 R01 111111 01 03" in lines     # lane 1: seen + inverted
    assert "D 111111 R02 111111 02 01" in lines     # lane 2: seen, true polarity
