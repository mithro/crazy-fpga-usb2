"""Build an applet for a platform with a given toolchain and collect reports."""
import os
from dataclasses import dataclass
from pathlib import Path

from ..applets import APPLETS, load_builtin_applets
from ..platforms import get_platform

__all__ = ["build_applet", "vivado_env", "xray_env", "BuildResult", "TOOLCHAINS"]

VIVADO_SETTINGS = Path("/opt/Xilinx/2025.2/Vivado/settings64.sh")
OPENXC7_ROOT = Path("/home/tim/github/mithro/fpgas-online-test-designs/.venv/toolchains/openxc7")

TOOLCHAINS = {"vivado": "Vivado", "xray": "Xray"}


@dataclass
class BuildResult:
    applet: str
    build_dir: Path
    bitstream: Path | None
    reports: list


def vivado_env():
    env = dict(os.environ)
    env.setdefault("AMARANTH_ENV_VIVADO", str(VIVADO_SETTINGS))
    return env


def xray_env():
    env = dict(os.environ)
    env["PATH"] = f"{OPENXC7_ROOT / 'bin'}:{env['PATH']}"
    env.setdefault("DB_DIR", str(OPENXC7_ROOT / "squashfs-root/opt/nextpnr-xilinx/external/prjxray-db"))
    env.setdefault("CHIPDB_DIR", str(OPENXC7_ROOT / "chipdb"))
    return env


def build_applet(name, *, platform, variant, toolchain, build_root="build", do_build=True, args=None):
    load_builtin_applets()
    applet_cls = APPLETS[name]
    plat = get_platform(platform, variant=variant, toolchain=TOOLCHAINS[toolchain])
    tag = applet_cls.build_tag(args) if args is not None else ""
    build_dir = Path(build_root) / f"{name}{tag}-{platform}-{variant}" / toolchain
    build_dir.mkdir(parents=True, exist_ok=True)
    env = vivado_env() if toolchain == "vivado" else xray_env()
    old = dict(os.environ)
    os.environ.update(env)
    try:
        applet = applet_cls(args)
        extra = {}
        if toolchain == "vivado":
            xdc = "\n".join(applet.vivado_constraints())
            if xdc:
                extra["add_constraints"] = xdc
        products = plat.build(applet, name="top", build_dir=str(build_dir), do_build=do_build, **extra)
    finally:
        os.environ.clear()
        os.environ.update(old)
    if not do_build:
        products.extract(str(build_dir))   # BuildPlan.extract(root) writes the generated files there
        return BuildResult(name, build_dir, None, [])
    bit = build_dir / "top.bit"
    reports = sorted(str(p.name) for p in build_dir.glob("top_*.rpt"))
    return BuildResult(name, build_dir, bit if bit.exists() else None, reports)
