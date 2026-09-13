"""Endpoint-local directional security sessions."""

from threading import RLock

from ...common.errors import ErrorCode
from ...common.identifiers import StationId
from ..errors import SecurityFault
from .material import DirectionalSessionMaterial
from .replay import ReplayWindow
from .state_store import SQLiteSecurityStateStore


class TxSecuritySession:
    def __init__(self, material: DirectionalSessionMaterial, store: SQLiteSecurityStateStore):
        self.material = material
        self.store = store
        self.lock = RLock()
        self.next_counter = store.tx_reserved_until(material.material_id) + 1
        self.reserved_until = self.next_counter - 1

    def reserve_invocation(self, body_length: int) -> int:
        with self.lock:
            if self.next_counter > self.reserved_until:
                self.next_counter, self.reserved_until = self.store.reserve_tx_block(self.material.material_id)
            counter = self.next_counter
            self.next_counter += 1
            return counter


class RxSecuritySession:
    def __init__(self, material: DirectionalSessionMaterial, store: SQLiteSecurityStateStore):
        self.material = material
        self.store = store
        self.lock = RLock()
        self.replay_window = ReplayWindow(*store.rx_snapshot(material.material_id))


class SecuritySessionManager:
    def __init__(self, local_station_id: StationId,
                 materials: tuple[DirectionalSessionMaterial, ...],
                 state_store: SQLiteSecurityStateStore):
        self._store = state_store
        self._closed = False
        self._tx = {}
        self._rx = {}
        for material in materials:
            if material.source_id == local_station_id:
                self._tx[material.direction] = TxSecuritySession(material, state_store)
            elif material.destination_id == local_station_id:
                self._rx[material.direction] = RxSecuritySession(material, state_store)
            else:
                raise ValueError("Material excludes local station")

    def tx_session(self, *, source_id: StationId, destination_id: StationId) -> TxSecuritySession:
        if self._closed:
            raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_SESSION_UNAVAILABLE)
        try:
            return self._tx[source_id.value, destination_id.value, 1]
        except KeyError:
            raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_SESSION_UNAVAILABLE) from None

    def rx_session(self, *, source_id: StationId, destination_id: StationId, key_epoch: int) -> RxSecuritySession:
        if self._closed or key_epoch != 1:
            raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_EPOCH_MISMATCH if key_epoch != 1 else ErrorCode.PRESENTATION_SECURITY_SESSION_UNAVAILABLE)
        try:
            return self._rx[source_id.value, destination_id.value, key_epoch]
        except KeyError:
            raise SecurityFault(ErrorCode.PRESENTATION_SECURITY_SESSION_UNAVAILABLE) from None

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._store.close()
