"""Link package boundary.

VDE Message와 Packet의 wire format을 소유한다. Logical Channel, slot, timeout과 재전송
시각은 결정하지 않는다.
"""

from .data_plane import LinkDataPlane, VdeterLink
from .transfer import LinkTransferManager

__all__ = ("LinkDataPlane", "LinkTransferManager", "VdeterLink")
