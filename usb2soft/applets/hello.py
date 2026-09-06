"""Hello applet: heartbeat LEDs and an identity banner on the UART once per second."""
import subprocess
from amaranth import Elaboratable, Module, Signal
from amaranth.lib import io

from . import Applet, register
from ..clock.netv2 import NeTV2BringupClocks
from ..debug.console import Console
from ..debug.report import Hex
from ..debug.dna import DNAPort, DNA_BITS

UART_BAUD = 115200


def git_describe():
    try:
        return subprocess.check_output(["git", "describe", "--tags", "--dirty", "--always"],
                                       text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "unknown"


class HelloCore(Elaboratable):
    """Everything except pins and clocking, so it can be simulated."""
    def __init__(self, *, divisor, period_cycles, variant, version, simulate_dna=None):
        self.divisor = divisor
        self.period_cycles = period_cycles
        self.uart_tx = Signal(init=1)
        self.leds = Signal(6)
        self.locked = Signal(init=1)
        self.simulate_dna = simulate_dna
        self.dna = Signal(DNA_BITS)
        self.dna_valid = Signal()
        banner = f"usb2soft hello {variant} {version} DNA=".encode()
        self.console = Console([banner, Hex(self.dna), b"\r\n"], divisor=divisor)

    def elaborate(self, platform):
        m = Module()
        m.submodules.console = self.console
        if self.simulate_dna is not None:
            m.d.comb += [self.dna.eq(self.simulate_dna), self.dna_valid.eq(1)]
        else:
            m.submodules.dna = dna = DNAPort()
            m.d.comb += [self.dna.eq(dna.dna), self.dna_valid.eq(dna.valid)]

        timer = Signal(range(self.period_cycles))
        seconds = Signal(4)
        tick = Signal()
        m.d.comb += tick.eq(timer == self.period_cycles - 1)
        with m.If(tick):
            m.d.sync += [timer.eq(0), seconds.eq(seconds + 1)]
        with m.Else():
            m.d.sync += timer.eq(timer + 1)

        m.d.comb += [
            self.console.trigger.eq(tick & self.dna_valid),
            self.uart_tx.eq(self.console.tx),
            self.leds[0].eq(seconds[0]),      # 1 Hz heartbeat (same bit as leds[2], kept as the obvious blinker)
            self.leds[1].eq(self.locked),
            self.leds[2:6].eq(seconds),
        ]
        return m


@register
class Hello(Applet):
    name = "hello"
    description = "heartbeat LEDs + identity banner on the UART (115200 8N1)"

    def elaborate(self, platform):
        m = Module()
        m.submodules.clocks = clocks = NeTV2BringupClocks()
        m.domains += clocks.domains
        core = HelloCore(divisor=round(60e6 / UART_BAUD), period_cycles=60_000_000,
                         variant=platform.variant, version=git_describe())
        m.submodules.core = core
        m.d.comb += core.locked.eq(clocks.locked)

        uart = platform.request("uart", 0, dir="-")
        m.submodules.uart_tx = tx = io.Buffer("o", uart.tx)
        m.d.comb += tx.o.eq(core.uart_tx)
        for i in range(6):
            led = platform.request("led", i, dir="-")
            m.submodules[f"led{i}"] = buf = io.Buffer("o", led)
            m.d.comb += buf.o.eq(core.leds[i])
        return m
