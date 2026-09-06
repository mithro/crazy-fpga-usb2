# crazy-fpga-usb2

USB 2.0 high-speed (480 Mbit/s) signalling recovered and transmitted directly
on Xilinx 7-series SelectIO pins, with no external USB PHY, presented to the
[LUNA](https://github.com/gregdavill/luna) USB stack as a virtual UTMI PHY.
Written in native [Amaranth](https://amaranth-lang.org/).

See `docs/superpowers/specs/` for the design and `docs/` for hardware set-ups,
clocking notes, test strategy and measured results.

Licensed under the Apache License, Version 2.0.
