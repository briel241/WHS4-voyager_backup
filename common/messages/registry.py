"""
Message ID와 Type별 모듈을 잇는 매핑

링크 계층 vdeter_link/messages/registry.py와 같은 방식이다.
새 메시지를 추가할 때는 모듈을 만들고 아래 dictionary에 등록한 뒤
codec.py의 ApplicationMessage Union에 dataclass를 더한다.
"""

from __future__ import annotations

from . import (
    ais_auth,
    application_ack,
    environmental_info,
    navigational_warning,
    position_report,
    route_instruction,
    weather_observation,
)

MESSAGE_CODECS = {
    position_report.MESSAGE_ID: position_report,
    route_instruction.MESSAGE_ID: route_instruction,
    application_ack.MESSAGE_ID: application_ack,
    ais_auth.MESSAGE_ID: ais_auth,
    navigational_warning.MESSAGE_ID: navigational_warning,
    environmental_info.MESSAGE_ID: environmental_info,
    weather_observation.MESSAGE_ID: weather_observation,
}

# dataclass 타입에서 Message ID를 역으로 찾는다.
MESSAGE_IDS = {
    position_report.PositionReport: position_report.MESSAGE_ID,
    route_instruction.RouteInstruction: route_instruction.MESSAGE_ID,
    application_ack.ApplicationAck: application_ack.MESSAGE_ID,
    ais_auth.AisAuthValue: ais_auth.MESSAGE_ID,
    navigational_warning.NavigationalWarning: navigational_warning.MESSAGE_ID,
    environmental_info.EnvironmentalInfo: environmental_info.MESSAGE_ID,
    weather_observation.WeatherObservation: weather_observation.MESSAGE_ID,
}
