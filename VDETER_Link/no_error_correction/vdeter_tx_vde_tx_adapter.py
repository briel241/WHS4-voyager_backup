# Thin GRC entry-point wrapper for the VDE TX Adapter.
# VDETxSource in vdeter_link/adapters/gnuradio_tx.py provides the implementation.
# Message, Packet, and Burst logic is not duplicated here.
import os
import sys

try:
    _base = os.path.dirname(os.path.abspath(__file__))
except NameError:
    # __file__ is unavailable when GRC evaluates the code without a generated file.
    _base = os.path.abspath(os.getcwd())

# Locate the vdeter_link package independently of the launch directory.
_probe = _base
while True:
    if os.path.isfile(os.path.join(_probe, 'vdeter_link', '__init__.py')):
        if _probe not in sys.path:
            sys.path.insert(0, _probe)
        break
    _parent = os.path.dirname(_probe)
    if _parent == _probe:
        break
    _probe = _parent

from vdeter_link.adapters import gnuradio_tx


class blk(gnuradio_tx.VDETxSource):
    """Transmit fixed or interactive requests as Temporary Burst bytes."""

    def __init__(self):
        super().__init__()
