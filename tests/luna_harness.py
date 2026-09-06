"""Shared harness for the LUNA-on-soft-PHY tests: device PHY + synthesiser + SoftPHYUSBDevice,
scaled reset-sequencer timings (test-only monkeypatch of class attributes; LUNA is unmodified),
and a sample-level wire driver."""
from amaranth import Module, Elaboratable, ClockDomain
from amaranth.sim import Simulator

from usb2soft.phy import SoftUTMIPHY, LineStateSynthesiser
from usb2soft.luna import SoftPHYUSBDevice, standard_descriptors
from usb2soft.sim import usbhs

# Scaled USBResetSequencer constants (usb cycles). All must be <= the 3 ms constant, which sizes
# the sequencer's timers. Real values: 5 us = 300, 2 ms = 120 000, 2.5 ms = 150 000,
# 2.5 us = 150, 200 us = 12 000, 3 ms = 180 000.
SCALED = {
    "_CYCLES_5_MICROSECONDS": 30,
    "_CYCLES_2_MILLISECONDS": 120,
    "_CYCLES_2P5_MILLISECONDS": 400,
    "_CYCLES_2P5_MICROSECONDS": 12,
    "_CYCLES_200_MICROSECONDS": 600,
    "_CYCLES_3_MILLISECONDS": 3000,
}
SYNTH_SCALED = dict(se0_cycles=60, gap_cycles=6, hold_cycles=20)


def scale_sequencer(monkeypatch, values=SCALED):
    from luna.gateware.usb.usb2.reset import USBResetSequencer
    for k, v in values.items():
        monkeypatch.setattr(USBResetSequencer, k, v)


class LunaDeviceHarness(Elaboratable):
    def __init__(self, *, synth=SYNTH_SCALED, descriptors=None):
        self.phy = SoftUTMIPHY(usb_domain="usb", cdr_domain="rx_cdr", tx_domain="tx_cdr",
                               synthesiser=LineStateSynthesiser(**synth))
        self.device = SoftPHYUSBDevice(bus=self.phy)
        self.descriptors = descriptors or standard_descriptors()
        self.device.add_standard_control_endpoint(self.descriptors)

    def elaborate(self, platform):
        m = Module()
        for d in ("usb", "rx_cdr", "tx_cdr"):
            m.domains += ClockDomain(d)
        m.submodules.phy = self.phy
        m.submodules.device = self.device
        m.d.comb += self.device.connect.eq(1)
        return m


def make_sim(dut):
    sim = Simulator(dut)
    sim.add_clock(1 / 60e6, domain="usb")
    sim.add_clock(1 / 120e6, domain="rx_cdr")
    sim.add_clock(1 / 120e6, domain="tx_cdr")
    return sim


def packet_words(packet_bytes, *, ppm=0.0, sync_bits=32, idle_words=4):
    """16-sample words for one packet on the device's RX wire, idle J before and after."""
    bits = usbhs.packet_line_bits(bytes(packet_bytes), sync_bits=sync_bits)
    samples = usbhs.LineSampler(samples_per_ui=4, ppm=ppm).sample(bits)
    return [0xFFFF] * idle_words + list(usbhs.words(samples, 16)) + [0xFFFF] * idle_words


async def play_words(ctx, phy, words):
    for w in words:
        ctx.set(phy.samples, w)
        await ctx.tick("rx_cdr")
    ctx.set(phy.samples, 0xFFFF)
