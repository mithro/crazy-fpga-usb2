"""Link test: PHY TX -> (internal expander | HDMI pins) -> PHY RX with a packet generator/checker
and UART counters. This is the first bitstream that runs the P2/P3 datapath in silicon.

Packet format (UTMI bytes): seq_lo, seq_hi, then N payload bytes from a 16-bit LFSR seeded with
the sequence number, then a 16-bit Fletcher checksum (over everything before it). Lengths cycle
through 8, 64 and 512 bytes of payload.
"""
from amaranth import Elaboratable, Module, Signal, Cat, Const, Mux, Array
from amaranth.lib import io
from amaranth.lib.cdc import PulseSynchronizer, FFSynchronizer

from . import Applet, register
from ..clock.netv2 import NeTV2PhyClocks
from ..debug.console import Console
from ..debug.report import Hex
from ..io.sampler import Sampler, IdelayCtrl
from ..io.serializer import Serializer
from ..rx import RxPath
from ..sim.expander import SampleExpander
from ..tx import TxPath

CONSOLE_BAUD = 115200
LENGTHS = (8, 64, 512)


def _lfsr_step(state):
    # x^16 + x^14 + x^13 + x^11 + 1 (Fibonacci), returns next state
    bit = state[0] ^ state[2] ^ state[3] ^ state[5]
    return Cat(state[1:], bit)


class PacketGenerator(Elaboratable):
    """Drives UTMI tx_* with the packet format above; one packet every ``gap`` idle cycles."""
    def __init__(self, *, gap=16, domain="usb"):
        self.gap = gap
        self.domain = domain
        self.tx_data = Signal(8)
        self.tx_valid = Signal()
        self.tx_ready = Signal()
        self.packets = Signal(32)

    def elaborate(self, platform):
        m = Module()
        sync = m.d[self.domain]
        seq = Signal(16)
        length_sel = Signal(range(len(LENGTHS)))
        length = Signal(10)
        remaining = Signal(11)
        lfsr = Signal(16)
        f1 = Signal(8)
        f2 = Signal(8)
        gap = Signal(range(self.gap + 1))
        m.d.comb += length.eq(Array([Const(n, 10) for n in LENGTHS])[length_sel])

        def send(byte, next_state):
            m.d.comb += [self.tx_data.eq(byte), self.tx_valid.eq(1)]
            with m.If(self.tx_ready):
                s1 = (f1 + byte)[:8]
                m.d[self.domain] += [f1.eq(s1), f2.eq((f2 + s1)[:8])]
                m.next = next_state

        with m.FSM(domain=self.domain):
            with m.State("GAP"):
                sync += gap.eq(gap + 1)
                with m.If(gap == self.gap):
                    sync += [gap.eq(0), f1.eq(0), f2.eq(0), lfsr.eq((seq ^ 0x5A5A) | 0x8000), remaining.eq(length)]
                    m.next = "SEQ_LO"
            with m.State("SEQ_LO"):
                send(seq[0:8], "SEQ_HI")
            with m.State("SEQ_HI"):
                send(seq[8:16], "PAYLOAD")
            with m.State("PAYLOAD"):
                m.d.comb += [self.tx_data.eq(lfsr[0:8]), self.tx_valid.eq(1)]
                with m.If(self.tx_ready):
                    s1 = (f1 + lfsr[0:8])[:8]
                    sync += [f1.eq(s1), f2.eq((f2 + s1)[:8]), lfsr.eq(_lfsr_step(lfsr)),
                             remaining.eq(remaining - 1)]
                    with m.If(remaining == 1):
                        m.next = "CK1"
            with m.State("CK1"):
                m.d.comb += [self.tx_data.eq(f1), self.tx_valid.eq(1)]
                with m.If(self.tx_ready):
                    m.next = "CK2"
            with m.State("CK2"):
                m.d.comb += [self.tx_data.eq(f2), self.tx_valid.eq(1)]
                with m.If(self.tx_ready):
                    sync += [seq.eq(seq + 1), self.packets.eq(self.packets + 1),
                             length_sel.eq(Mux(length_sel == len(LENGTHS) - 1, 0, length_sel + 1))]
                    m.next = "GAP"
        return m


