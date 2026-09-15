"""software backend 회귀 — 실제 VDETER 소프트웨어 무선 위의 MASS/ROC 시나리오.

실행: VDETER\\.venv\\Scripts\\python.exe -m pytest tests/test_mass_roc_software_backend.py -q

`--transport software`가 쓰는 경로 그대로: stage5 소프트웨어 그룹의 실제
`VdeService` + TEST adapter inline SUBMIT/STATUS + launcher record 형식 +
실제 RocAgent/MassAgent. 전송이 실제 무선 스택을 왕복하므로 이 파일은
수십 초 걸린다 — 시나리오 하나(SC-VDE-005)로 경로를 잠근다.

전제: in-repo VDETER + cryptography (이 저장소의 회귀 환경에는 둘 다 있다).
"""

from __future__ import annotations

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_REPO, "VDETER", "src"),
           os.path.join(_REPO, "VDETER"),
           _REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from common.textout import ensure_utf8_output

ensure_utf8_output()


def test_sc_vde_005_over_software_radio_service():
    try:
        from apps.mass_roc_software import (
            SoftwareHarnessUnavailable,
            build_software_scenario_controller,
        )
    except ImportError as exc:  # pragma: no cover - 회귀 환경에는 존재
        raise AssertionError(f"software 하네스 import 실패: {exc}")

    try:
        harness, controller = build_software_scenario_controller()
    except SoftwareHarnessUnavailable as exc:
        # 회귀 환경(README·memory 명시)에는 VDETER+cryptography가 있다 —
        # 조립 실패를 skip이 아니라 FAIL로 다룬다.
        raise AssertionError(f"소프트웨어 무선 조립 실패: {exc}")
    try:
        result = controller.run_sc_vde_005()
    finally:
        harness.close()

    detail = result.to_dict()
    assert detail["status"] == "PASS", detail
    assert len(detail["transfers"]) == 2
    for transfer in detail["transfers"]:
        assert transfer["final_transfer_state"] == "COMPLETED"
        assert transfer["received"]["byte_exact"]
        assert transfer["transfer_id"]
    assert detail["counters"]["wrong_destination"] == 0


if __name__ == "__main__":
    test_sc_vde_005_over_software_radio_service()
    print("PASS: software backend SC-VDE-005")
