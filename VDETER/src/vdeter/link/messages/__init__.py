"""Type별 VDE-TER Message 모델과 codec package.

`interface.py`가 codec 계약을, `registry.py`가 명시적 등록·검증·freeze·조회를,
`builtins.py`가 단일 등록 지점을 제공한다. Stage 1 승인 Type 0, 4, 13, 20, 21, 22,
74, 75, 76, 81, 90, 92, 93은 각각 `type_XX.py`에서 구현하고 명시적으로 등록한다.
"""
