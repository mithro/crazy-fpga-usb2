"""HDMI link discovery: every HDMI TX lane beacons this board's id and lane number as 1 Mbaud
UART frames; every HDMI RX lane listens (true and inverted polarity) and the console reports
what each lane hears. Used to measure how the NeTV2s are cross-cabled (docs/hardware-setups.md §1)."""
from amaranth import Elaboratable, Module, Signal, Array, Cat, Const, Mux
from amaranth.lib import io
from luna.gateware.interface.uart import UARTTransmitter

from . import Applet, register
from ..clock.netv2 import NeTV2BringupClocks
from ..debug.console import Console
from ..debug.dna import DNAPort
from ..debug.report import Hex
from ..io.uart import UARTReceiver

SYNC = 0xA5
LANE_BAUD = 1_000_000
CONSOLE_BAUD = 115200
LANE_NAMES = ("clk", "d0", "d1", "d2")


def frame_bytes(own_id, *, port, lane):
    ids = [(own_id >> 16) & 0xFF, (own_id >> 8) & 0xFF, own_id & 0xFF]
    pl = (port << 4) | lane
    ck = ids[0] ^ ids[1] ^ ids[2] ^ pl
    return bytes([SYNC, *ids, pl, ck])


class LaneBeacon(Elaboratable):
    def __init__(self, *, divisor, own_id=None, port=0, lane=0, id_signal=None):
        self.divisor = divisor
        self.port, self.lane = port, lane
        self.own_id = own_id
        self.id_signal = id_signal      # Signal(24) alternative to a constant id
        self.tx = Signal(init=1)

    def elaborate(self, platform):
        m = Module()
        m.submodules.uart = uart = UARTTransmitter(divisor=self.divisor)
        ident = Signal(24)
        if self.id_signal is not None:
            m.d.comb += ident.eq(self.id_signal)
        else:
            m.d.comb += ident.eq(self.own_id)
        pl = Const((self.port << 4) | self.lane, 8)
        ck = ident[16:24] ^ ident[8:16] ^ ident[0:8] ^ pl
        table = Array([Const(SYNC, 8), ident[16:24], ident[8:16], ident[0:8], pl, ck])
        index = Signal(range(6))
        m.d.comb += [uart.stream.payload.eq(table[index]), uart.stream.valid.eq(1), self.tx.eq(uart.tx)]
        with m.If(uart.stream.ready):
            m.d.sync += index.eq(Mux(index == 5, 0, index + 1))
        return m


class _FrameMatcher(Elaboratable):
    """Sliding 6-byte window; ``hit`` strobes when a valid frame ends."""
    def __init__(self):
        self.data = Signal(8)
        self.valid = Signal()
        self.hit = Signal()
        self.peer_id = Signal(24)
        self.peer_pl = Signal(8)

    def elaborate(self, platform):
        m = Module()
        win = [Signal(8, name=f"w{i}") for i in range(6)]
        with m.If(self.valid):
            m.d.sync += [win[i].eq(win[i + 1]) for i in range(5)]
            m.d.sync += win[5].eq(self.data)
        ok = (win[0] == SYNC) & ((win[1] ^ win[2] ^ win[3] ^ win[4]) == win[5])
        m.d.sync += self.hit.eq(0)
        # ``valid`` last cycle shifted a new byte in; evaluate the window now.
        prev_valid = Signal()
        m.d.sync += prev_valid.eq(self.valid)
        with m.If(prev_valid & ok):
            m.d.sync += [self.hit.eq(1), self.peer_id.eq(Cat(win[3], win[2], win[1])), self.peer_pl.eq(win[4])]
        return m


class LaneListener(Elaboratable):
    def __init__(self, *, divisor, timeout_cycles):
        self.divisor = divisor
        self.timeout_cycles = timeout_cycles
        self.rx = Signal(init=1)
        self.seen = Signal()
        self.inverted = Signal()
        self.peer_id = Signal(24)
        self.peer_pl = Signal(8)

    def elaborate(self, platform):
        m = Module()
        m.submodules.rx_true = rx_t = UARTReceiver(divisor=self.divisor)
        m.submodules.rx_inv = rx_i = UARTReceiver(divisor=self.divisor)
        m.submodules.fm_true = fm_t = _FrameMatcher()
        m.submodules.fm_inv = fm_i = _FrameMatcher()
        m.d.comb += [
            rx_t.rx.eq(self.rx), rx_i.rx.eq(~self.rx),
            fm_t.data.eq(rx_t.data), fm_t.valid.eq(rx_t.valid),
            fm_i.data.eq(rx_i.data), fm_i.valid.eq(rx_i.valid),
        ]
        timer = Signal(range(self.timeout_cycles + 1))
        with m.If(fm_t.hit | fm_i.hit):
            m.d.sync += [
                timer.eq(self.timeout_cycles), self.seen.eq(1),
                self.inverted.eq(fm_i.hit & ~fm_t.hit),
                self.peer_id.eq(Mux(fm_t.hit, fm_t.peer_id, fm_i.peer_id)),
                self.peer_pl.eq(Mux(fm_t.hit, fm_t.peer_pl, fm_i.peer_pl)),
            ]
        with m.Elif(timer != 0):
            m.d.sync += timer.eq(timer - 1)
        with m.Else():
            m.d.sync += self.seen.eq(0)
        return m


