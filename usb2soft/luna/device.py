"""``SoftPHYUSBDevice``: LUNA's ``USBDevice`` on a ``SoftUTMIPHY``.

LUNA decides how to treat its ``bus`` argument by duck typing: an object with ``dir`` is a ULPI
bus, one with ``d_n`` is raw FS I/O for its ``GatewarePHY``, anything else is used as a UTMI bus
directly — but that last branch was written for the gateware PHY and sets ``always_fs = True`` /
``data_clock = 12e6``. Both attributes are read only in ``elaborate`` (``USBTokenDetector``,
``USBInterpacketTimer``, the reset sequencer's ``full_speed_only``), so overriding them after
``super().__init__()`` puts the device back on the normal 60 MHz high-speed path. No LUNA source
is changed.
"""
from luna.gateware.usb.usb2.device import USBDevice
from usb_protocol.emitters import DeviceDescriptorCollection

__all__ = ["SoftPHYUSBDevice", "standard_descriptors"]


class SoftPHYUSBDevice(USBDevice):
    def __init__(self, *, bus, handle_clocking=False):
        super().__init__(bus=bus, handle_clocking=handle_clocking)
        assert self.utmi is bus, "SoftUTMIPHY must be used as a bare UTMI bus"
        self.always_fs = False
        self.data_clock = 60e6


def standard_descriptors(*, vendor=0x1209, product=0x0001, max_packet_size=64):
    """A minimal descriptor set: device (18 bytes), one configuration with one vendor-class
    interface and no endpoints. ``max_packet_size`` is ep0's (64 is the only legal HS value)."""
    d = DeviceDescriptorCollection()
    with d.DeviceDescriptor() as dev:
        dev.idVendor = vendor
        dev.idProduct = product
        dev.bMaxPacketSize0 = max_packet_size
        dev.iManufacturer = "crazy-fpga-usb2"
        dev.iProduct = "soft PHY test device"
        dev.iSerialNumber = "0001"
        dev.bNumConfigurations = 1
    with d.ConfigurationDescriptor() as c:
        with c.InterfaceDescriptor() as i:
            i.bInterfaceNumber = 0
            i.bInterfaceClass = 0xFF
    return d
