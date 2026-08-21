"""단계 0 시험용 endpoint fixture.

`FakeRadioAdapter`와 Stub 계층으로 조립한 endpoint를 만든다. 모든 값은 test scope다.
"""

from __future__ import annotations

import os

from vdeter.config.loader import load_configuration, load_configuration_file
from vdeter.runtime.endpoint import build_endpoint
from vdeter.runtime.wiring import build_dependencies

from tests import VDETER_ROOT

CONFIG_DIR = os.path.join(VDETER_ROOT, "configs")
FAKE_CONFIG_PATH = os.path.join(CONFIG_DIR, "stage0_fake_endpoint.toml")
SOFTWARE_CONFIG_PATH = os.path.join(CONFIG_DIR, "stage0_software_skeleton.toml")
B210_CONFIG_PATH = os.path.join(CONFIG_DIR, "stage0_b210_skeleton.toml")


def load_fake_configuration():
    result = load_configuration_file(FAKE_CONFIG_PATH)
    assert result.is_ok, result.error if result.is_err else ""
    return result.value


def build_fake_endpoint():
    """(endpoint, dependencies)를 만든다."""

    configuration = load_fake_configuration()
    dependencies = build_dependencies(configuration)
    assert dependencies.is_ok, dependencies.error if dependencies.is_err else ""
    endpoint = build_endpoint(dependencies.value)
    assert endpoint.is_ok
    return endpoint.value, dependencies.value


def config_text(**overrides) -> str:
    """시험용 TOML 문자열을 만든다. override는 그대로 치환한다."""

    base = read_config_text(FAKE_CONFIG_PATH)
    for old, new in overrides.items():
        base = base.replace(old.replace("__", " "), new)
    return base


def read_config_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def load_text_configuration(text: str):
    return load_configuration(text, "<test>")