class DiscoveryCore(Elaboratable):
    def __init__(self, *, own_id=None, id_signal=None, divisor, timeout_cycles, report_period,
                 console_divisor=None, n_ports=2):
        self.n_ports = n_ports
        self.divisor = divisor
        self.console_divisor = console_divisor or divisor
        self.timeout_cycles = timeout_cycles
        self.report_period = report_period
        self.ident = Signal(24)
        self._own_id, self._id_signal = own_id, id_signal
        self.tx = [[Signal(name=f"tx{p}_{l}", init=1) for l in range(4)] for p in range(n_ports)]
        self.rx = [[Signal(name=f"rx{p}_{l}", init=1) for l in range(4)] for p in range(n_ports)]
        self.uart_tx = Signal(init=1)
        self.listeners = [[LaneListener(divisor=divisor, timeout_cycles=timeout_cycles)
                           for _ in range(4)] for _ in range(n_ports)]

    def elaborate(self, platform):
        m = Module()
        m.d.comb += self.ident.eq(self._id_signal if self._id_signal is not None else self._own_id)
        for p in range(self.n_ports):
            for l in range(4):
                b = LaneBeacon(divisor=self.divisor, id_signal=self.ident, port=p, lane=l)
                m.submodules[f"beacon{p}_{l}"] = b
                m.d.comb += self.tx[p][l].eq(b.tx)
                m.submodules[f"listen{p}_{l}"] = lst = self.listeners[p][l]
                m.d.comb += lst.rx.eq(self.rx[p][l])

        # Report: one line per lane per period plus a header. Fields are muxed by ``sel``.
        n_lanes = 4 * self.n_ports
        sel = Signal(range(n_lanes + 1))
        peer_id = Signal(24)
        peer_pl = Signal(8)
        flags = Signal(8)
        lane_code = Signal(8)
        flat = [self.listeners[p][l] for p in range(self.n_ports) for l in range(4)]
        m.d.comb += [
            peer_id.eq(Array([x.peer_id for x in flat])[sel]),
            peer_pl.eq(Array([x.peer_pl for x in flat])[sel]),
            flags.eq(Array([Cat(x.seen, x.inverted) for x in flat])[sel]),
            lane_code.eq(Array([Const((p << 4) | l, 8) for p in range(self.n_ports) for l in range(4)])[sel]),
        ]
        m.submodules.header = header = Console([b"D ", Hex(self.ident), b" HELLO\r\n"], divisor=self.console_divisor)
        m.submodules.line = line = Console([b"D ", Hex(self.ident), b" R", Hex(lane_code), b" ",
                                            Hex(peer_id), b" ", Hex(peer_pl), b" ", Hex(flags), b"\r\n"],
                                           divisor=self.console_divisor)
        # Both consoles idle high and never transmit at the same time, so AND their tx lines.
        m.d.comb += self.uart_tx.eq(header.tx & line.tx)

        # Report sequencer. A Console's busy is combinational from its reporter's SEND state: low
        # in the cycle the trigger is applied, high from the next cycle until the last byte is
        # accepted. The *_WAIT states are entered one cycle after the trigger, so ~busy is safe
        # there. Because the two consoles have separate UART transmitters sharing one line, a
        # hand-off must also wait for the transmitter to go idle (ten bit-times after the last
        # byte was accepted), otherwise the next console starts driving mid-byte.
        period = Signal(range(self.report_period))
        with m.FSM():
            with m.State("WAIT"):
                with m.If(period == self.report_period - 1):
                    m.d.sync += [period.eq(0), sel.eq(0)]
                    m.next = "HEADER"
                with m.Else():
                    m.d.sync += period.eq(period + 1)
            with m.State("HEADER"):
                m.d.comb += header.trigger.eq(1)
                m.next = "HEADER_WAIT"
            with m.State("HEADER_WAIT"):
                with m.If(~header.busy & header.idle):
                    m.next = "LINE"
            with m.State("LINE"):
                m.d.comb += line.trigger.eq(1)
                m.next = "LINE_WAIT"
            with m.State("LINE_WAIT"):
                with m.If(~line.busy & line.idle):
                    with m.If(sel == n_lanes - 1):
                        m.next = "WAIT"
                    with m.Else():
                        m.d.sync += sel.eq(sel + 1)
                        m.next = "LINE"
        return m


@register
class HDMIDiscovery(Applet):
    name = "hdmi-discovery"
    description = "beacon on all HDMI TX lanes, report what each HDMI RX lane hears"

    def elaborate(self, platform):
        m = Module()
        m.submodules.clocks = clocks = NeTV2BringupClocks()
        m.domains += clocks.domains
        m.submodules.dna = dna = DNAPort()
        core = DiscoveryCore(id_signal=dna.dna[0:24], divisor=round(60e6 / LANE_BAUD),
                             console_divisor=round(60e6 / CONSOLE_BAUD),
                             timeout_cycles=6_000_000, report_period=30_000_000)
        m.submodules.core = core
        for p in range(2):
            out = platform.request("hdmi_out", p, dir="-")
            inp = platform.request("hdmi_in", p, dir="-")
            for l, name in enumerate(LANE_NAMES):
                m.submodules[f"obuf{p}{l}"] = ob = io.Buffer("o", getattr(out, name))
                m.d.comb += ob.o.eq(core.tx[p][l])
                m.submodules[f"ibuf{p}{l}"] = ib = io.Buffer("i", getattr(inp, name))
                m.d.comb += core.rx[p][l].eq(ib.i)
        uart = platform.request("uart", 0, dir="-")
        m.submodules.uart_tx = tx = io.Buffer("o", uart.tx)
        m.d.comb += tx.o.eq(core.uart_tx)
        return m
