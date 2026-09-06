"""A bit-level USB high-speed host for simulation.

``HSHostModel`` talks to a ``SoftUTMIPHY`` through its *wire* side only: it plays 16-sample words
into ``phy.samples`` (host → device, through the wire model with an optional ppm offset) and
decodes the PHY's ``line``/``oe`` output (device → host) with the reference decoder. On top of
that it scripts tokens, data, handshakes and control transfers, retries on NAK, and measures the
device's response latency in bit times.
"""
from . import usbhs
from .usb_crc import PID, token, sof, data, handshake

__all__ = ["HSHostModel", "HostError", "GET_DESCRIPTOR_DEVICE", "SET_ADDRESS"]

HS_RESPONSE_TIMEOUT_BITS = 736        # USB 2.0 7.1.19.2, host waiting for a device response
HS_DEVICE_RESPONSE_BITS = 192         # the device must start its reply within this (7.1.19.2)
MIN_GAP_BITS = 16                     # host-side inter-packet gap we leave after a device packet
SAME_SOURCE_GAP_BITS = 96             # token -> DATA from the same transmitter: >= 88 (7.1.18.2)


class HostError(AssertionError):
    pass


def setup_bytes(bmRequestType, bRequest, wValue, wIndex, wLength):
    return bytes([bmRequestType, bRequest, wValue & 0xFF, wValue >> 8, wIndex & 0xFF, wIndex >> 8,
                  wLength & 0xFF, wLength >> 8])


def GET_DESCRIPTOR_DEVICE(length=64):
    return setup_bytes(0x80, 0x06, 0x0100, 0, length)


def SET_ADDRESS(address):
    return setup_bytes(0x00, 0x05, address, 0, 0)


