"""Applets: complete top-level designs selectable from the CLI (after LUNA's applets/ and
Greg Davill's ButterStick-projects layout)."""
import importlib

from amaranth import Elaboratable

__all__ = ["Applet", "APPLETS", "register", "load_builtin_applets"]

APPLETS = {}

# Built-in applet modules; each registers itself on import.
_BUILTIN = ("hello", "hdmi_discovery")


class Applet(Elaboratable):
    name = None
    description = ""

    @classmethod
    def add_arguments(cls, parser):
        """Applet-specific CLI options (override if needed)."""

    def __init__(self, args=None):
        self.args = args


def register(cls):
    if not cls.name:
        raise ValueError("applet needs a name")
    APPLETS[cls.name] = cls
    return cls


def load_builtin_applets():
    """Import the built-in applet modules so they register themselves."""
    for modname in _BUILTIN:
        try:
            importlib.import_module(f"{__name__}.{modname}")
        except ModuleNotFoundError as exc:
            # Only tolerate the applet module itself being absent (not yet written), never a
            # missing dependency inside it.
            if exc.name != f"{__name__}.{modname}":
                raise
