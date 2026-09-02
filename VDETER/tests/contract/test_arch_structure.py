"""TC-ARCH-001..004: package 구조, import 경계, Port 계약, 책임 분리.

요구사항: `ARCH-REQ-01`, `ARCH-REQ-03`, `ARCH-REQ-04`, `ARCH-REQ-07`
"""

from __future__ import annotations

import ast
import importlib
import os
import sys
import typing
import unittest

from tests import SRC_ROOT, VDETER_ROOT

TARGET_PACKAGES = (
    "vdeter",
    "vdeter.common",
    "vdeter.common.contracts",
    "vdeter.service",
    "vdeter.presentation",
    "vdeter.presentation.codecs",
    "vdeter.link",
    "vdeter.link.messages",
    "vdeter.link.packet",
    "vdeter.link.segment",
    "vdeter.link.short_data",
    "vdeter.link.data_session",
    "vdeter.mac",
    "vdeter.phy",
    "vdeter.phy.burst",
    "vdeter.phy.fec",
    "vdeter.phy.modulation",
    "vdeter.radio",
    "vdeter.radio.software",
    "vdeter.radio.b210",
    "vdeter.profiles",
    "vdeter.config",
    "vdeter.runtime",
)

TARGET_MODULES = (
    "vdeter.common.identifiers",
    "vdeter.common.addressing",
    "vdeter.common.time",
    "vdeter.common.random_source",
    "vdeter.common.errors",
    "vdeter.common.results",
    "vdeter.common.events",
    "vdeter.common.serialization",
    "vdeter.common.logging",
    "vdeter.common.contracts.service",
    "vdeter.common.contracts.presentation",
    "vdeter.common.contracts.link",
    "vdeter.common.contracts.message",
    "vdeter.common.contracts.mac",
    "vdeter.common.contracts.phy",
    "vdeter.common.contracts.radio",
    "vdeter.service.interface",
    "vdeter.service.models",
    "vdeter.presentation.interface",
    "vdeter.presentation.models",
    "vdeter.presentation.registry",
    "vdeter.presentation.stub",
    "vdeter.link.interface",
    "vdeter.link.models",
    "vdeter.link.stub",
    "vdeter.link.messages.interface",
    "vdeter.link.messages.registry",
    "vdeter.link.messages.builtins",
    "vdeter.link.packet.builder",
    "vdeter.link.packet.crc",
    "vdeter.link.packet.models",
    "vdeter.link.packet.parser",
    "vdeter.link.segment.allocator",
    "vdeter.link.segment.codec",
    "vdeter.link.segment.models",
    "vdeter.link.short_data.codec",
    "vdeter.link.data_session.fragmentation",
    "vdeter.link.data_session.models",
    "vdeter.link.data_session.reassembly",
    "vdeter.mac.interface",
    "vdeter.mac.models",
    "vdeter.mac.stub",
    "vdeter.phy.interface",
    "vdeter.phy.models",
    "vdeter.phy.stub",
    "vdeter.radio.interface",
    "vdeter.radio.models",
    "vdeter.radio.fake",
    "vdeter.profiles.models",
    "vdeter.profiles.payload_formats",
    "vdeter.profiles.messages",
    "vdeter.profiles.link_phy",
    "vdeter.config.models",
    "vdeter.config.loader",
    "vdeter.runtime.wiring",
    "vdeter.runtime.endpoint",
    "vdeter.runtime.projection",
)

#: 외부 무선 환경 의존성. core import에서 필요하지 않아야 한다.
EXTERNAL_RADIO_MODULES = ("gnuradio", "uhd", "pmt")

LAYER_PACKAGES = (
    "service",
    "presentation",
    "link",
    "mac",
    "phy",
    "radio",
)


def _iter_source_files():
    for root, _dirs, files in os.walk(os.path.join(SRC_ROOT, "vdeter")):
        if "__pycache__" in root:
            continue
        for name in sorted(files):
            if name.endswith(".py"):
                yield os.path.join(root, name)


