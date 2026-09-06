"""Smoke tests that the pinned toolchain dependencies import."""


def test_amaranth_version():
    import amaranth
    major, minor, *_ = amaranth.__version__.split(".")
    assert (int(major), int(minor)) == (0, 5)


def test_luna_imports():
    from luna.gateware.interface.gateware_phy import GatewarePHY  # noqa: F401
    from luna.gateware.usb.usb2.device import USBDevice  # noqa: F401
    from luna.gateware.interface.utmi import UTMIInterface  # noqa: F401
