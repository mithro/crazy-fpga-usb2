"""Kosagi/Alphamax NeTV2 platform (Artix-7 XC7A35T or XC7A100T, FGG484, speed grade -2).

Pin data: litex-boards ``kosagi_netv2.py`` and the prjxray ``package_pins.csv`` for
xc7a35tfgg484 (see docs/hardware-setups.md). Pairs marked ``Inverted()`` in litex-boards are
swapped on the PCB and are declared with ``DiffPairsN`` so gateware sees true polarity.
"""
from amaranth.build import Resource, Subsignal, Pins, PinsN, DiffPairs, DiffPairsN, Attrs, Clock
from amaranth.vendor import XilinxPlatform

__all__ = ["NeTV2Platform", "VARIANTS"]

VARIANTS = {
    "a7-35":  "xc7a35t",
    "a7-100": "xc7a100t",
}

_LVCMOS33 = Attrs(IOSTANDARD="LVCMOS33")
_TMDS_33  = Attrs(IOSTANDARD="TMDS_33")


def _hdmi(name, number, *, clk, d0, d1, d2, direction):
    """Build an HDMI resource; each lane is (p, n, inverted)."""
    def pair(p, n, inv):
        cls = DiffPairsN if inv else DiffPairs
        return cls(p, n, dir=direction)
    return Resource(name, number,
        Subsignal("clk", pair(*clk)),
        Subsignal("d0",  pair(*d0)),
        Subsignal("d1",  pair(*d1)),
        Subsignal("d2",  pair(*d2)),
        _TMDS_33)


class NeTV2Platform(XilinxPlatform):
    # ``XilinxPlatform.device`` is an abstract property in amaranth 0.5; a plain class attribute
    # overrides it, and __init__ then sets the per-variant value on the instance.
    device      = None
    package     = "fgg484"
    speed       = "2"
    default_clk = "clk50"

    resources = [
        Resource("clk50", 0, Pins("J19", dir="i"), Clock(50e6), _LVCMOS33),

        # Six user LEDs, active low.
        *[Resource("led", i, PinsN(p, dir="o"), _LVCMOS33)
          for i, p in enumerate("M21 N20 L21 AA21 R19 M16".split())],

        # UART to the Raspberry Pi GPIO header (FPGA TX -> Pi GPIO15).
        Resource("uart", 0,
            Subsignal("tx", Pins("E14", dir="o")),
            Subsignal("rx", Pins("E13", dir="i")),
            _LVCMOS33),

        # HDMI: two inputs (RX0 bank 15, RX1 bank 14) and two outputs (TX0 bank 14, TX1 bank 16).
        _hdmi("hdmi_in", 0, direction="i",
              clk=("L19", "L20", True), d0=("K21", "K22", True),
              d1=("J20", "J21", True),  d2=("J22", "H22", True)),
        _hdmi("hdmi_in", 1, direction="i",
              clk=("Y18", "Y19", True), d0=("AA18", "AB18", False),
              d1=("AA19", "AB20", True), d2=("AB21", "AB22", True)),
        _hdmi("hdmi_out", 0, direction="o",
              clk=("W19", "W20", True), d0=("W21", "W22", False),
              d1=("U20", "V20", False), d2=("T21", "U21", False)),
        _hdmi("hdmi_out", 1, direction="o",
              clk=("G21", "G22", True), d0=("E22", "D22", True),
              d1=("C22", "B22", True),  d2=("B21", "A21", True)),

        # HDMI DDC side channels.
        Resource("hdmi_in_ddc", 0, Subsignal("scl", Pins("T18")), Subsignal("sda", Pins("V18")), _LVCMOS33),
        Resource("hdmi_in_ddc", 1, Subsignal("scl", PinsN("W17")), Subsignal("sda", Pins("R17")), _LVCMOS33),

        # USB D+/D- via the PCIe SMBus pair (bunnie's netv2mvp-usb3-v1 breakout): SM_P=E19, SM_N=D19.
        # Input-only differential standard usable in a 3.3 V bank; provisional until the set-up 3
        # adaptor network (docs/hardware-setups.md §5) fixes the receiver common mode. Vivado rejects
        # IBUFDS with a single-ended IOSTANDARD, hence not LVCMOS33.
        Resource("usb_hax", 0, Subsignal("d", DiffPairs("E19", "D19", dir="i")), Attrs(IOSTANDARD="LVDS_25")),

        # PCIe "hax" single-ended pins (luna-boards numbering).
        *[Resource("hax", i, Pins(p), _LVCMOS33)
          for i, p in enumerate("B15 B16 B13 A15 A16 A13 A14 B17 A18 C17".split())],
    ]
    connectors = []

    def __init__(self, *, variant="a7-35", toolchain="Vivado"):
        if variant not in VARIANTS:
            raise ValueError(f"unknown NeTV2 variant {variant!r}; choose from {sorted(VARIANTS)}")
        self.variant = variant
        self.device = VARIANTS[variant]
        super().__init__(toolchain=toolchain)