def _module_name(path: str) -> str:
    relative = os.path.relpath(path, SRC_ROOT).replace(os.sep, ".")
    if relative.endswith(".__init__.py"):
        return relative[: -len(".__init__.py")]
    return relative[: -len(".py")]


def _current_package(path: str) -> str:
    """상대 import의 기준이 되는 package 이름."""

    module_name = _module_name(path)
    if os.path.basename(path) == "__init__.py":
        return module_name
    return module_name.rpartition(".")[0]


def _imported_modules(path: str) -> tuple:
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    package_parts = _current_package(path).split(".")
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package_parts[: len(package_parts) - (node.level - 1)]
                target = ".".join(base + ([node.module] if node.module else []))
            else:
                target = node.module or ""
            imported.append(target)
    return tuple(imported)


def _layer_of(module_name: str) -> str | None:
    parts = module_name.split(".")
    if len(parts) < 2 or parts[0] != "vdeter":
        return None
    return parts[1]


def _numpy_imports_all_guarded(path: str) -> bool:
    """numpy import가 전부 try/except ImportError 본문 안에 있는지."""

    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)

    def _is_numpy(node) -> bool:
        if isinstance(node, ast.Import):
            return any(alias.name.split(".")[0] == "numpy" for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            return (node.module or "").split(".")[0] == "numpy"
        return False

    guarded = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        names = []
        for handler in node.handlers:
            if isinstance(handler.type, ast.Name):
                names.append(handler.type.id)
            elif isinstance(handler.type, ast.Tuple):
                names.extend(e.id for e in handler.type.elts
                             if isinstance(e, ast.Name))
        if "ImportError" not in names:
            continue
        for statement in node.body:
            for child in ast.walk(statement):
                if _is_numpy(child):
                    guarded.add(id(child))
    return all(
        id(node) in guarded
        for node in ast.walk(tree) if _is_numpy(node)
    )



class TargetPackageImportTest(unittest.TestCase):
    """TC-ARCH-001: 전체 목표 package·module import와 외부 의존성 부재."""

    def test_TC_ARCH_001_all_target_packages_import(self):
        for name in TARGET_PACKAGES + TARGET_MODULES:
            with self.subTest(module=name):
                importlib.import_module(name)

    def test_TC_ARCH_001_core_import_without_gnuradio_uhd(self):
        for name in TARGET_PACKAGES + TARGET_MODULES:
            importlib.import_module(name)
        for external in EXTERNAL_RADIO_MODULES:
            self.assertNotIn(
                external,
                sys.modules,
                f"core import가 {external} module을 필요로 했습니다",
            )

    def test_TC_ARCH_001_radio_skeletons_import_without_dependencies(self):
        software = importlib.import_module("vdeter.radio.software")
        b210 = importlib.import_module("vdeter.radio.b210")
        self.assertTrue(hasattr(software, "SoftwareRadioAdapter"))
        self.assertTrue(hasattr(b210, "B210RadioAdapter"))
        for external in EXTERNAL_RADIO_MODULES:
            self.assertNotIn(external, sys.modules)

    def test_TC_ARCH_001_numpy_is_optional_dsp_dependency(self):
        try:
            import tomllib
        except ModuleNotFoundError:  # pragma: no cover - Python 3.10
            import tomli as tomllib  # type: ignore[no-redef]

        with open(os.path.join(VDETER_ROOT, "pyproject.toml"), "rb") as handle:
            project = tomllib.load(handle)["project"]
        required = tuple(item.lower() for item in project.get("dependencies", ()))
        dsp = tuple(
            item.lower()
            for item in project.get("optional-dependencies", {}).get("dsp", ())
        )
        self.assertFalse(any(item.startswith("numpy") for item in required))
        self.assertTrue(any(item.startswith("numpy") for item in dsp))


class ForbiddenImportTest(unittest.TestCase):
    """TC-ARCH-002: 금지된 계층 간 import와 실험 경로 의존 정적 검사."""

    def test_TC_ARCH_002_common_does_not_import_layers(self):
        for path in _iter_source_files():
            module_name = _module_name(path)
            if not module_name.startswith("vdeter.common"):
                continue
            for imported in _imported_modules(path):
                layer = _layer_of(imported)
                self.assertNotIn(
                    layer,
                    LAYER_PACKAGES + ("runtime", "config", "profiles"),
                    f"{module_name}이 {imported}를 import합니다",
                )

    def test_TC_ARCH_002_layers_do_not_import_other_layers(self):
        for path in _iter_source_files():
            module_name = _module_name(path)
            own_layer = _layer_of(module_name)
            if own_layer not in LAYER_PACKAGES:
                continue
            for imported in _imported_modules(path):
                imported_layer = _layer_of(imported)
                if imported_layer is None or imported_layer == own_layer:
                    continue
                self.assertIn(
                    imported_layer,
                    ("common", "profiles"),
                    f"{module_name}이 다른 계층 {imported}를 import합니다",
                )

    def test_TC_ARCH_002_layers_do_not_import_runtime(self):
        for path in _iter_source_files():
            module_name = _module_name(path)
            if module_name.startswith("vdeter.runtime"):
                continue
            # 검사 대상은 6개 layer뿐이다 (자매 시험과 동일 스코프). stage7 등
            # runtime-오케스트레이션 additive 모듈은 설계상 runtime을 사용한다
            # (stage7 기준선 §4.1) — 2026-08-27 사용자 승인 스코프 정정.
            if _layer_of(module_name) not in LAYER_PACKAGES:
                continue
            for imported in _imported_modules(path):
                self.assertFalse(
                    imported.startswith("vdeter.runtime"),
                    f"{module_name}이 runtime을 import합니다",
                )

    def test_TC_ARCH_002_profiles_and_config_do_not_import_layers(self):
        for path in _iter_source_files():
            module_name = _module_name(path)
            if not (
                module_name.startswith("vdeter.profiles")
                or module_name.startswith("vdeter.config")
            ):
                continue
            own_layer = _layer_of(module_name)
            for imported in _imported_modules(path):
                imported_layer = _layer_of(imported)
                if imported_layer is None or imported_layer == own_layer:
                    continue
                if own_layer == "config" and imported_layer == "radio":
                    # config는 adapter 선택 enum(radio.models)만 참조한다.
                    self.assertTrue(
                        imported.startswith("vdeter.radio.models"),
                        f"{module_name}이 {imported}를 import합니다",
                    )
                    continue
                self.assertIn(
                    imported_layer,
                    ("common", "profiles"),
                    f"{module_name}이 계층 {imported}를 import합니다",
                )

    def test_TC_ARCH_002_no_source_imports_legacy_experiments(self):
        """공식 경로는 `legacy/`의 비운영 보관본을 import하지 않는다 (DR-STAGE0-003)."""

        forbidden_roots = ("temporary_bpsk", "legacy", "experiments")
        for path in _iter_source_files():
            for imported in _imported_modules(path):
                root = imported.split(".")[0]
                self.assertNotIn(
                    root,
                    forbidden_roots,
                    f"{_module_name(path)}이 보관본 경로 {imported}를 import합니다",
                )

    def test_TC_ARCH_002_legacy_is_outside_the_source_tree(self):
        """보관본은 `src/vdeter` 밖에만 존재한다."""

        source_root = os.path.join(SRC_ROOT, "vdeter")
        for name in ("legacy", "experiments", "temporary_bpsk"):
            with self.subTest(directory=name):
                self.assertFalse(os.path.exists(os.path.join(source_root, name)))
        self.assertTrue(
            os.path.isdir(os.path.join(VDETER_ROOT, "legacy", "temporary_bpsk")),
            "격리한 임시 BPSK 보관본이 legacy/에 없습니다",
        )

    #: numpy 허용 모듈 — 파일 단위 한정 (phy 계층 포괄 허용 아님).
    #: pyproject가 numpy를 기본 의존이 아닌 `dsp` 선택 의존으로 두고
    #: "PHY·Radio 구현 단계에서 사용한다"고 선언하므로, 검사 범위를 그
    #: 기존 설계 계약과 일치시킨다 (시험 완화가 아니다 — 2026-09-02 사용자
    #: 승인). 허용 모듈은 ImportError 가드와 순수 Python 폴백이 강제되며,
    #: 폴백 동작·규격 판정·복호 동치는 `tests/unit/test_phy_numpy_optional.py`가
    #: 고정한다.
    NUMPY_ALLOWED_MODULES = frozenset({
        "vdeter.phy.resampling",
        "vdeter.phy.burst.rrc",
    })

    def test_TC_ARCH_002_no_external_radio_dependency_in_core_layers(self):
        device_roots = ("gnuradio", "uhd", "pmt")
        for path in _iter_source_files():
            module_name = _module_name(path)
            layer = _layer_of(module_name)
            if layer not in ("common", "service", "presentation", "link", "mac", "phy", "profiles"):
                continue
            for imported in _imported_modules(path):
                root = imported.split(".")[0]
                self.assertNotIn(
                    root,
                    device_roots,
                    f"{module_name}이 {imported}를 import합니다",
                )
                if root != "numpy":
                    continue
                self.assertIn(
                    module_name,
                    self.NUMPY_ALLOWED_MODULES,
                    f"{module_name}이 {imported}를 import합니다 — "
                    "numpy는 허용 목록 모듈에서만 쓸 수 있습니다",
                )

    def test_TC_ARCH_002_numpy_allowed_modules_keep_the_import_guarded(self):
        for module_name in sorted(self.NUMPY_ALLOWED_MODULES):
            path = os.path.join(
                SRC_ROOT, *module_name.split(".")) + ".py"
            self.assertTrue(
                _numpy_imports_all_guarded(path),
                f"{module_name}의 numpy import가 try/except ImportError "
                "가드 밖에 있습니다 — 순수 Python 폴백 강제 위반",
            )

    def test_TC_ARCH_002_radio_skeletons_have_no_module_level_device_import(self):
        for relative in ("radio/software/__init__.py", "radio/b210/__init__.py"):
            path = os.path.join(SRC_ROOT, "vdeter", *relative.split("/"))
            for imported in _imported_modules(path):
                root = imported.split(".")[0]
                self.assertNotIn(root, ("gnuradio", "uhd", "pmt"))


class PortContractTest(unittest.TestCase):
    """TC-ARCH-003: 최소 Port method와 EventSink 계약."""

    EXPECTED = {
        "vdeter.service.interface:VdeService": ("start", "stop", "submit", "cancel", "status"),
        "vdeter.presentation.interface:PresentationPort": ("encode_payload", "decode_payload"),
        "vdeter.link.interface:LinkPort": (
            "analyze_transfer",
            "build_packet",
            "encode_packet",
            "encode_tbb",
            "decode_packet",
            "process_fragment",
            "control_reassembly",
            "control_transfer",
            "control_tbb_assembly",
        ),
        "vdeter.mac.interface:MacPort": (
            "submit_requirement",
            "handle_recovered_packet",
            "handle_message",
            "control_session",
        ),
        "vdeter.phy.interface:PhyPort": ("encode_packet", "decode_samples"),
        "vdeter.radio.interface:RadioAdapter": (
            "configure",
            "start",
            "stop",
            "schedule_tx",
            "request_rx",
            "status",
        ),
        "vdeter.common.events:InternalEventSink": ("publish",),
        "vdeter.common.events:ServiceEventSink": ("publish",),
    }

    IMPLEMENTATIONS = {
        "vdeter.presentation.stub:StubPresentationAdapter": (
            "encode_payload",
            "decode_payload",
        ),
        "vdeter.link.stub:StubLink": ("analyze_transfer", "build_packet", "decode_packet"),
        "vdeter.mac.stub:StubMac": (
            "submit_requirement",
            "handle_recovered_packet",
            "handle_message",
            "control_session",
        ),
        "vdeter.phy.stub:StubPhy": ("encode_packet", "decode_samples"),
        "vdeter.radio.fake:FakeRadioAdapter": (
            "configure",
            "start",
            "stop",
            "schedule_tx",
            "request_rx",
            "status",
        ),
        "vdeter.radio.software:SoftwareRadioAdapter": (
            "configure",
            "start",
            "stop",
            "schedule_tx",
            "request_rx",
            "status",
        ),
        "vdeter.radio.b210:B210RadioAdapter": (
            "configure",
            "start",
            "stop",
            "schedule_tx",
            "request_rx",
            "status",
        ),
        "vdeter.runtime.endpoint:VdeTerEndpoint": (
            "start",
            "stop",
            "submit",
            "cancel",
            "status",
        ),
        "vdeter.common.events:RecordingInternalEventSink": ("publish",),
        "vdeter.common.events:RecordingServiceEventSink": ("publish",),
    }

    def _resolve(self, reference: str):
        module_name, symbol = reference.split(":")
        module = importlib.import_module(module_name)
        self.assertTrue(hasattr(module, symbol), f"{reference}가 없습니다")
        return getattr(module, symbol)

    def test_TC_ARCH_003_protocols_declare_minimum_methods(self):
        for reference, methods in self.EXPECTED.items():
            protocol = self._resolve(reference)
            for method in methods:
                with self.subTest(protocol=reference, method=method):
                    self.assertTrue(hasattr(protocol, method))

    def test_TC_ARCH_003_implementations_provide_minimum_methods(self):
        for reference, methods in self.IMPLEMENTATIONS.items():
            implementation = self._resolve(reference)
            for method in methods:
                with self.subTest(implementation=reference, method=method):
                    self.assertTrue(callable(getattr(implementation, method, None)))

    def test_TC_ARCH_003_ports_declare_exact_result_types(self):
        from vdeter.common.contracts.link import (
            BuiltPacket,
            EncodedPacket,
            FragmentProcessResult,
            LinkTransferControlResult,
            ReassemblyControlResult,
            TbbAssemblyControlResult,
            TbbEncodedPacketSet,
            TransferRequirement,
        )
        from vdeter.common.contracts.mac import (
            MacActionSet,
            PacketDecodeAction,
            RequirementAccepted,
            SessionControlResult,
        )
        from vdeter.common.contracts.message import DecodedMessages
        from vdeter.common.contracts.phy import BasebandBurst, RecoveredPacketSet
        from vdeter.common.contracts.presentation import ApplicationData, PresentationData
        from vdeter.common.contracts.radio import (
            RadioConfigurationApplied,
            RadioRxAccepted,
            RadioStarted,
            RadioStatusSnapshot,
            RadioStopped,
            RadioTxAccepted,
        )
        from vdeter.common.contracts.service import (
            CancelAccepted,
            ServiceStarted,
            ServiceStopped,
            TransferAccepted,
            TransferStatusSnapshot,
        )
        from vdeter.common.events import EventPublished
        from vdeter.common.results import InternalResult, ServiceResult

        expected = {
            "vdeter.service.interface:VdeService": {
                "start": ServiceResult[ServiceStarted],
                "stop": ServiceResult[ServiceStopped],
                "submit": ServiceResult[TransferAccepted],
                "cancel": ServiceResult[CancelAccepted],
                "status": ServiceResult[TransferStatusSnapshot],
            },
            "vdeter.presentation.interface:PresentationPort": {
                "encode_payload": InternalResult[PresentationData],
                "decode_payload": InternalResult[ApplicationData],
            },
            "vdeter.link.interface:LinkPort": {
                "analyze_transfer": InternalResult[TransferRequirement],
                "build_packet": InternalResult[BuiltPacket],
                "encode_packet": InternalResult[EncodedPacket],
                "encode_tbb": InternalResult[TbbEncodedPacketSet],
                "decode_packet": InternalResult[DecodedMessages],
                "process_fragment": InternalResult[FragmentProcessResult],
                "control_reassembly": InternalResult[ReassemblyControlResult],
                "control_transfer": InternalResult[LinkTransferControlResult],
                "control_tbb_assembly": InternalResult[TbbAssemblyControlResult],
            },
            "vdeter.mac.interface:MacPort": {
                "submit_requirement": InternalResult[RequirementAccepted],
                "handle_recovered_packet": InternalResult[PacketDecodeAction],
                "handle_message": InternalResult[MacActionSet],
                "control_session": InternalResult[SessionControlResult],
            },
            "vdeter.phy.interface:PhyPort": {
                "encode_packet": InternalResult[BasebandBurst],
                "decode_samples": InternalResult[RecoveredPacketSet],
            },
            "vdeter.radio.interface:RadioAdapter": {
                "configure": InternalResult[RadioConfigurationApplied],
                "start": InternalResult[RadioStarted],
                "stop": InternalResult[RadioStopped],
                "schedule_tx": InternalResult[RadioTxAccepted],
                "request_rx": InternalResult[RadioRxAccepted],
                "status": InternalResult[RadioStatusSnapshot],
            },
            "vdeter.common.events:InternalEventSink": {
                "publish": InternalResult[EventPublished]
            },
            "vdeter.common.events:ServiceEventSink": {
                "publish": InternalResult[EventPublished]
            },
        }
        for reference, methods in expected.items():
            protocol = self._resolve(reference)
            for method_name, expected_return in methods.items():
                with self.subTest(protocol=reference, method=method_name):
                    hints = typing.get_type_hints(getattr(protocol, method_name))
                    self.assertEqual(expected_return, hints.get("return"))

    def test_TC_ARCH_003_ports_use_public_contract_dtos_only(self):
        """Port signature는 `common.contracts`의 공개 DTO만 참조한다."""

        for relative in (
            "service/interface.py",
            "presentation/interface.py",
            "link/interface.py",
            "mac/interface.py",
            "phy/interface.py",
            "radio/interface.py",
        ):
            path = os.path.join(SRC_ROOT, "vdeter", *relative.split("/"))
            for imported in _imported_modules(path):
                if not imported.startswith("vdeter"):
                    continue
                self.assertTrue(
                    imported.startswith("vdeter.common"),
                    f"{relative}이 {imported}를 import합니다",
                )


class ResponsibilityBoundaryTest(unittest.TestCase):
    """TC-ARCH-004: 승인 명세의 계층 책임을 실제 package 경계에서 검사한다.

    소유권 목록을 별도 manifest에 다시 복제하지 않는다. 책임의 판정 원본은 상위 명세 §6과
    각 단계 기준선이며, 이 시험은 공개 경계 module과 상태 혼합을 검사한다. 계층 간 금지
    의존은 `TC-ARCH-002`가 검사한다.
    """

    RUNTIME_STATE_FIELDS = frozenset(
        {
            "session_handle",
            "ack_state",
            "retransmission_count",
            "tbb_state",
            "runtime_buffer",
            "device_handle",
        }
    )

    def test_TC_ARCH_004_each_layer_has_public_boundary_modules(self):
        for layer in LAYER_PACKAGES:
            with self.subTest(layer=layer):
                package = os.path.join(SRC_ROOT, "vdeter", layer)
                self.assertTrue(os.path.isfile(os.path.join(package, "interface.py")))
                self.assertTrue(os.path.isfile(os.path.join(package, "models.py")))

    def test_TC_ARCH_004_profile_and_config_do_not_store_runtime_state(self):
        for package in ("profiles", "config"):
            root = os.path.join(SRC_ROOT, "vdeter", package)
            for name in sorted(os.listdir(root)):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(root, name)
                with open(path, "r", encoding="utf-8") as handle:
                    tree = ast.parse(handle.read(), filename=path)
                declared = {
                    target.id
                    for node in ast.walk(tree)
                    if isinstance(node, (ast.AnnAssign, ast.Assign))
                    for target in (
                        ([node.target] if isinstance(node, ast.AnnAssign) else node.targets)
                    )
                    if isinstance(target, ast.Name)
                }
                forbidden = sorted(declared & self.RUNTIME_STATE_FIELDS)
                self.assertEqual(
                    [], forbidden, f"{package}/{name}이 runtime 상태를 선언합니다: {forbidden}"
                )


if __name__ == "__main__":
    unittest.main()
