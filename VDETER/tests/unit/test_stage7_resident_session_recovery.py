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


def test_samples_are_never_dropped_without_their_completion():
    """표본만 버리고 완료를 남기면 PHY 불변식이 결정론적으로 깨진다.

    lane sample과 completion은 radio_adapter가 **항상 인접한 쌍**으로 방출한다.
    표본만 폐기하면 완료가 빈 버퍼로 도착해 phy_bridge의 "성공 terminal의
    buffered sample이 정확히 한 연속 8,192-sample block이 아닙니다"가 반드시
    터진다.

    실측 phase-g-service-20260831-161748 (나이 폐기를 넣었던 실행):
      폐기 ROC 4,275 · MASS-A 5,761 · MASS-B 5,684
      위반 ROC 6,661 · MASS-A 8,880 · MASS-B 8,776   (비 1.54~1.56)
    lane 하나만 오류여도 논리 창 전체가 폐기되는 병합 규칙 탓에 수신 표본의
    44~55%가 파괴됐고, 정작 큐 포화(처리량 문제)는 해결하지 못했다.

    다시 넣으려면 표본과 완료를 **함께** 버리거나, 폐기한 창을
    record_rx_fault/reset_stream으로 정리해 완료가 도달하지 않게 해야 한다.
    """
    body = _drain_source()
    assert "_SAMPLE_MAX_AGE_S" not in body, (
        "표본 나이 폐기가 다시 들어왔다 - 완료와 짝을 맞추지 않으면 "
        "PHY 불변식을 깬다")


def test_completion_promotion_preserves_sample_ordering():
    """같은 배치에 표본이 있는 창의 완료를 앞으로 빼면 안 된다.

    완료를 무조건 앞세웠더니 창이 표본을 받기 전에 닫혀 빈 버퍼로 성공
    종료를 시도했다 — phase-g-service-20260831-152333 실측:
    PHY_INTERNAL_INVARIANT_BROKEN 12,925건 (buffered_samples=0).
    직전 두 실행은 0건이었다. 표본→완료 순서는 PHY 불변식의 요구다.
    """
    body = _drain_source()
    assert "sample_keys" in body, "표본 결속 조건이 사라졌다"
    assert 'item[0] == "c" and tuple(item[1:4]) not in sample_keys' in body,         "같은 창에 표본이 있어도 완료를 승격하고 있다"


def test_stall_budget_is_preserved():
    """300ms 예산과 MAC 미니사이클은 그대로 유지돼야 한다 (기존 보호)."""
    body = _drain_source()
    assert "> 0.3" in body
    assert "mac_cycle()" in body
    assert "appendleft(leftover)" in body


# ------------------------------------------------------ decode worker 경계

def _pool_source():
    source = _read(LAUNCHER)
    start = source.index("class _DecodePool:")
    return source[start:source.index("_D7.__init__ = ", start)]


def test_decode_and_reset_share_one_fifo():
    """PHY stream은 연속성으로 보존/폐기를 판정한다 — reset이 decode를
    앞지르면 결과가 조용히 달라진다. 두 job은 같은 큐로 가야 한다."""
    body = _pool_source()
    assert "JOB_DECODE" in body and "JOB_RESET" in body
    for marker in ('self._jobs[lane_index].put_nowait',):
        assert body.count(marker) >= 2, (
            "decode와 reset이 같은 lane 큐로 가지 않는다")


def test_workers_are_spawned_not_forked():
    """런처는 여러 스레드를 쓴다 — fork는 자식에서 교착할 수 있다."""
    body = _pool_source()
    assert 'get_context("spawn")' in body, "spawn이 아니다"
    assert "fork" not in body


def test_queues_are_bounded():
    source = _read(LAUNCHER)
    assert "_DECODE_JOB_QUEUE_MAX" in source
    assert "_DECODE_RESULT_QUEUE_MAX" in source
    body = _pool_source()
    assert body.count("maxsize=") >= 2, "큐가 unbounded다"


def test_failure_is_closed_and_never_restarts():
    """crash·큐 포화·지연은 재시작하지 않고 세션을 끝낸다.

    stream 연속성과 진단 기록이 사라진 뒤의 조용한 재기동은 정상 운전처럼
    보이는 공백을 만든다.
    """
    body = _pool_source()
    fail = body[body.index("def fail(self, reason):"):]
    # generation 무효화가 in-flight terminal보다 먼저여야 늦게 온 결과가
    # stale로 폐기된다.
    assert fail.index("_lane_generation = -1") < fail.index("_lane_terminal")
    assert "os._exit(3)" in fail, "세션을 fail-closed로 끝내지 않는다"
    for banned in ("restart", "respawn", "_start_worker("):
        assert banned not in body, f"자동 재시작 흔적: {banned}"


def test_fatal_paths_are_not_silent():
    """큐 포화·사망·지연·결과 반영 실패가 전부 fail()로 모여야 한다."""
    body = _pool_source()
    for marker in ("큐 포화", "worker 사망", "결과 지연", "worker FATAL"):
        assert marker in body, f"{marker} 경로가 없다"


def test_results_are_committed_on_the_pump_thread_under_dispatch_lock():
    source = _read(LAUNCHER)
    pump = source[source.index("def _pump_decode_results():"):]
    pump = pump[:pump.index("def _drain_dispatch_queue():")]
    assert "with _dispatch_lock:" in pump, (
        "결과 반영이 배송 직렬화 락 밖에서 일어난다")
    drain = _drain_source()
    assert "_pump_decode_results()" in drain, (
        "펌프 회전마다 결과를 반영하지 않는다")


def test_startup_failure_does_not_fall_back_silently():
    """워커 기동 실패 시 동기 경로로 조용히 떨어지면 어느 경로로 돌았는지
    모르는 evidence가 남는다."""
    source = _read(LAUNCHER)
    init = source[source.index("def _dispatcher_init_with_decode_pool("):]
    init = init[:init.index("_D7.__init__ = ")]
    assert "raise" in init


if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            print(f"  {name} OK")
    print("PASS: 상주 세션 복구 경로 계약")
