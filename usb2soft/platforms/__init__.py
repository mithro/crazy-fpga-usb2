"""Board platforms."""
from .netv2 import NeTV2Platform

_PLATFORMS = {
    "netv2": NeTV2Platform,
}


def get_platform(name, **kwargs):
    """Instantiate the platform registered under ``name``; raises KeyError if unknown."""
    return _PLATFORMS[name](**kwargs)


__all__ = ["get_platform", "NeTV2Platform"]
