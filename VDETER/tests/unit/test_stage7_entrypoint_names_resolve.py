"""진입점의 이름이 전부 해소되는가 — 실행해야만 드러나는 NameError 를 정적으로 잡는다.

왜 필요한가 (2026-09-15 실물 검출)
  1-3(STR-07)이 TBB fixture 계산을 production module 로 옮기면서, 같은 블록에 있던
  `import time as _time` 까지 지웠다. launcher `main()` 의 pump·drain·계측 경로는
  그 별칭을 13곳에서 쓰고 있었으므로 **모든 launcher 가 기동 직후 NameError 로
  죽었다**. 그런데 그 상태로 1-3·2-1·2-2·2-3·3-1·3-2·3-3 회귀가 전부 통과했다 —
  어떤 시험도 `endpoint_launcher.main()` 을 실행하지 않기 때문이다. 실물 RR-P0 의
  1초짜리 실패로 처음 드러났다.

무엇을 검사하는가
  `symtable` 로 각 진입점의 함수 스코프를 훑어, **어디에도 묶이지 않은 이름**
  (지역·enclosing·모듈 전역·builtins 어디에도 없는 이름)을 찾는다. 이런 이름은
  그 줄에 도달하는 즉시 NameError 다.

무엇을 검사하지 않는가
  동적 이름(`globals()[...]`·`setattr`)과 조건부 import 의 **실행 시점**은 보지
  않는다. 타입 오류·논리 오류도 보지 않는다. 이 시험은 "실행하면 반드시 NameError"
  인 부류만 잡는다 — 그 부류가 실제로 자격 실행을 날렸기 때문이다.
"""

import builtins
import os
import symtable
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

#: 실행 진입점 — 시험이 `main()` 을 돌리지 않는 파일들이다.
ENTRY_POINTS = (
    "VDETER/apps/endpoint_launcher.py",
    "VDETER/apps/coordinator_main.py",
    "VDETER/apps/scenario_controller.py",
    "apps/mass_roc_workload_controller.py",
    "apps/standalone/cli.py",
)

_BUILTINS = frozenset(dir(builtins))

#: 실행기가 모듈에 넣어 주는 이름 — 소스에 대입이 없어 symtable 이 못 본다.
_MODULE_DUNDERS = frozenset((
    "__file__", "__name__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__", "__path__", "__cached__", "__debug__",
    "__annotations__",
))


def _module_level_names(table: symtable.SymbolTable) -> frozenset:
    """모듈 스코프에서 묶이는 이름 (import·대입·def·class)."""
    return frozenset(symbol.get_name() for symbol in table.get_symbols()
                     if symbol.is_assigned() or symbol.is_imported()
                     or symbol.is_namespace())


def _unresolved(path: str) -> list:
    """(스코프 경로, 이름) — 어디에도 묶이지 않아 NameError 가 될 이름."""
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    top = symtable.symtable(source, path, "exec")
    known = _module_level_names(top) | _BUILTINS | _MODULE_DUNDERS
    found = []

    def walk(table, trail):
        for symbol in table.get_symbols():
            # `is_global()` = 이 스코프에서 묶이지 않아 전역으로 해소될 이름.
            # 그 중 모듈 전역에도 builtins 에도 없으면 실행 시 NameError 다.
            if (symbol.is_referenced() and symbol.is_global()
                    and symbol.get_name() not in known):
                found.append((" > ".join(trail), symbol.get_name()))
        for child in table.get_children():
            walk(child, trail + [child.get_name()])

    for child in top.get_children():
        walk(child, [child.get_name()])
    return sorted(set(found))


class TC_ENTRYPOINT_NAMES(unittest.TestCase):
    """진입점 함수가 쓰는 이름이 전부 해소된다."""

    def test_no_unresolved_name_in_any_entry_point(self):
        for relative in ENTRY_POINTS:
            with self.subTest(entry_point=relative):
                path = os.path.join(_REPO, relative.replace("/", os.sep))
                self.assertTrue(os.path.isfile(path), path)
                problems = _unresolved(path)
                self.assertEqual(problems, [], (relative, problems))

    def test_the_check_catches_the_defect_it_was_written_for(self):
        """음성 대조: `_time` 별칭을 지운 사본에서 이 검사가 실제로 잡는가.

        production 파일은 건드리지 않고 메모리 안 사본만 변형한다.
        """
        path = os.path.join(_REPO, "VDETER", "apps", "endpoint_launcher.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        alias = "    import time as _time\n"
        self.assertIn(alias, source, "복원한 별칭이 사라졌다")
        mutated = source.replace(alias, "", 1)

        table = symtable.symtable(mutated, path, "exec")
        known = (_module_level_names(table) | _BUILTINS
                 | _MODULE_DUNDERS)
        hits = []

        def walk(node):
            for symbol in node.get_symbols():
                if (symbol.is_referenced() and symbol.is_global()
                        and symbol.get_name() not in known):
                    hits.append((node.get_name(), symbol.get_name()))
            for child in node.get_children():
                walk(child)

        for child in table.get_children():
            walk(child)
        self.assertIn(("main", "_time"), hits, hits)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
