"""USB CRC5/CRC16 and packet builders: catalogue check values, spec residuals, and LUNA's own
token detector / data receiver as the oracle for bit ordering on the wire."""
import pytest
from amaranth import Module, Elaboratable, Signal, ClockDomain
from amaranth.sim import Simulator

from usb2soft.sim import usb_crc
from usb2soft.sim.usb_crc import PID, crc5, crc16, token, sof, data, handshake, split_token


def test_catalogue_check_values():
    msg = b"123456789"
    assert crc16(msg) == 0xB4C8                                   # CRC-16/USB
    value = int.from_bytes(msg, "little")
    assert crc5(value, nbits=72) == 0x19                          # CRC-5/USB


def test_residuals():
    # Running the CRC over payload + appended CRC gives the constant residual (USB 2.0 8.3.5).
    for payload in (b"", b"\x00", b"\x01\x02\x03\x04", bytes(range(64))):
        pkt = data(PID.DATA0, payload)
        bits = [(b >> i) & 1 for b in pkt[1:] for i in range(8)]
        crc = 0xFFFF
        for b in bits:
            fb = ((crc >> 15) & 1) ^ b
            crc = (crc << 1) & 0xFFFF
            if fb:
                crc ^= 0x8005
        assert crc == usb_crc.CRC16_RESIDUAL
    for field in (0x000, 0x7FF, 0x15 | (0xE << 7), 0x3A | (0xA << 7)):
        body = field | (crc5(field) << 11)
        bits = [(body >> i) & 1 for i in range(16)]
        crc = 0x1F
        for b in bits:
            fb = ((crc >> 4) & 1) ^ b
            crc = (crc << 1) & 0x1F
            if fb:
                crc ^= 0x05
        assert crc == usb_crc.CRC5_RESIDUAL


def test_pid_bytes_and_builders():
    assert PID.byte(PID.IN) == 0x69 and PID.byte(PID.OUT) == 0xE1 and PID.byte(PID.SETUP) == 0x2D
    assert PID.byte(PID.SOF) == 0xA5 and PID.byte(PID.DATA0) == 0xC3 and PID.byte(PID.DATA1) == 0x4B
    assert PID.byte(PID.ACK) == 0xD2 and PID.byte(PID.NAK) == 0x5A and PID.byte(PID.STALL) == 0x1E
    assert handshake(PID.ACK) == b"\xD2"
    t = token(PID.IN, 5, 0)
    assert len(t) == 3 and t[0] == 0x69
    assert split_token(t) == (PID.IN, 5, 0, True)
    assert split_token(token(PID.SETUP, 0x7F, 0xF))[1:] == (0x7F, 0xF, True)
    assert data(PID.DATA1, b"") == bytes([0x4B, 0x00, 0x00])
    s = sof(0x123)
    assert split_token(s)[0] == PID.SOF and (s[1] | (s[2] << 8)) & 0x7FF == 0x123
    bad = bytearray(token(PID.OUT, 3, 2)); bad[2] ^= 0x80
    assert split_token(bytes(bad))[3] is False


class _TokenHarness(Elaboratable):
    """LUNA's USBTokenDetector on a bare UTMI record, driven by our bytes."""
    def __init__(self):
        from luna.gateware.interface.utmi import UTMIInterface
        from luna.gateware.usb.usb2.packet import USBTokenDetector
        self.utmi = UTMIInterface()
        self.det = USBTokenDetector(utmi=self.utmi, filter_by_address=False)

    def elaborate(self, platform):
        m = Module()
        m.domains.usb = ClockDomain("usb")
        m.submodules.det = self.det
        m.d.comb += self.det.speed.eq(0)      # USBSpeed.HIGH == 0 in LUNA
        return m


async def _drive_packet(ctx, utmi, pkt, gap=8):
    ctx.set(utmi.rx_active, 1)
    await ctx.tick("usb")
    for b in pkt:
        ctx.set(utmi.rx_data, b)
        ctx.set(utmi.rx_valid, 1)
        await ctx.tick("usb")
        ctx.set(utmi.rx_valid, 0)
        await ctx.tick("usb")
    ctx.set(utmi.rx_active, 0)
    for _ in range(gap):
        await ctx.tick("usb")


