"""Local decoded-window overlap policy. Not an RF interference detector.

Compare actual recovered burst starts within a completed logical RX window.
Its required lane terminals have already joined. Undecoded interference or
bursts outside that capture are unobservable. No TX journal, IQ synthesis,
cross-device clock comparison, new thread, or RX hold timer.
"""
from collections import deque
from threading import RLock
import base64
import hashlib

ENDPOINTS = ("MASS-A", "MASS-B", "ROC")
MAX_PENDING_RX = 128
MAX_OBSERVATIONS = 32
MODEL = "LOCAL_DECODED_WINDOW_OVERLAP_DROP_V1"
SOURCE_BY_RECEIVER_LANE = {
    "MASS-A": {0: "ROC", 1: "MASS-B"},
    "MASS-B": {0: "ROC", 1: "MASS-A"},
    "ROC": {0: "MASS-A", 1: "MASS-B"}}


class CollisionRuntimeError(RuntimeError):
    pass


def overlapping_indices(rows):
    dropped = set()
    for i, a in enumerate(rows):
        for j in range(i):
            b = rows[j]
            if (a["lane"] != b["lane"] and a["physical_channel"] == b["physical_channel"]
                    and a["device_hw_timestamp_ns"] < b["end_hw_ns"]
                    and b["device_hw_timestamp_ns"] < a["end_hw_ns"]):
                dropped.update((i, j))
    return dropped


class RxCollisionGate:
    def __init__(self, session_id, receiver, *, active_from_ns=0, station_ids=None):
        if not isinstance(session_id, str) or not 0 < len(session_id) <= 128 or receiver not in ENDPOINTS:
            raise ValueError("bounded workload owner and known receiver required")
        self.session_id, self.receiver = session_id, receiver
        self.active_from_ns = active_from_ns
        self.station_ids = dict(station_ids or {})
        self.accepting = True
        self.lock = RLock()
        self.pending = {}
        self.observations = deque()
        self.released_packets = {}
        self.application_receipts = deque()
        self.fault = None
        self.counts = {"released": 0, "dropped": 0, "indeterminate": 0}
        self.decisions = deque(maxlen=128)

    def fail(self, reason):
        with self.lock:
            self.fault = self.fault or str(reason)[:128]

    def hold(self, key, item, packets):
        with self.lock:
            if not self.accepting:
                return None
            if key in self.pending or len(self.pending) >= MAX_PENDING_RX:
                self.fail("RX_LOCAL_QUEUE_CAPACITY")
                return False
            self.pending[key] = (item, packets)
            return True

    def begin_close(self):
        with self.lock:
            self.accepting = False

    def drain(self):
        ready = []
        with self.lock:
            for key, (item, packets) in list(self.pending.items()):
                rows = []
                for packet, meta in packets:
                    if (not isinstance(meta, dict) or type(meta.get("device_hw_timestamp_ns")) is not int
                            or meta["device_hw_timestamp_ns"] < 0 or meta.get("lane") not in (0, 1)
                            or type(meta.get("duration_ns")) is not int or meta["duration_ns"] <= 0):
                        rows = None
                        self.fail("ACTUAL_RX_TIME_OR_PROFILE_DURATION_MISSING")
                        break
                    rows.append(dict(meta, end_hw_ns=meta["device_hw_timestamp_ns"] + meta["duration_ns"]))
                if rows is None:
                    self.counts["indeterminate"] += len(packets)
                    ready.append((item, (), self.fault))
                    del self.pending[key]
                    continue
                dropped = overlapping_indices(rows)
                kept, seen = [], set()
                for i, ((packet, _), row) in enumerate(zip(packets, rows)):
                    digest = hashlib.sha256(packet.packet).hexdigest()
                    self.decisions.append({"model": MODEL, "decision": "DROP" if i in dropped else "RELEASE",
                        "packet_digest": digest, "lane": row["lane"], "start_hw_ns": row["device_hw_timestamp_ns"],
                        "end_hw_ns": row["end_hw_ns"], "duration_basis": "PROFILE_DEVICE_BURST",
                        "comparison_scope": "COMPLETED_LOGICAL_RX_WINDOW", "rf_collision_measured": False})
                    if i in dropped:
                        self.counts["dropped"] += 1
                        continue
                    if digest in seen:
                        continue
                    seen.add(digest)
                    kept.append(packet)
                    source = SOURCE_BY_RECEIVER_LANE[self.receiver][row["lane"]]
                    packet_id = getattr(getattr(packet, "packet_id", None), "value", None)
                    obs = dict(row, packet_digest=digest,
                        observation_key=f"{row['lane']}:{row['device_hw_timestamp_ns']}:{digest}",
                        source_station=self.station_ids.get(source, source), source_endpoint=source,
                        destination_station="UNKNOWN", source_station_basis="FIXED_PHYSICAL_INPUT_PATH",
                        address_basis="NOT_INFERRED_FROM_TRANSMITTER",
                        packet_base64=base64.b64encode(packet.packet).decode("ascii"))
                    if packet_id is not None:
                        self.released_packets[packet_id] = {k: v for k, v in obs.items() if k != "packet_base64"}
                        while len(self.released_packets) > MAX_PENDING_RX:
                            del self.released_packets[next(iter(self.released_packets))]
                    # Consumer lag inhibits PoC on polling, never blocks normal RX/ACK.
                    if len(self.observations) >= MAX_OBSERVATIONS:
                        self.fail("POC_OBSERVATION_CONSUMER_TOO_SLOW")
                    else:
                        self.observations.append(obs)
                    self.counts["released"] += 1
                ready.append((item, tuple(kept), None))
                del self.pending[key]
        return ready

    def application_delivered(self, packet_id):
        with self.lock:
            record = self.released_packets.pop(packet_id, None)
            if record is None:
                return
            if len(self.application_receipts) >= MAX_OBSERVATIONS:
                self.fail("APPLICATION_RECEIPT_CONSUMER_TOO_SLOW")
                return
            self.application_receipts.append(dict(record, receiver=self.receiver,
                basis="MAC_PRESENTATION_SERVICE_ACCEPTED"))

    def poll(self):
        with self.lock:
            result = {"session_id": self.session_id, "receiver": self.receiver, "model": MODEL,
                "fault": self.fault, "pending": len(self.pending), "counts": dict(self.counts),
                "observations": list(self.observations), "application_receipts": list(self.application_receipts),
                "decisions": list(self.decisions)}
            self.observations.clear()
            self.application_receipts.clear()
            self.decisions.clear()
            return result
