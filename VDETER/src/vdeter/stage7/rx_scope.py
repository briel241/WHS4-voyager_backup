"""Launcher-owned workload RX scope; existing MAC planner and device owner only.

Commands are bounded/non-waiting. Controller polls during start/close; ongoing
normal RX does not depend on IPC polling. No TX schedule is exchanged.
"""
from .configuration import LOWER_CHANNEL_NAME, UPPER_CHANNEL_NAME, LOWER_RF_TUNE_HZ, UPPER_RF_TUNE_HZ
from .collision_runtime import RxCollisionGate, CollisionRuntimeError
from ..common.results import Ok
from copy import deepcopy
from math import isfinite


def _validate_tuning(value, *, manual=False):
    """Reject missing/non-finite device values before declaring ready or restored."""
    def number(v, *, positive=False):
        return type(v) in (int, float) and isfinite(v) and (not positive or v > 0)
    def lanes(name, *, positive=False):
        rows = value.get(name)
        return isinstance(rows, dict) and all(
            number(rows.get(i, rows.get(str(i))), positive=positive) for i in (0, 1))
    if (not isinstance(value, dict) or not number(value.get('analog_bandwidth_hz'), positive=True)
            or not lanes('lane_center_hz', positive=True)):
        raise CollisionRuntimeError('invalid/non-finite restorable RX readback')
    if manual or 'rx_lo_hz' in value or 'lane_dsp_hz' in value:
        if not number(value.get('rx_lo_hz'), positive=True) or not lanes('lane_dsp_hz'):
            raise CollisionRuntimeError('invalid/non-finite manual RX readback')
    if 'lane_rf_lo_hz' in value and not lanes('lane_rf_lo_hz', positive=True):
        raise CollisionRuntimeError('invalid lane RF LO readback')


