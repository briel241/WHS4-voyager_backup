"""Test-only bridge: approved literal pins -> legacy Phase-G unparse pins.

Never used by production and never writes a reference. Validate the canonical
repository source against fixed AST AND literal pins before translating; do not
derive expected pins from the remote/mutated source under test.
"""

import ast
import hashlib
import json
from pathlib import Path


_ROOT = Path(__file__).resolve().parent.parent
_FIXTURE = _ROOT / "tests/fixtures/stage7_phase_i_function_bindings.json"
_RENAME_FIXTURE = (_ROOT /
                   "tests/fixtures/stage7_approved_rename_function_bindings.json")
_SOURCE = _ROOT / "VDETER/apps/coordinator_main.py"

#: 과거 자격 기록(phase-g reference·profile)의 **당시 경로** -> 현재 경로.
#: 2-3 구조 정리에서 승인된 이동만 담는다. 과거 기록 자체는 소급 수정하지 않으므로
#: 그 키로 현재 파일을 찾으려면 이 표가 필요하다. 표에 없는 경로는 그대로 쓴다.
QUALIFIED_PATH_MOVES = {
    "VDETER/apps/stage7_coordinator_main.py": "VDETER/apps/coordinator_main.py",
    "VDETER/apps/stage7_endpoint_launcher.py": "VDETER/apps/endpoint_launcher.py",
    "VDETER/apps/stage7_scenario_controller.py": "VDETER/apps/scenario_controller.py",
}


def current_path_for(qualified_relpath: str) -> str:
    """과거 기록의 경로를 현재 저장소 경로로 옮긴다(승인 대응표만 적용)."""
    if qualified_relpath in QUALIFIED_PATH_MOVES:
        return QUALIFIED_PATH_MOVES[qualified_relpath]
    moved = qualified_relpath
    for old, new in (("VDETER/src/vdeter/stage7/testing/adapter.py",
                      "VDETER/src/vdeter/b210_system/control_plane/adapter.py"),
                     ("VDETER/src/vdeter/stage7/testing/controller.py",
                      "VDETER/src/vdeter/b210_system/control_plane/controller.py"),
                     ("VDETER/src/vdeter/stage7/testing/poc_device.py",
                      "VDETER/src/vdeter/b210_system/control_plane/poc_device.py"),
                     ("VDETER/src/vdeter/stage7/", "VDETER/src/vdeter/b210_system/"),
                     ("VDETER/configs/stage7/", "VDETER/configs/b210_system/")):
        if moved.startswith(old):
            return new + moved[len(old):] if old.endswith("/") else new
    return moved


def legacy_phase_i_pins(*, canonical_source=None, bindings=None):
    if bindings is None:
        bindings = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    if (bindings.get("artifact_type") != "STAGE7_PHASE_I_APPROVED_FUNCTION_HASHES_V2"
            or bindings.get("source_sha256_binding", {}).get("method_id")
            != "literal-source-lf-v1"):
        raise ValueError("unsupported Phase-I source hash contract")
    if canonical_source is None:
        canonical_source = _SOURCE.read_text(encoding="utf-8")
    source = canonical_source.replace("\r\n", "\n")
    lines = source.split("\n")
    nodes = {node.name: node for node in ast.parse(source).body
             if isinstance(node, ast.FunctionDef)}
    sha = lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest()
    result = {}
    for name, pin in bindings["functions"].items():
        node = nodes.get(name)
        if node is None:
            raise ValueError(f"approved function missing: {name}")
        first = min([node.lineno] + [d.lineno for d in node.decorator_list]) - 1
        literal = "\n".join(lines[first:node.end_lineno]) + "\n"
        if (sha(ast.dump(node)) != pin["ast_sha256"]
                or sha(literal) != pin["source_sha256"]):
            raise ValueError(f"canonical approved function pin mismatch: {name}")
        result[name] = {"ast_sha256": pin["ast_sha256"],
                        "source_sha256": sha(ast.unparse(node))}
    return result


def approved_rename_pins(*, bindings=None):
    """2-3 승인 이름·경로 치환으로 해시가 바뀐 자격 함수의 **고정 해시**.

    Phase-I 승인 변경과 같은 방식이다: 이름 allow-list 가 아니라 고정 해시이므로
    이 기록 이후의 추가 변경은 다시 검출된다. 산출 근거·이전 값은 fixture 안에 있다.
    """
    import json as _json

    if bindings is None:
        bindings = _json.loads(_RENAME_FIXTURE.read_text(encoding="utf-8"))
    if bindings.get("artifact_type") != "STAGE7_APPROVED_RENAME_FUNCTION_HASHES_V1":
        raise ValueError("unsupported approved-rename hash contract")
    return {name: dict(spec) for name, spec in bindings["functions"].items()}
