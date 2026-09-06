"""usb2soft command line: list applets, build bitstreams."""
import argparse
import sys

from .applets import APPLETS, load_builtin_applets


def main(argv=None):
    load_builtin_applets()
    parser = argparse.ArgumentParser(prog="usb2soft")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list applets")

    b = sub.add_parser("build", help="build an applet bitstream")
    b.add_argument("applet", choices=sorted(APPLETS))
    b.add_argument("--platform", default="netv2")
    b.add_argument("--variant", default="a7-35")
    b.add_argument("--toolchain", default="vivado", choices=["vivado", "xray"])
    b.add_argument("--build-root", default="build")
    b.add_argument("--no-build", action="store_true", help="generate sources only")
    for applet in APPLETS.values():
        applet.add_arguments(b)

    args = parser.parse_args(argv)
    if args.command == "list":
        for name, cls in sorted(APPLETS.items()):
            print(f"{name:24s} {cls.description}")
        return 0
    if args.command == "build":
        from .build import build_applet
        result = build_applet(args.applet, platform=args.platform, variant=args.variant,
                              toolchain=args.toolchain, build_root=args.build_root,
                              do_build=not args.no_build, args=args)
        print(f"build dir: {result.build_dir}")
        if result.bitstream:
            print(f"bitstream: {result.bitstream}")
        for r in result.reports:
            print(f"report:    {r}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
