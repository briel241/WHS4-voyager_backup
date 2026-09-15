"""Exact wire-prefix and original-address authenticated data."""

from ...common.addressing import RouteAddresses
from ...common.errors import ErrorCode
from ..constants import AAD_BYTES, AAD_DOMAIN, HEADER_BYTES
from ..errors import SecurityFault


def build_aad(*, prefix: bytes, route: RouteAddresses, security_header: bytes) -> bytes:
    if type(prefix) is not bytes or len(prefix) != 3:
        raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_POLICY_VIOLATION)
    if type(security_header) is not bytes or len(security_header) != HEADER_BYTES:
        raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_PROFILE_UNSUPPORTED)
    if not isinstance(route, RouteAddresses) or route.original_destination.address.is_broadcast:
        raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_POLICY_VIOLATION)
    source = route.original_source.station_id.value
    destination = route.original_destination.address.station_id.value
    if source == 0 or destination == 0 or source == destination:
        raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_POLICY_VIOLATION)
    aad = AAD_DOMAIN + prefix + source.to_bytes(4, "big") + destination.to_bytes(4, "big") + security_header
    assert len(aad) == AAD_BYTES
    return aad
