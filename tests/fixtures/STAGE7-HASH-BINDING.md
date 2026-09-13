# Phase-I literal binding and legacy test compatibility

`stage7_phase_i_function_bindings.json` V2 uses `literal-source-lf-v1`:
SHA-256 of a function's literal source, decorators included, LF line endings,
one final LF, paired with its `ast.dump` SHA-256. Unknown method/version fails.
The AST check is still interpreter-dependent across Python minor releases;
literal source stability does not imply arbitrary Python-version support.

Qualification's `test_stage7_service_phase.py` compares these fixed pins directly.
Root host/CLI tests target the unchanged legacy verifier, which expects
`ast.unparse` hashes. `tests/stage7_binding_fixtures.py` first validates the
canonical repository source against both fixed pins, then translates ONLY those
approved functions to the current interpreter's legacy representation in memory.
It never takes expected hashes from a remote or mutated source under test.
Statement/comment mutations, unknown methods and invalid pins must be rejected.

Historical Phase-G reference files and `apps/stage7_service_layer.py` are not
modified. Their pre-existing cross-Python unparse mismatch for other functions
remains a legacy environment limitation, not a reason to skip or relax gates.
This fixture bridge neither qualifies an installation nor grants RF access.
