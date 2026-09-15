"""RF-off negative tests for literal/legacy fixture interoperability."""

import ast
import hashlib
import json
from pathlib import Path

import pytest

from stage7_binding_fixtures import legacy_phase_i_pins


ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "VDETER/apps/coordinator_main.py"
FIXTURE = ROOT / "tests/fixtures/stage7_phase_i_function_bindings.json"


def test_approved_source_translates_to_legacy_without_changing_fixture():
    before = FIXTURE.read_bytes()
    source = SOURCE.read_text(encoding="utf-8")
    nodes = {n.name: n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)}
    translated = legacy_phase_i_pins()
    for name, pin in translated.items():
        assert pin["source_sha256"] == hashlib.sha256(
            ast.unparse(nodes[name]).encode("utf-8")).hexdigest()
    assert FIXTURE.read_bytes() == before
    assert legacy_phase_i_pins(canonical_source=source.replace("\n", "\r\n")) == translated


@pytest.mark.parametrize("name", ["run_mass_roc_session", "run_rr_p0", "run_rr_p3_fault"])
@pytest.mark.parametrize("kind", ["statement", "comment"])
def test_changed_canonical_source_cannot_be_reapproved(name, kind):
    source = SOURCE.read_text(encoding="utf-8")
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == name)
    lines = source.splitlines(keepends=True)
    line = lines[node.body[0].lineno - 1]
    indent = line[:len(line) - len(line.lstrip())]
    inserted = "_unapproved_mutation = 1" if kind == "statement" else "# unapproved comment"
    lines.insert(node.body[0].lineno - 1, indent + inserted + "\n")
    with pytest.raises(ValueError, match="pin mismatch"):
        legacy_phase_i_pins(canonical_source="".join(lines))


@pytest.mark.parametrize("field", ["version", "method", "pin", "missing"])
def test_bad_binding_cannot_fall_back(field):
    bindings = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if field == "version":
        bindings["artifact_type"] = "STAGE7_PHASE_I_APPROVED_FUNCTION_HASHES_V1"
    elif field == "method":
        bindings["source_sha256_binding"]["method_id"] = "unknown"
    elif field == "pin":
        bindings["functions"]["run_rr_p0"]["source_sha256"] = "0" * 64
    else:
        del bindings["source_sha256_binding"]["method_id"]
    with pytest.raises(ValueError):
        legacy_phase_i_pins(bindings=bindings)