@pytest.mark.parametrize("pkt,pid,addr,ep", [
    (token(PID.IN, 5, 0), PID.IN, 5, 0),
    (token(PID.SETUP, 0, 0), PID.SETUP, 0, 0),
    (token(PID.OUT, 0x55, 0xA), PID.OUT, 0x55, 0xA),
    (sof(0x5A5), PID.SOF, 0x5A5 & 0x7F, 0x5A5 >> 7),
])
def test_luna_token_detector_accepts_our_tokens(pkt, pid, addr, ep):
    dut = _TokenHarness()
    seen = []

    async def tb(ctx):
        await _drive_packet(ctx, dut.utmi, pkt)

    async def watch(ctx):
        while True:
            if ctx.get(dut.det.interface.new_token) or ctx.get(dut.det.interface.new_frame):
                seen.append((ctx.get(dut.det.interface.pid), ctx.get(dut.det.interface.address),
                             ctx.get(dut.det.interface.endpoint), ctx.get(dut.det.interface.frame)))
            await ctx.tick("usb")

    sim = Simulator(dut)
    sim.add_clock(1 / 60e6, domain="usb")
    sim.add_testbench(tb)
    sim.add_testbench(watch, background=True)
    sim.run()
    assert len(seen) == 1, seen
    if pid == PID.SOF:
        assert seen[0][3] == 0x5A5
    else:
        assert seen[0][:3] == (pid, addr, ep)


def test_luna_token_detector_rejects_bad_crc():
    dut = _TokenHarness()
    seen = []
    bad = bytearray(token(PID.IN, 5, 0)); bad[2] ^= 0x40

    async def tb(ctx):
        await _drive_packet(ctx, dut.utmi, bytes(bad))

    async def watch(ctx):
        while True:
            if ctx.get(dut.det.interface.new_token):
                seen.append(1)
            await ctx.tick("usb")

    sim = Simulator(dut)
    sim.add_clock(1 / 60e6, domain="usb")
    sim.add_testbench(tb)
    sim.add_testbench(watch, background=True)
    sim.run()
    assert seen == []


class _DataHarness(Elaboratable):
    def __init__(self):
        from luna.gateware.interface.utmi import UTMIInterface
        from luna.gateware.usb.usb2.packet import USBDataPacketReceiver
        self.utmi = UTMIInterface()
        self.rx = USBDataPacketReceiver(utmi=self.utmi, standalone=True)

    def elaborate(self, platform):
        m = Module()
        m.domains.usb = ClockDomain("usb")
        m.submodules.rx = self.rx
        return m


@pytest.mark.parametrize("payload", [b"", b"\x00\x01\x02\x03", bytes(range(18)), bytes(range(64))])
def test_luna_data_receiver_accepts_our_crc16(payload):
    dut = _DataHarness()
    result = {"complete": 0, "mismatch": 0}

    async def tb(ctx):
        await _drive_packet(ctx, dut.utmi, data(PID.DATA1, payload), gap=16)

    async def watch(ctx):
        while True:
            result["complete"] += ctx.get(dut.rx.packet_complete)
            result["mismatch"] += ctx.get(dut.rx.crc_mismatch)
            await ctx.tick("usb")

    sim = Simulator(dut)
    sim.add_clock(1 / 60e6, domain="usb")
    sim.add_testbench(tb)
    sim.add_testbench(watch, background=True)
    sim.run()
    assert result == {"complete": 1, "mismatch": 0}, result


def test_luna_data_receiver_flags_bad_crc16():
    dut = _DataHarness()
    result = {"complete": 0, "mismatch": 0}
    pkt = bytearray(data(PID.DATA0, b"\x10\x20")); pkt[-1] ^= 1

    async def tb(ctx):
        await _drive_packet(ctx, dut.utmi, bytes(pkt), gap=16)

    async def watch(ctx):
        while True:
            result["complete"] += ctx.get(dut.rx.packet_complete)
            result["mismatch"] += ctx.get(dut.rx.crc_mismatch)
            await ctx.tick("usb")

    sim = Simulator(dut)
    sim.add_clock(1 / 60e6, domain="usb")
    sim.add_testbench(tb)
    sim.add_testbench(watch, background=True)
    sim.run()
    assert result["complete"] == 0 and result["mismatch"] >= 1, result
