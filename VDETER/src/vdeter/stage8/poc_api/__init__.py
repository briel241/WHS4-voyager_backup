"""(호환) 옛 공개 경로 `vdeter.stage8.poc_api` -> 새 경로 `vdeter.poc.api`.

같은 module·class·enum·함수 **객체**를 옛 이름으로도 찾게 한다. 구현을 복제하지
않으므로 `isinstance`·enum 동일성이 두 경로에서 동일하다. 새 production 내부
소비자는 새 경로만 쓴다(계약시험이 정적으로 확인한다).

유지·제거 조건은 `vdeter.stage8` package docstring 참조.
"""

from __future__ import annotations

import sys

from vdeter.poc import api as _api

#: 공개 surface의 하위 module. 옛 이름으로도 **같은 객체**가 잡히게 등록한다.
_PUBLIC_SUBMODULES = (
    "codec", "errors", "limits", "messages", "negotiation",
    "protocol", "sdk", "seams", "transport", "types",
)

for _name in _PUBLIC_SUBMODULES:
    _module = getattr(_api, _name, None)
    if _module is None:  # pragma: no cover - 방어적
        _module = __import__("vdeter.poc.api." + _name, fromlist=(_name,))
    sys.modules[__name__ + "." + _name] = _module
    globals()[_name] = _module
del _name, _module

# 최상위 재수출(같은 객체 바인딩).
globals().update({_key: getattr(_api, _key) for _key in _api.__all__})

__all__ = tuple(_api.__all__)
