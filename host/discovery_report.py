#!/usr/bin/env python3
"""Parse `hdmi-discovery` console output into a cabling table."""
import re
import sys
from dataclasses import dataclass

LANE_NAMES = ["clk", "d0", "d1", "d2"]
_LINE = re.compile(r"^D ([0-9A-F]{6}) R([0-9A-F])([0-9A-F]) ([0-9A-F]{6}) ([0-9A-F]{2}) ([0-9A-F]{2})$")


@dataclass(frozen=True)
class Lane:
    peer: str
    port: int
    lane: int
    seen: bool
    inverted: bool


class Topology:
    def __init__(self):
        self.own_id = None
        self.lanes = {}


def parse(text):
    t = Topology()
    for raw in text.splitlines():
        line = raw.strip()
        if line.endswith("HELLO") and line.startswith("D "):
            t.own_id = line.split()[1]
            continue
        m = _LINE.match(line)
        if not m:
            continue
        own, p, l, peer, pl, flags = m.groups()
        t.own_id = t.own_id or own
        f = int(flags, 16)
        t.lanes[(p, l)] = Lane(peer=peer, port=int(pl, 16) >> 4, lane=int(pl, 16) & 0xF,
                               seen=bool(f & 1), inverted=bool(f & 2))
    return t


def render(t):
    rows = [f"board {t.own_id}"]
    for (p, l), lane in sorted(t.lanes.items()):
        name = f"rx{p}.{LANE_NAMES[int(l, 16)]}"
        if lane.seen:
            pol = "inverted" if lane.inverted else "true"
            peer_lane = LANE_NAMES[lane.lane] if lane.lane < len(LANE_NAMES) else f"?{lane.lane:X}"
            rows.append(f"{name:8s} <- {lane.peer} tx{lane.port}.{peer_lane} {pol}")
        else:
            rows.append(f"{name:8s} -")
    return "\n".join(rows)


if __name__ == "__main__":
    print(render(parse(sys.stdin.read())))