class PacketChecker(Elaboratable):
    """Consumes UTMI rx_*; counts good packets, bad packets and sequence gaps."""
    def __init__(self, domain="usb"):
        self.domain = domain
        self.rx_data = Signal(8)
        self.rx_valid = Signal()
        self.rx_active = Signal()
        self.rx_error = Signal()
        self.good = Signal(32)
        self.bad = Signal(32)
        self.errors = Signal(32)
        self.gaps = Signal(32)

    def elaborate(self, platform):
        m = Module()
        sync = m.d[self.domain]
        prev_active = Signal()
        count = Signal(11)
        seq = Signal(16)
        expect_seq = Signal(16)
        have_expect = Signal()
        lfsr = Signal(16)
        f1 = Signal(8)
        f2 = Signal(8)
        mismatch = Signal()
        last2 = Signal(16)        # the last two bytes received (checksum candidates)
        sync += prev_active.eq(self.rx_active)
        with m.If(self.rx_error):
            sync += self.errors.eq(self.errors + 1)

        with m.If(self.rx_active & ~prev_active):
            sync += [count.eq(0), f1.eq(0), f2.eq(0), mismatch.eq(0)]
        with m.If(self.rx_valid):
            b = self.rx_data
            # Fletcher over all but the trailing two bytes: keep a two-byte delay line and fold
            # the byte that falls out of it.
            older = last2[0:8]           # the byte falling out of the two-byte delay line
            sync += last2.eq(Cat(last2[8:16], b))
            with m.If(count >= 2):
                s1 = (f1 + older)[:8]
                sync += [f1.eq(s1), f2.eq((f2 + s1)[:8])]
            with m.If(count == 0):
                sync += seq[0:8].eq(b)
            with m.Elif(count == 1):
                sync += [seq[8:16].eq(b), lfsr.eq((Cat(seq[0:8], b) ^ 0x5A5A) | 0x8000)]
            with m.Elif(count >= 4):
                # payload byte is the one that just left the delay line (older), compare it
                with m.If(older != lfsr[0:8]):
                    sync += mismatch.eq(1)
                sync += lfsr.eq(_lfsr_step(lfsr))
            sync += count.eq(count + 1)
        with m.If(prev_active & ~self.rx_active):
            # packet finished: last2 holds the checksum bytes; f1/f2 cover bytes 0..N-3
            ok = (last2[0:8] == f1) & (last2[8:16] == f2) & ~mismatch & (count >= 4)
            with m.If(ok):
                sync += self.good.eq(self.good + 1)
                with m.If(have_expect & (seq != expect_seq)):
                    sync += self.gaps.eq(self.gaps + 1)
                sync += [expect_seq.eq(seq + 1), have_expect.eq(1)]
            with m.Else():
                sync += self.bad.eq(self.bad + 1)
        return m


