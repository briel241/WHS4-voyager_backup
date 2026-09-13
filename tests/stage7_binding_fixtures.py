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
_SOURCE = _ROOT / "VDETER/apps/stage7_coordinator_main.py"


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
