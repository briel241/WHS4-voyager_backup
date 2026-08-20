"""단계 0 빈 endpoint 실행 진입점.

`apps`는 `service`, `config`, `runtime`의 공개 계약만 사용한다 (`stage0_baseline.md` §4).
protocol 로직을 여기에 작성하지 않는다.

사용법
```text
python apps/stage0_endpoint.py configs/stage0_fake_endpoint.toml
```

이 진입점은 설정을 읽어 endpoint를 조립하고 start → 상태 확인 → stop을 수행한다.
Fake/Stub 계층을 사용하므로 실제 VDE-TER 송수신을 수행하지 않는다.
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from vdeter.common.contracts.service import ServiceStartRequest, ServiceStopRequest  # noqa: E402
from vdeter.config.loader import load_configuration_file  # noqa: E402
from vdeter.runtime.endpoint import build_endpoint  # noqa: E402
from vdeter.runtime.wiring import build_dependencies  # noqa: E402


def run(config_path: str) -> int:
    loaded = load_configuration_file(config_path)
    if loaded.is_err:
        print(f"[config] {loaded.error.code}: {loaded.error.message}")
        return 2
    configuration = loaded.value

    dependencies = build_dependencies(configuration)
    if dependencies.is_err:
        print(f"[wiring] {dependencies.error.code}: {dependencies.error.message}")
        return 3

    endpoint_result = build_endpoint(dependencies.value)
    if endpoint_result.is_err:
        print(f"[endpoint] {endpoint_result.error.code}: {endpoint_result.error.message}")
        return 4
    endpoint = endpoint_result.value

    started = endpoint.start(
        ServiceStartRequest(
            local_station_id=configuration.endpoint.station_id,
            role=configuration.endpoint.role,
            config_ref=config_path,
        )
    )
    if started.is_err:
        print(f"[start] {started.error.code}: {started.error.message}")
        return 5
    print(f"[start] endpoint={started.value.endpoint_id} health={started.value.health.value}")
    for name, fingerprint in dependencies.value.registries.fingerprints():
        print(f"[registry] {name}={fingerprint[:16]}")
    print(f"[config] fingerprint={configuration.endpoint_fingerprint[:23]}")

    stopped = endpoint.stop(ServiceStopRequest(reason="apps entrypoint"))
    if stopped.is_err:
        print(f"[stop] {stopped.error.code}: {stopped.error.message}")
        return 6
    print(f"[stop] health={stopped.value.health.value}")
    return 0


def main(argv) -> int:
    if len(argv) != 2:
        print("usage: python apps/stage0_endpoint.py <config.toml>")
        return 1
    return run(argv[1])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
