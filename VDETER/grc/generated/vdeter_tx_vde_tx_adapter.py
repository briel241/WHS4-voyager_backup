# Thin GRC entry-point wrapper for the VDE TX Adapter.
# VDETxSource in vdeter/radio/gnuradio_tx.py provides the implementation.
# Message, Packet, and Burst logic is not duplicated here.
import os
import sys

try:
    _base = os.path.dirname(os.path.abspath(__file__))
except NameError:
    # __file__ is unavailable when GRC evaluates the code without a generated file.
    _base = os.path.abspath(os.getcwd())

# Locate the src-layout vdeter package independently of the launch directory.
_probe = _base
while True:
    _src = os.path.join(_probe, 'src')
    if os.path.isfile(os.path.join(_src, 'vdeter', '__init__.py')):
        if _src not in sys.path:
            sys.path.insert(0, _src)
        break
    _parent = os.path.dirname(_probe)
    if _parent == _probe:
        break
    _probe = _parent

from vdeter.radio import gnuradio_tx


class blk(gnuradio_tx.VDETxSource):
    """Transmit fixed or interactive requests as Temporary Burst bytes."""

    def __init__(self):
        super().__init__()