class LinkTestCore(Elaboratable):
    """TxPath + RxPath + generator/checker + counters. ``internal=True`` wires TX to RX through a
    SampleExpander; otherwise ``samples`` (in) and ``line``/``oe`` (out) are exposed for pins."""
    def __init__(self, *, internal, phase_shift=0, gap=16, samples_per_ui=4, usb_domain="usb",
                 cdr_domain="rx_cdr", tx_domain="tx_cdr", report_period=60_000_000, console_divisor=521):
        self.internal = internal
        self.usb_domain, self.cdr_domain, self.tx_domain = usb_domain, cdr_domain, tx_domain
        self.report_period = report_period
        self.tx = TxPath(tx_domain=tx_domain, usb_domain=usb_domain)
        self.rx = RxPath(samples_per_ui=samples_per_ui, samples_per_word=4 * samples_per_ui,
                         cdr_domain=cdr_domain, usb_domain=usb_domain)
        self.gen = PacketGenerator(gap=gap, domain=usb_domain)
        self.chk = PacketChecker(domain=usb_domain)
        self.expander = SampleExpander(samples_per_ui=samples_per_ui, phase_shift=phase_shift,
                                       domain=cdr_domain) if internal else None
        self.samples = self.rx.samples
        self.line = self.tx.line
        self.oe = self.tx.oe
        self.inject = Signal(4 * samples_per_ui)   # XOR mask on the internal samples (fault injection)
        self.slips_up = Signal(32)
        self.slips_down = Signal(32)
        self.uart_tx = Signal(init=1)
        self.console = Console([b"L ", Hex(self.gen.packets), b" ", Hex(self.chk.good), b" ",
                                Hex(self.chk.bad), b" ", Hex(self.chk.errors), b" ", Hex(self.chk.gaps),
                                b" ", Hex(self.slips_up), b" ", Hex(self.slips_down), b" ",
                                Hex(self.rx.cdr.phase), b"\r\n"], divisor=console_divisor)

    def elaborate(self, platform):
        m = Module()
        m.submodules.tx = self.tx
        m.submodules.rx = self.rx
        m.submodules.gen = self.gen
        m.submodules.chk = self.chk
        m.submodules.console = self.console
        usb = m.d[self.usb_domain]
        m.d.comb += [
            self.tx.tx_data.eq(self.gen.tx_data), self.tx.tx_valid.eq(self.gen.tx_valid),
            self.gen.tx_ready.eq(self.tx.tx_ready),
            self.chk.rx_data.eq(self.rx.rx_data), self.chk.rx_valid.eq(self.rx.rx_valid),
            self.chk.rx_active.eq(self.rx.rx_active), self.chk.rx_error.eq(self.rx.rx_error),
            self.uart_tx.eq(self.console.tx),
        ]
        if self.internal:
            m.submodules.expander = self.expander
            m.d.comb += [self.expander.line.eq(self.tx.line), self.expander.oe.eq(self.tx.oe),
                         self.rx.samples.eq(self.expander.samples ^ self.inject)]
        # Slip strobes from the CDR domain into usb-domain counters.
        for name, src, cnt in (("up", self.rx.cdr.slip_up, self.slips_up), ("dn", self.rx.cdr.slip_down, self.slips_down)):
            ps = PulseSynchronizer(self.cdr_domain, self.usb_domain)
            m.submodules[f"ps_{name}"] = ps
            m.d.comb += ps.i.eq(src)
            with m.If(ps.o):
                usb += cnt.eq(cnt + 1)
        timer = Signal(range(self.report_period))
        with m.If(timer == self.report_period - 1):
            usb += timer.eq(0)
            m.d.comb += self.console.trigger.eq(1)
        with m.Else():
            usb += timer.eq(timer + 1)
        return m


@register
class LinkTest(Applet):
    name = "link-test"
    description = "PHY TX->RX soak test: --mode internal (expander loopback) or hdmi (TX0.d0 -> RX<n>.d0)"

    @classmethod
    def build_tag(cls, args):
        mode = getattr(args, "mode", "internal")
        return f"-{mode}" + (f"-rx{getattr(args, 'hdmi_rx', 0)}" if mode == "hdmi" else "")

    @classmethod
    def add_arguments(cls, parser):
        parser.add_argument("--mode", choices=["internal", "hdmi"], default="internal")
        parser.add_argument("--phase-shift", type=int, default=1, help="internal mode: sample rotation 0..3")
        parser.add_argument("--hdmi-rx", type=int, choices=[0, 1], default=0)

    def elaborate(self, platform):
        m = Module()
        args = self.args
        mode = getattr(args, "mode", "internal")
        m.submodules.clocks = clocks = NeTV2PhyClocks()
        m.domains += clocks.domains
        core = LinkTestCore(internal=(mode == "internal"), phase_shift=getattr(args, "phase_shift", 1))
        m.submodules.core = core
        if mode == "hdmi":
            m.submodules.idc = IdelayCtrl()
            rx = platform.request("hdmi_in", getattr(args, "hdmi_rx", 0), dir="-")
            tx = platform.request("hdmi_out", 0, dir="-")
            m.submodules.sampler = sampler = Sampler(rx.d0)
            m.submodules.ser = ser = Serializer(tx.d0)
            m.d.comb += [core.samples.eq(sampler.samples), ser.line.eq(core.line), ser.oe.eq(core.oe)]
        uart = platform.request("uart", 0, dir="-")
        m.submodules.uart_tx = tx_buf = io.Buffer("o", uart.tx)
        m.d.comb += tx_buf.o.eq(core.uart_tx)
        leds = [platform.request("led", i, dir="-") for i in range(3)]
        bufs = [io.Buffer("o", l) for l in leds]
        for i, b in enumerate(bufs):
            m.submodules[f"led{i}"] = b
        m.d.comb += [bufs[0].o.eq(clocks.locked), bufs[1].o.eq(core.rx.rx_active),
                     bufs[2].o.eq(core.chk.bad.any() | core.chk.errors.any())]
        return m
