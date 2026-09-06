#!/usr/bin/env python3
"""Runs on the Raspberry Pi: print everything received on a serial port for N seconds."""
import argparse
import sys
import time

import serial

p = argparse.ArgumentParser()
p.add_argument("--port", required=True)
p.add_argument("--baud", type=int, default=115200)
p.add_argument("--seconds", type=float, default=5.0)
a = p.parse_args()
s = serial.Serial(a.port, a.baud, timeout=0.2)
s.reset_input_buffer()
end = time.monotonic() + a.seconds
while time.monotonic() < end:
    data = s.read(4096)
    if data:
        sys.stdout.write(data.decode("ascii", "replace"))
        sys.stdout.flush()
