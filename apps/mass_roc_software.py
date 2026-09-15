"""MASS/ROC 시나리오의 software 무선 하네스 (Phase B).

`--transport software` backend다. Stage 7 실장비 경로와 **같은 코드**
(`MassRocScenarioController` + TEST-only adapter inline SUBMIT/STATUS +
launcher event record 형식)를 쓰되, endpoint를 별도 process·B210 대신
in-process VDETER 소프트웨어 무선 그룹(`tests.support.stage5`)으로 조립한다.

경계는 stage7과 동일하다 — 공개 `VdeService` API(submit/status)와 public
`ServiceEvent(DATA_RECEIVED)`만 쓴다. Link/MAC/PHY/Radio 내부는 만지지 않는다.
차이는 두 가지뿐이다: Service 확보 방식(in-process 조립)과 시계 진행
(`poll()`이 소프트웨어 그룹 시계를 직접 돌린다).
"""

from __future__ import annotations

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _path in (_ROOT, os.path.join(_ROOT, "VDETER", "src"),
              os.path.join(_ROOT, "VDETER")):
    if _path not in sys.path:
        sys.path.insert(0, _path)


class SoftwareHarnessUnavailable(RuntimeError):
    """이 환경에서 소프트웨어 무선 그룹을 조립할 수 없다."""


#: 우리 endpoint 이름 ↔ stage5 하네스 endpoint 이름.
_STAGE5_ENDPOINT_BY_OURS = {
    "MASS-A": "stage5-ship-a",
    "MASS-B": "stage5-ship-b",
    "ROC": "stage5-roc",
}

#: in-process 채널의 peer 자격 (adapter authorized_controller와 동일해야 함).
_LOCAL_PEER = ("in-process", 0)

_TEST_PLAN_SHA = "0" * 64


class _LocalChannel:
    """소켓 없이 adapter.handle_command를 직접 부르는 §16.2 채널."""

    def __init__(self, adapter) -> None:
        self._adapter = adapter

    def request(self, message: dict) -> dict:
        return self._adapter.handle_command(message, _LOCAL_PEER)


class _DrivenEventLog:
    """software 그룹의 ServiceEvent를 launcher record 형식으로 모으는 event 소스.

    `poll()`이 소프트웨어 그룹 시계를 몇 스텝 진행시키고, 각 endpoint sink에
    새로 도착한 public ServiceEvent를 launcher의 `service_event_record`로
    직렬화해 `records`에 쌓는다 — stage7 event JSONL tail과 같은 계약.
    """

    def __init__(self, harness, drive_steps_per_poll: int = 8) -> None:
        self._harness = harness
        self._steps = drive_steps_per_poll
        self.records = {ep: [] for ep in _STAGE5_ENDPOINT_BY_OURS}
        self._seen = {ep: 0 for ep in _STAGE5_ENDPOINT_BY_OURS}

    def poll(self) -> None:
        self._harness.drive(self._steps)
        from apps.stage7_endpoint_launcher import service_event_record

        for endpoint_id, sink_events in self._harness.events_by_endpoint.items():
            new = sink_events[self._seen[endpoint_id]:]
            for event in new:
                self.records[endpoint_id].append(service_event_record(event))
            self._seen[endpoint_id] = len(sink_events)

    def wait(self, predicate, timeout_s: float):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self.poll()
            found = predicate(self.records)
            if found:
                return found
        return None


class SoftwareMassRocHarness:
    """소프트웨어 무선 그룹 + TEST adapter + inline controller 한 벌."""

    def __init__(self) -> None:
        try:
            from tests.support.stage5 import (
                MASS_A_STATION_ID,
                MASS_B_STATION_ID,
                ROC_STATION_ID,
                build_stage5_software_group,
                drive_stage5_group,
                start_stage5_group,
            )
            from vdeter.service import HandlerServiceEventSink
            from vdeter.stage7.testing.adapter import Stage7ServiceApiTestAdapter
            from vdeter.stage7.testing.controller import Stage7ScenarioController
        except ImportError as exc:
            raise SoftwareHarnessUnavailable(f"VDETER import 실패: {exc}") from exc

        self._drive_fn = drive_stage5_group
        self.station_by_endpoint = {
            "MASS-A": MASS_A_STATION_ID.value,
            "MASS-B": MASS_B_STATION_ID.value,
            "ROC": ROC_STATION_ID.value,
        }
        ours_by_stage5 = {v: k for k, v in _STAGE5_ENDPOINT_BY_OURS.items()}
        self.events_by_endpoint = {ep: [] for ep in _STAGE5_ENDPOINT_BY_OURS}

        def sink_factory(stage5_endpoint_id):
            ours = ours_by_stage5.get(stage5_endpoint_id)
            if ours is None:  # pragma: no cover - 하네스 구성 밖 endpoint
                return HandlerServiceEventSink(lambda _event: None)
            return HandlerServiceEventSink(self.events_by_endpoint[ours].append)

        try:
            self._clock, self._group = build_stage5_software_group(
                service_sink_factory=sink_factory, e2e_security_mode="ENFORCED",
            )
            start_stage5_group(self._clock, self._group)
        except Exception as exc:  # noqa: BLE001 - cryptography 등 조립 실패
            raise SoftwareHarnessUnavailable(
                f"소프트웨어 무선 그룹 조립 실패: {exc}") from exc

        by_station = self._group.by_station()
        channels = {}
        self.adapters = {}
        for endpoint_id, station in self.station_by_endpoint.items():
            adapter = Stage7ServiceApiTestAdapter(
                endpoint_id=endpoint_id,
                service=by_station[station],
                resolved_test_plan_sha256=_TEST_PLAN_SHA,
                authorized_controller=_LOCAL_PEER,
            )
            self.adapters[endpoint_id] = adapter
            channels[endpoint_id] = _LocalChannel(adapter)

        self.controller = Stage7ScenarioController(
            run_id="software-mass-roc",
            resolved_test_plan_sha256=_TEST_PLAN_SHA,
            adapter_channels=channels,
            host_raw_clock=time.monotonic_ns,
        )
        self.events = _DrivenEventLog(self)

    def drive(self, steps: int) -> None:
        self._drive_fn(self._clock, self._group, steps)

    def close(self) -> None:
        try:
            self._group.stop()
            self._group.close_final()
        except Exception:  # noqa: BLE001 - 종료 최선 노력
            pass


def build_software_scenario_controller():
    """(harness, MassRocScenarioController) 한 벌을 만든다."""
    from apps.stage7_mass_roc_controller import MassRocScenarioController

    harness = SoftwareMassRocHarness()
    controller = MassRocScenarioController(
        controller=harness.controller,
        events=harness.events,
        # 소프트웨어 시계는 poll()이 직접 돌리므로 벽시계 여유는 짧아도 된다.
        status_timeout_s=60.0,
        receive_timeout_s=60.0,
    )
    return harness, controller
