"""상주 세션(Phase G)에서만 드러나는 복구 경로 계약.

단발 실행(rr-p1/p2/p3)은 시나리오 하나를 돌고 프로세스가 죽었으므로, 한 번
망가진 상태가 이후에 영향을 줄 수 없었다. Phase G는 하드웨어를 한 번 세우고
workload 4개를 갈아끼우며 최대 2시간 유지하므로, "복구 불가 상태"가 세션
전체를 조용히 무효화한다. 여기서 고정하는 것은 그 복구 경로 둘이다.

  B2  TBB_ACTIVE emit 실패 후 재시도 경로가 열려 있는가
  B3  재조립 오류 뒤 MAC이 죽은 RX session을 버리는가
"""

import ast
import io
import os

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
LAUNCHER = os.path.join(REPO, "VDETER", "apps", "stage7_endpoint_launcher.py")
MAC = os.path.join(REPO, "VDETER", "src", "vdeter", "mac", "coordinator.py")
REASSEMBLY = os.path.join(
    REPO, "VDETER", "src", "vdeter", "link", "data_session", "reassembly.py")


def _read(path):
    return io.open(path, encoding="utf-8").read()


# ------------------------------------------------------------------ B2

def test_tbb_active_emit_has_a_retry_path():
    """emit 실패 시 재시도가 가능해야 한다.

    `emit_tbb_active`는 state를 TBB_ACTIVE로 **먼저** 바꾼 뒤 이벤트를 보낸다.
    전송이 실패하면 tbb_emitted는 False인데 state는 이미 바뀌어 있어,
    감시 조건이 SERVICE_START_STAGED만 보면 재시도가 영영 일어나지 않는다.
    """
    source = _read(LAUNCHER)
    start = source.index("watching_tbb = ")
    clause = source[start:source.index("message = server.recv_message", start)]
    assert "SERVICE_START_STAGED" in clause
    assert "TBB_ACTIVE" in clause, "emit 실패 후 재시도 경로가 없다"
    assert "not tbb_emitted" in clause, "성공 뒤에도 계속 감시하면 안 된다"


def test_emit_tbb_active_sets_state_before_sending():
    """위 시험의 전제(state 선변경)가 실제로 성립하는지 고정한다."""
    client = _read(os.path.join(REPO, "VDETER", "src", "vdeter", "stage7",
                                "runtime_client.py"))
    tree = ast.parse(client)
    node = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "emit_tbb_active")
    body = ast.unparse(node)
    assert body.index("self.state = 'TBB_ACTIVE'") < body.index("_send_event")


def test_tbb_active_is_pumped():
    """TBB_ACTIVE에서 펌프가 멈추면 ROC 방송이 끊겨 MASS가 획득하지 못한다."""
    source = _read(LAUNCHER)
    marker = "_PUMP_ACTIVE_STATES = ("
    gate = source[source.index(marker):]
    gate = gate[:gate.index(")") + 1]
    for state in ("SERVICE_ADMISSION_READY", "SERVICE_START_STAGED",
                  "TBB_ACTIVE", "RUNNING"):
        assert state in gate, f"{state}가 펌프 게이트에서 빠졌다"


# ------------------------------------------------------------------ B3

def test_reassembly_discard_codes_cover_every_context_pop():
    """MAC의 code 집합이 Link의 폐기 경로를 하나도 빠뜨리지 않아야 한다.

    Link가 context를 버렸는데 MAC이 그 code를 모르면, MAC은 죽은 handle을
    계속 재사용하고 이후 모든 fragment가 LINK_UNKNOWN_SESSION이 된다.
    """
    import re

    link_codes = set(re.findall(r"ErrorCode\.([A-Z_]+)", _read(REASSEMBLY)))
    mac = _read(MAC)
    block = mac[mac.index("_REASSEMBLY_DISCARD_CODES = frozenset({"):]
    block = block[:block.index("})")]
    mac_codes = set(re.findall(r"ErrorCode\.([A-Z_]+)", block))

    missing = sorted(link_codes - mac_codes)
    assert not missing, f"Link가 쓰는 폐기 code를 MAC이 모른다: {missing}"
    # 반대로 Link에 없는 code를 넣으면 무관한 실패에 session을 버리게 된다.
    extra = sorted(mac_codes - link_codes)
    assert not extra, f"reassembly와 무관한 code가 섞였다: {extra}"


