"""Device test: a LUNA USB device on one soft PHY enumerated by HostLite on a second soft PHY,
both inside the FPGA, joined by the internal loopback wires (P5 Task 7).

Modes: ``internal`` (synchronous SampleExpander wires), ``async-fast`` / ``async-slow`` (the host
PHY — both its CDR and its transmitter — runs from the offset PLL, so the device receives at
+3788 / −4630 ppm and the host receives the device's transmissions offset the other way, through
two AsyncResamplers). ``hdmi``: device PHY on HDMI RX0/TX0 lane 0 for the two-board set-up (no
host-lite in the bitstream; the other board runs ``--role host``).

Report line once per second:
``D <loops> <ok> <bad> <timeouts> <naks> <restarts> <sofs> <chirps> <hs> <slip_up> <slip_dn>``.
LEDs: clocks locked, device in high speed, error sticky.
"""
from amaranth import Elaboratable, Module, Signal, Cat
from amaranth.lib import io

from . import Applet, register
from ..clock.netv2 import NeTV2PhyClocks
from ..debug.console import Console
from ..debug.report import Hex
from ..hostlite import HostLite, enumeration_script
from ..io.sampler import Sampler, IdelayCtrl
from ..io.serializer import Serializer
from ..luna import SoftPHYUSBDevice, standard_descriptors
from ..phy import SoftUTMIPHY, LineStateSynthesiser
from ..sim.expander import SampleExpander, AsyncResampler

CONSOLE_BAUD = 115200
SYNTH_REAL = dict(se0_cycles=600, gap_cycles=60, hold_cycles=300, min_chirp_cycles=60_000)
HOST_REAL = dict(sof_period=7500, start_delay=180_000, restart_delay=6000, reply_timeout=256)


class DeviceTestCore(Elaboratable):
    def __init__(self, *, role="both", async_tx=False, synth=SYNTH_REAL, host=HOST_REAL,
                 report_period=60_000_000, console_divisor=521):
        """``role``: "both" (device + host-lite with internal wires), "device" (device PHY on pins),
        "host" (host-lite PHY on pins)."""
        self.role, self.async_tx, self.report_period = role, async_tx, report_period
        host_cdr, host_tx = ("tx_async", "tx_async") if async_tx else ("rx_cdr", "tx_cdr")
        self.descriptors = standard_descriptors()
        if role in ("both", "device"):
            self.dev_phy = SoftUTMIPHY(synthesiser=LineStateSynthesiser(**synth))
            self.device = SoftPHYUSBDevice(bus=self.dev_phy)
            self.device.add_standard_control_endpoint(self.descriptors)
        if role in ("both", "host"):
            self.host_phy = SoftUTMIPHY(cdr_domain=host_cdr, tx_domain=host_tx)
            self.host = HostLite(enumeration_script(self.descriptors.get_descriptor_bytes(1)), **host)
        if role == "both":
            if async_tx:
                self.h2d = AsyncResampler(in_domain="tx_async", out_domain="rx_cdr")
                self.d2h = AsyncResampler(in_domain="tx_cdr", out_domain="tx_async")
            else:
                self.h2d = SampleExpander(phase_shift=1, domain="rx_cdr")
                self.d2h = SampleExpander(phase_shift=2, domain="rx_cdr")
        # pins (roles device/host)
        self.samples = Signal(16)
        self.line = Signal(4)
        self.oe = Signal(4)
        # statistics (usb domain)
        self.hs = Signal()
        self.slips_up = Signal(32)
        self.slips_down = Signal(32)
        self.error = Signal()
        self.uart_tx = Signal(init=1)
        z = Signal(32)
        h = self.host if role != "device" else None
        d = self.dev_phy if role != "host" else None
        fields = [
            h.loops if h else z, h.ok if h else z, h.bad if h else z, h.timeouts if h else z,
            h.naks if h else z, h.restarts if h else z, h.sofs if h else z,
            d.synthesiser.chirps if d else z, self.hs, self.slips_up, self.slips_down,
        ]
        segs = [b"D"]
        for f in fields:
            segs += [b" ", Hex(f)]
        segs.append(b"\r\n")
        self.console = Console(segs, divisor=console_divisor)

    def elaborate(self, platform):
        m = Module()
        m.submodules.console = self.console
        usb = m.d.usb
        if self.role in ("both", "device"):
            m.submodules.dev_phy = self.dev_phy
            m.submodules.device = self.device
            m.d.comb += [self.device.connect.eq(1),
                         self.hs.eq((self.device.speed == 0) & (self.dev_phy.op_mode == 0))]
            cdr = self.dev_phy.rx.cdr
            for src, cnt in ((cdr.slip_up, self.slips_up), (cdr.slip_down, self.slips_down)):
                raw = Signal(32)
                with m.If(src):
                    m.d.rx_cdr += raw.eq(raw + 1)
                usb += cnt.eq(raw)
        if self.role in ("both", "host"):
            m.submodules.host_phy = self.host_phy
            m.submodules.host = self.host
            h, p = self.host, self.host_phy
            m.d.comb += [p.tx_data.eq(h.tx_data), p.tx_valid.eq(h.tx_valid), h.tx_ready.eq(p.tx_ready),
                         h.rx_data.eq(p.rx_data), h.rx_valid.eq(p.rx_valid), h.rx_active.eq(p.rx_active),
                         h.rx_error.eq(p.rx_error)]
            with m.If(h.bad.any() | h.timeouts.any()):
                usb += self.error.eq(1)
        if self.role == "both":
            m.submodules.h2d = self.h2d
            m.submodules.d2h = self.d2h
            m.d.comb += [
                self.h2d.line.eq(self.host_phy.line), self.h2d.oe.eq(self.host_phy.oe),
                self.dev_phy.samples.eq(self.h2d.samples),
                self.d2h.line.eq(self.dev_phy.line), self.d2h.oe.eq(self.dev_phy.oe),
                self.host_phy.samples.eq(self.d2h.samples),
            ]
        elif self.role == "device":
            m.d.comb += [self.dev_phy.samples.eq(self.samples), self.line.eq(self.dev_phy.line),
                         self.oe.eq(self.dev_phy.oe)]
        else:
            m.d.comb += [self.host_phy.samples.eq(self.samples), self.line.eq(self.host_phy.line),
                         self.oe.eq(self.host_phy.oe)]
        timer = Signal(range(self.report_period))
        with m.If(timer == self.report_period - 1):
            usb += timer.eq(0)
            m.d.comb += self.console.trigger.eq(1)
        with m.Else():
            usb += timer.eq(timer + 1)
        m.d.comb += self.uart_tx.eq(self.console.tx)
        return m


