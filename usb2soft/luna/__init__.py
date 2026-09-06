"""LUNA integration: a ``USBDevice`` that runs high speed on the soft PHY."""
from .device import SoftPHYUSBDevice, standard_descriptors

__all__ = ["SoftPHYUSBDevice", "standard_descriptors"]
