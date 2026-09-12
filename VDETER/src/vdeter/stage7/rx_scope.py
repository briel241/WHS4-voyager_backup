"""Launcher-owned workload RX scope; existing MAC planner and device owner only.

Commands are bounded/non-waiting. Controller polls during start/close; ongoing
normal RX does not depend on IPC polling. No TX schedule is exchanged.
"""
from .configuration import LOWER_CHANNEL_NAME, UPPER_CHANNEL_NAME, LOWER_RF_TUNE_HZ, UPPER_RF_TUNE_HZ
from .collision_runtime import RxCollisionGate, CollisionRuntimeError
from ..common.results import Ok


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
        if self.active:
            return {"active": True}
        if self.dual:
            if self.pending():
                return {"active": False, "draining": True}
            worker = self.seam._worker()
            if self.touched:
                raise CollisionRuntimeError("partial tuning must be restored before retry")
            previous = worker.read_tuning()
            if (not isinstance(previous, dict) or not previous.get("analog_bandwidth_hz")
                    or any(self._lane(previous.get("lane_center_hz", {}), i) is None for i in (0, 1))):
                raise CollisionRuntimeError("restorable RX tuning snapshot required")
            self.previous, self.touched = previous, True
            mid = (LOWER_RF_TUNE_HZ + UPPER_RF_TUNE_HZ) // 2
            offsets = {0: mid - UPPER_RF_TUNE_HZ, 1: mid - LOWER_RF_TUNE_HZ}
            rb = worker.apply_manual_tuning(mid, offsets, 4_700_000)
            if (abs(rb.get("rx_lo_hz", 0) - mid) > 1 or rb.get("analog_bandwidth_hz", 0) < 4_700_000
                    or any(self._lane(rb.get("lane_dsp_hz", {}), i) is None
                           or abs(self._lane(rb["lane_dsp_hz"], i) - offsets[i]) > 1 for i in (0, 1))
                    or any(self._lane(rb.get("lane_center_hz", {}), i) is None
                           or abs(self._lane(rb["lane_center_hz"], i) - (mid-offsets[i])) > 1 for i in (0, 1))):
                raise CollisionRuntimeError("actual dual RX tuning readback mismatch")
            self.readback = rb
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
            restored = self.seam._worker().restore_tuning(self.previous)
            if (abs(restored.get("analog_bandwidth_hz", 0) - self.previous["analog_bandwidth_hz"]) > 1
                    or any(self._lane(restored.get("lane_center_hz", {}), i) is None
                           or abs(self._lane(restored["lane_center_hz"], i)
                                  - self._lane(self.previous["lane_center_hz"], i)) > 1 for i in (0, 1))):
                raise CollisionRuntimeError("RX restore unverified; ownership retained")
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
