# Thin GRC entry-point wrapper for the VDE RX Adapter.
# VDERxSink in vdeter_link/adapters/gnuradio_rx.py provides the implementation.
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

from vdeter_link.adapters import gnuradio_rx


class blk(gnuradio_rx.VDERxSink):
    """Recover VDE Packets after a preamble tag and print parsed Messages."""

    def __init__(self, tag_name='preamble_detected'):
        super().__init__(tag_name=tag_name)