class HSHostModel:
    def __init__(self, phy, *, ppm=0.0, sync_bits=32, samples_per_ui=4, rx_domain="rx_cdr",
                 tx_domain="tx_cdr"):
        self.phy = phy
        self.ppm = ppm
        self.sync_bits = sync_bits
        self.S = samples_per_ui
        self.rx_domain, self.tx_domain = rx_domain, tx_domain
        self.sampler = usbhs.LineSampler(samples_per_ui=samples_per_ui, ppm=ppm)
        self.received = []            # decoded device packets: (bytes, start_cycle)
        self.latencies = []           # bit times from our last sample to the device's first driven bit
        self.frame = 0
        self._cycle = 0               # tx-domain cycle counter (collector)
        self._sent_end_cycle = None
        self._line, self._oe = [], []

    # -- wire side -------------------------------------------------------------------------
    async def collector(self, ctx):
        """Background: record the device PHY's line output and decode complete packets."""
        driving = False
        cur = []
        while True:
            l, o = ctx.get(self.phy.line), ctx.get(self.phy.oe)
            for i in range(4):
                if (o >> i) & 1:
                    if not driving:
                        driving = True
                        cur = []
                        if self._sent_end_cycle is not None:
                            self.latencies.append((self._cycle - self._sent_end_cycle) * 4 + i)
                            self._sent_end_cycle = None
                    cur.append((l >> i) & 1)
                elif driving:
                    driving = False
                    self.received.append((self._decode(cur), self._cycle))
            self._cycle += 1
            await ctx.tick(self.tx_domain)

    def _decode(self, line_bits):
        # idle J precedes the burst, so decode NRZI relative to J
        packets = usbhs.reference_decode([1] + list(line_bits), min_sync_zeros=8)
        if len(packets) != 1:
            return b"<undecodable burst of %d bits, %d packets>" % (len(line_bits), len(packets))
        return bytes(packets[0])

    async def send(self, ctx, packet, *, idle_words=2):
        bits = usbhs.packet_line_bits(bytes(packet), sync_bits=self.sync_bits)
        samples = self.sampler.sample(bits)
        words = [0xFFFF] * idle_words + list(usbhs.words(samples, 4 * self.S))
        for w in words:
            ctx.set(self.phy.samples, w)
            await ctx.tick(self.rx_domain)
        ctx.set(self.phy.samples, 0xFFFF)
        # collector runs in the tx domain at the same rate (both 120 MHz here)
        self._sent_end_cycle = self._cycle

    async def receive(self, ctx, timeout_bits=HS_RESPONSE_TIMEOUT_BITS):
        """Wait for the next decoded device packet; None on timeout (bit times from now)."""
        n = len(self.received)
        for _ in range(timeout_bits // (4 * 1) + 1):
            if len(self.received) > n:
                pkt = self.received[-1][0]
                await self.idle(ctx, MIN_GAP_BITS)
                return pkt
            await ctx.tick(self.tx_domain)
        return None

    async def idle(self, ctx, bits):
        for _ in range(max(1, bits // 4)):
            await ctx.tick(self.rx_domain)

    # -- protocol --------------------------------------------------------------------------
    async def send_sof(self, ctx):
        self.frame = (self.frame + 1) & 0x7FF
        await self.send(ctx, sof(self.frame))

    async def expect(self, ctx, what, timeout_bits=HS_RESPONSE_TIMEOUT_BITS):
        pkt = await self.receive(ctx, timeout_bits)
        if pkt is None:
            raise HostError(f"timeout waiting for {what}")
        return pkt

    async def transaction_out(self, ctx, pid, address, endpoint, data_pid, payload, *, retries=8):
        """token + DATAx, expect ACK (retry on NAK). Returns the handshake PID."""
        for _ in range(retries):
            await self.send(ctx, token(pid, address, endpoint))
            await self.idle(ctx, SAME_SOURCE_GAP_BITS)
            await self.send(ctx, data(data_pid, payload))
            hs = await self.expect(ctx, "handshake")
            if hs == handshake(PID.NAK):
                await self.idle(ctx, 64)
                continue
            if hs != handshake(PID.ACK):
                raise HostError(f"unexpected reply to {pid:#x}: {hs.hex()}")
            return PID.ACK
        raise HostError("NAKed too often")

    async def transaction_in(self, ctx, address, endpoint, *, retries=8):
        """IN, expect DATAx (retry on NAK), ACK it. Returns (data_pid, payload)."""
        for _ in range(retries):
            await self.send(ctx, token(PID.IN, address, endpoint))
            pkt = await self.expect(ctx, "DATA")
            if pkt == handshake(PID.NAK):
                await self.idle(ctx, 64)
                continue
            pid = pkt[0] & 0xF
            if pid not in (PID.DATA0, PID.DATA1):
                raise HostError(f"unexpected reply to IN: {pkt.hex()}")
            body, crc = pkt[1:-2], pkt[-2:]
            if data(pid, body)[-2:] != crc:
                raise HostError(f"bad CRC16 on {pkt.hex()}")
            await self.send(ctx, handshake(PID.ACK))
            return pid, bytes(body)
        raise HostError("IN NAKed too often")

    async def control_transfer(self, ctx, address, setup, *, read_length=0):
        """SETUP/DATA0 (ACK); data stage IN packets (DATA1, DATA0, ...) up to ``read_length`` or a
        short packet; status stage (OUT ZLP DATA1 → ACK, or IN → DATA1 ZLP → ACK for no-data)."""
        await self.transaction_out(ctx, PID.SETUP, address, 0, PID.DATA0, setup)
        received = b""
        if read_length:
            expect_pid = PID.DATA1
            while len(received) < read_length:
                pid, body = await self.transaction_in(ctx, address, 0)
                if pid != expect_pid:
                    raise HostError(f"data toggle error: got {pid:#x}, expected {expect_pid:#x}")
                received += body
                expect_pid = PID.DATA0 if expect_pid == PID.DATA1 else PID.DATA1
                if len(body) < 64:
                    break
            await self.transaction_out(ctx, PID.OUT, address, 0, PID.DATA1, b"")
        else:
            pid, body = await self.transaction_in(ctx, address, 0)
            if pid != PID.DATA1 or body != b"":
                raise HostError(f"bad status stage: {pid:#x} {body.hex()}")
        return received