class WorkloadRxScope:
    def __init__(self, seam, owner, dual):
        if not isinstance(owner, str) or not 0 < len(owner) <= 128 or type(dual) is not bool:
            raise ValueError("bounded scope owner and bool dual required")
        if dual and seam.endpoint_id != "MASS-B":
            raise ValueError("dual RX is exclusive to MASS-B")
        self.seam, self.owner, self.dual = seam, owner, dual
        self.previous = None
        self.touched = self.active = self.closed = False
        self.readback = None
        self.binding = self._binding()

    @property
    def radio(self):
        return self.seam._radio()

    @property
    def support(self):
        return self.seam._endpoint.stage4

    @property
    def mac(self):
        return self.support.mac

    def _binding(self):
        r = self.radio
        return r._generation, r.bridge.clock_id, r.bridge.generation

    def pending(self):
        with self.radio._condition:
            admitted = any(not c.terminal for c in self.radio._rx_children.values())
        return admitted or bool(self.support.lane_dispatcher._windows) or bool(self.support._rx_commands)

    def _replan(self):
        mac, coordinator = self.mac, self.mac.coordinator
        state = mac.state.evolve(planned_rx_windows=(), deferred_rx_windows=())
        commands, errors = [], []
        event_id = mac._synthetic_event_id()
        state = coordinator.plan_frame_windows(state, event_id, commands, errors)
        result = mac._apply(coordinator._finish(state, commands, event_id, errors[0] if errors else Ok(state)))
        if result.is_err:
            raise CollisionRuntimeError("RX replanning rejected")

    @staticmethod
    def _lane(mapping, lane):
        return mapping.get(lane, mapping.get(str(lane)))

    def activate(self):
        if self.closed:
            raise CollisionRuntimeError("closed RX scope cannot reactivate")
        if self.binding != self._binding():
            raise CollisionRuntimeError("RX clock generation changed before activation")
        if self.active:
            return {"active": True}
        if self.dual:
            if self.pending():
                return {"active": False, "draining": True}
            worker = self.seam._worker()
            if self.touched:
                raise CollisionRuntimeError("partial tuning must be restored before retry")
            previous = worker.read_tuning()
            _validate_tuning(previous)
            self.previous, self.touched = deepcopy(previous), True
            mid = (LOWER_RF_TUNE_HZ + UPPER_RF_TUNE_HZ) // 2
            offsets = {0: mid - UPPER_RF_TUNE_HZ, 1: mid - LOWER_RF_TUNE_HZ}
            rb = worker.apply_manual_tuning(mid, offsets, 4_700_000)
            _validate_tuning(rb, manual=True)
            if (abs(rb.get("rx_lo_hz", 0) - mid) > 1 or rb.get("analog_bandwidth_hz", 0) < 4_700_000
                    or any(self._lane(rb.get("lane_dsp_hz", {}), i) is None
                           or abs(self._lane(rb["lane_dsp_hz"], i) - offsets[i]) > 1 for i in (0, 1))
                    or any(self._lane(rb.get("lane_center_hz", {}), i) is None
                           or abs(self._lane(rb["lane_center_hz"], i) - (mid-offsets[i])) > 1 for i in (0, 1))):
                raise CollisionRuntimeError("actual dual RX tuning readback mismatch")
            if "lane_rf_lo_hz" in rb and any(abs(self._lane(rb["lane_rf_lo_hz"], i) - mid) > 1 for i in (0, 1)):
                raise CollisionRuntimeError("shared RX LO differs between lanes")
            self.readback = deepcopy(rb)
            self.radio.set_rx_lane_channels({0: UPPER_CHANNEL_NAME, 1: LOWER_CHANNEL_NAME})
            self.mac.coordinator._observer_channel_names = (LOWER_CHANNEL_NAME,)
            self.mac.coordinator._rx_planner_paused = False
            self._replan()
        self.support.collision_gate = RxCollisionGate(self.owner, self.seam.endpoint_id,
            station_ids=self.seam._station_id_by_endpoint)
        for stack in self.support.lane_dispatcher.stacks:
            stack.facade.diagnostic_sink.capture_recent_windows(True)
        self.active = True
        return {"active": True}

    def poll(self):
        gate = self.support.collision_gate
        if not self.active or gate is None:
            raise CollisionRuntimeError("RX scope not active")
        if self.binding != self._binding():
            gate.fail("RX_CLOCK_GENERATION_CHANGED")
        reply = gate.poll()
        with self.radio._condition:
            channels = {c.request.channel.name for c in self.radio._rx_children.values() if not c.terminal}
        reply["dual_ready"] = bool(self.dual and not self.closed and not gate.fault
            and {LOWER_CHANNEL_NAME, UPPER_CHANNEL_NAME} <= channels)
        reply["dual_readback"] = self.readback
        return reply

    def close(self):
        self.closed = True
        gate = self.support.collision_gate
        if gate is not None:
            gate.begin_close()
        if self.dual:
            self.mac.coordinator._rx_planner_paused = True
        return {"closing": True}

    def disable(self):
        if not self.closed:
            raise CollisionRuntimeError("close scope before disabling")
        gate = self.support.collision_gate
        if (gate is not None and gate.pending) or (self.dual and self.pending()):
            return {"disabled": False, "draining": True}
        if self.touched:
            restored = self.seam._worker().restore_tuning(deepcopy(self.previous))
            _validate_tuning(restored, manual="rx_lo_hz" in self.previous)
            if (abs(restored.get("analog_bandwidth_hz", 0) - self.previous["analog_bandwidth_hz"]) > 1
                    or any(self._lane(restored.get("lane_center_hz", {}), i) is None
                           or abs(self._lane(restored["lane_center_hz"], i)
                                  - self._lane(self.previous["lane_center_hz"], i)) > 1 for i in (0, 1))):
                raise CollisionRuntimeError("RX restore unverified; ownership retained")
            if "rx_lo_hz" in self.previous and (
                    abs(restored["rx_lo_hz"] - self.previous["rx_lo_hz"]) > 1
                    or any(abs(self._lane(restored["lane_dsp_hz"], i)
                               - self._lane(self.previous["lane_dsp_hz"], i)) > 1 for i in (0, 1))):
                raise CollisionRuntimeError("manual RX restore differs; ownership retained")
            self.touched = False
        if self.dual:
            self.radio.set_rx_lane_channels(None)
            self.mac.coordinator._observer_channel_names = ()
            self.mac.coordinator._rx_planner_paused = False
            try:
                self._replan()
            except BaseException:
                # Keep the restoration retry drainable after a partial publish.
                self.mac.coordinator._rx_planner_paused = True
                raise
        self.support.collision_gate = None
        for stack in self.support.lane_dispatcher.stacks:
            stack.facade.diagnostic_sink.capture_recent_windows(False)
        return {"disabled": True}