@register
class DeviceTest(Applet):
    name = "device-test"
    description = "LUNA device enumerated by HostLite through two soft PHYs: --mode internal|async-fast|async-slow|hdmi"

    @classmethod
    def build_tag(cls, args):
        mode = getattr(args, "mode", "internal")
        tag = f"-{mode}"
        if mode == "hdmi":
            tag += f"-{getattr(args, 'role', 'device')}-rx{getattr(args, 'hdmi_rx', 0)}"
        return tag

    @classmethod
    def add_arguments(cls, parser):
        parser.add_argument("--mode", choices=["internal", "async-fast", "async-slow", "hdmi"], default="internal")
        parser.add_argument("--role", choices=["device", "host"], default="device", help="hdmi mode: which end this board is")
        parser.add_argument("--hdmi-rx", type=int, choices=[0, 1], default=0)

    def vivado_constraints(self):
        mode = getattr(self.args, "mode", "internal")
        if not mode.startswith("async-"):
            return ()
        return ("set_clock_groups -asynchronous -group [get_clocks raw_tx_async] "
                "-group [get_clocks -filter {NAME != raw_tx_async}]",)

    def elaborate(self, platform):
        m = Module()
        args = self.args
        mode = getattr(args, "mode", "internal")
        async_tx = mode.split("-")[1] if mode.startswith("async-") else None
        m.submodules.clocks = clocks = NeTV2PhyClocks(async_tx=async_tx)
        m.domains += clocks.domains
        role = getattr(args, "role", "device") if mode == "hdmi" else "both"
        m.submodules.core = core = DeviceTestCore(role=role, async_tx=bool(async_tx))
        if mode == "hdmi":
            m.submodules.idc = idc = IdelayCtrl()
            rx = platform.request("hdmi_in", getattr(args, "hdmi_rx", 0), dir="-")
            tx = platform.request("hdmi_out", 0, dir="-")
            m.submodules.sampler = sampler = Sampler(rx.d0)
            m.submodules.ser = ser = Serializer(tx.d0)
            m.d.comb += [sampler.rdy.eq(idc.rdy), core.samples.eq(sampler.samples),
                         ser.line.eq(core.line), ser.oe.eq(core.oe)]
        uart = platform.request("uart", 0, dir="-")
        m.submodules.uart_tx = tx_buf = io.Buffer("o", uart.tx)
        m.d.comb += tx_buf.o.eq(core.uart_tx)
        leds = [platform.request("led", i, dir="-") for i in range(3)]
        bufs = [io.Buffer("o", l) for l in leds]
        for i, b in enumerate(bufs):
            m.submodules[f"led{i}"] = b
        m.d.comb += [bufs[0].o.eq(clocks.locked), bufs[1].o.eq(core.hs), bufs[2].o.eq(core.error)]
        return m