def test_dependency_failure_drops_the_poisoned_rx_session():
    """재조립 오류 뒤 MAC이 RX session을 버려 다음 DataStart가 재OPEN한다."""
    tree = ast.parse(_read(MAC))
    node = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_on_dependency_failed")
    body = ast.unparse(node)
    assert "_REASSEMBLY_DISCARD_CODES" in body
    assert "without_rx_session" in body
    # transfer_id가 있는 경로는 기존대로 transfer 종료로 처리한다 (불변).
    assert body.index("_terminate_transfer") < body.index("without_rx_session")


def test_rx_sessions_are_keyed_by_channel_so_a_dead_handle_persists():
    """B3가 왜 필요한지의 전제 — RX session은 논리채널로 조회된다."""
    mac = _read(MAC)
    assert "rx_session_for_channel(slot.logical_channel)" in mac
    models = _read(os.path.join(REPO, "VDETER", "src", "vdeter", "mac", "models.py"))
    block = models[models.index("def without_rx_session"):]
    block = block[:block.index("def ", 10)]
    # rx_sessions만 건드린다 — TX나 transfer 상태를 함께 지우면 안 된다.
    assert "rx_sessions=" in block
    assert "transfers" not in block and "tx_sessions" not in block


# ------------------------------------------------------------------ 큐 정체

def _drain_source():
    source = _read(LAUNCHER)
    start = source.index("def _drain_dispatch_queue():")
    return source[start:source.index("main._drain_dispatch_queue", start)]


def test_stale_samples_are_dropped_not_decoded():
    """창 수명을 넘긴 표본은 버려야 한다.

    처리해도 창이 이미 닫혀 결과가 버려지는데, 그 ~140ms가 뒤 항목을 또
    늦게 만들어 백로그가 영영 안 빠진다 (phase-g-service-20260831-130917:
    qlen 465 상시 · done=12/deferred=244 반복 · 데이터 전달 0).
    """
    body = _drain_source()
    assert "_SAMPLE_MAX_AGE_S" in body, "나이 상한 검사가 없다"
    assert 'item[0] == "s"' in body, "표본만 골라 버려야 한다"
    # 완료·오류는 절대 버리지 않는다 — 싸고 pending 장부를 비운다.
    assert 'item[0] in ("c", "e")' in body


def test_sample_age_threshold_is_derived_from_frame_length():
    """임계를 하드코딩하면 안 된다 — TDMA frame 길이에서 유도해야 한다."""
    source = _read(LAUNCHER)
    block = source[source.index("_SAMPLE_MAX_AGE_S ="):]
    block = block[:block.index(chr(10))]
    assert "FRAME_NANOSECONDS" in block or "_DISPATCH_FRAME_NS" in block, block


def test_completions_are_processed_before_samples():
    """완료를 표본 뒤에 줄세우면 pending RX 장부가 굶는다.

    실측: 장부 126~128 포화 · window-shed 6,600건 · MAC_TRANSFER_FAILED 504건.
    """
    body = _drain_source()
    cheap = body.index('ordered = [item for item in batch if item[0] in ("c", "e")]')
    heavy = body.index('ordered += [item for item in batch if item[0] == "s"]')
    assert cheap < heavy, "완료가 표본보다 먼저 배치되지 않았다"


def test_sample_dispatch_does_not_leak_the_timestamp():
    """적재 시각은 배수 전용이다 — 런타임 콜백 인자로 새면 안 된다."""
    body = _drain_source()
    assert "_samples_locked(*item[1:6])" in body,         "표본 호출이 적재 시각까지 넘기고 있다"


def test_stall_budget_is_preserved():
    """300ms 예산과 MAC 미니사이클은 그대로 유지돼야 한다 (기존 보호)."""
    body = _drain_source()
    assert "> 0.3" in body
    assert "mac_cycle()" in body
    assert "appendleft(leftover)" in body


if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            print(f"  {name} OK")
    print("PASS: 상주 세션 복구 경로 계약")
