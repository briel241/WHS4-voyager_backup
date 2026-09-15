"""Payload format별 codec package.

단계 0에서는 공식 VPFI payload format codec을 구현하지 않는다. R1027 Certificate Report,
AIS Authentication Scheme 1과 전자해도 format codec은 단계 5 Presentation 기준선에서
추가하며, 각 codec은 `presentation.registry`에 명시적으로 등록한다.
"""
