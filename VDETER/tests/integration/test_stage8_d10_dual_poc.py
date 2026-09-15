"""Real child/socket, shared production D-10, normal Data Session plus dual RX.

RF-off only: endpoints share one harness process; device I/O is the existing
software group. Launcher D-11/D-18 timing and actual B210 load are NOT proved.
"""
import base64
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from tests.integration import test_stage8_collision_socket as harness


@unittest.skipUnless(sys.platform.startswith('linux'), 'real Unix sockets and child')
class D10DualPocTest(unittest.TestCase):
    _stop_child = harness.CollisionSocketTests._stop_child

    def setUp(self):
        original = subprocess.Popen
        def spawn(args, *a, **kw):
            args = list(args)
            if 'tests.support.poc_device_harness' in args:
                args[args.index('tests.support.poc_device_harness')] = 'tests.support.poc_delivery_harness'
            return original(args, *a, **kw)
        with patch.object(subprocess, 'Popen', side_effect=spawn):
            harness.CollisionSocketTests.setUp(self)

    def _exercise(self, payload, payload_format='test.stage0.opaque', *, promoted=True):
        from apps.stage7_mass_roc_controller import _AdapterChannel, PocWorkloadHost
        from apps.poc_device_seams_launcher import LauncherDeviceClient
        from apps.standalone import poc_run
        from vdeter.poc.execution.snapshot import create_snapshot
        from vdeter.poc.testing.execution_fakes import write_poc_package
        from vdeter.poc.execution.verdict import ScenarioVerdict, PocInfrastructure
        # required dual workload를 쓰는 시험이므로 지원 조건을 충족한 설치 증언 fixture 안에서
        # 돈다(위치만 임시. 판정 논리는 production 그대로이며 후보 설치 검증이 아니다).
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(harness._attested_installation())
        for name, channel in tuple(self.controller._channels.items()):
            self.controller._channels[name] = _AdapterChannel(channel._client, timeout_s=3)
        tuning = LauncherDeviceClient(self.controller, 'MASS-B')
        read = dict(mode='READ', rx_lo_hz=None, lane_dsp_hz=None, analog_bandwidth_hz=None, snapshot=None)
        before = tuning.call('poc_tuning', read)
        host = PocWorkloadHost(controller=self.controller, events=None)
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory(prefix='d10-dual-poc-') as root:
            source = os.path.join(root, 'poc')
            os.mkdir(source, 0o700)
            write_poc_package(source, 'observer',
                capabilities=('observe_packet', 'observe_dual_physical_channel'),
                required_capabilities=('observe_dual_physical_channel',))
            snapshot = create_snapshot(source, 'observer', dest_dir=os.path.join(root, 'snapshot'),
                                       scenario_id='SC-VDE-006')
            workload = None
            try:
                workload = poc_run.prepare_workload(host, snapshot, scenario_id='SC-VDE-006',
                    run_id='d10-dual', workload_id='w1', socket_root=os.path.join(root, 'socket'))
                self.assertTrue(host._exchange.dual_ready)
                workload.deliver_scenario_start()
                response = self.controller.invoke_service_inline('ROC', scenario_id='ds-coexist',
                    step_id='submit-once', service_operation='SUBMIT', request=dict(
                        destination=111111111, payload_format=payload_format, ack_required=True,
                        payload_base64=base64.b64encode(payload).decode('ascii'), payload_sha256=digest))
                self.assertEqual('OK', response['body']['call_outcome'], response)
                transfer = response['body']['transfer_id']
                deadline, state, observed = time.monotonic()+120, None, 0
                while time.monotonic() < deadline:
                    for observation in host.new_observations():
                        workload.deliver_observation(observation)
                        observed += 1
                    response = self.controller.invoke_service_inline('ROC', scenario_id='ds-coexist',
                        step_id='status', service_operation='STATUS', request={'transfer_id': transfer})
                    state = response['body']['transfer_state']
                    if state in ('COMPLETED', 'FAILED', 'CANCELLED'):
                        break
                    time.sleep(.1)
                self.assertEqual('COMPLETED', state, response)
                self.assertGreater(observed, 0)
                result = workload.run(ScenarioVerdict.PASS)
                self.assertEqual(PocInfrastructure.COMPLETED, result.poc_infrastructure)
            finally:
                if workload is not None:
                    workload.abort()
                closed = host.close_collision_exchange()
        self.assertTrue(host._exchange.evidence['cleanup_complete'])
        # C항: tuning 적용·복원 readback 값이 **실 dual 경로**에서 evidence에 실린다.
        # 이 시험은 required dual PoC로 적용 경로에 실제로 진입하므로, 값이 비어 있으면
        # 그것은 규약(적용 안 함)이 아니라 배선 결함이다. mock 시험(collision_lifecycle)이
        # 값 보존·오류 이력을 덮고, 여기서는 실경로에서 값이 실제로 생기는지를 본다.
        # 적용 전/후 값이 없거나 복원이 미검증이면 복원 PASS로 세지 않는다.
        actor = (closed or {})['endpoints']['MASS-B']
        self.assertIsNotNone(actor['dual_previous'], closed)
        self.assertIsNotNone(actor['dual_readback'], closed)
        self.assertIs(actor['restore']['verified'], True, actor['restore'])
        self.assertEqual(actor['restore']['previous'], actor['restore']['readback'])
        # dual이 아닌 endpoint는 값이 없다 — 있는 척하지 않는다.
        self.assertIsNone((closed or {})['endpoints']['MASS-A']['dual_readback'])
        # A(설치 capability)는 설치 증언에서만 온다. 이 시험은 증언 fixture 안에서 돌므로
        # 광고되며, 그 값은 scope close 여부와 무관하다(runtime readiness를 추종하지 않는다).
        self.assertTrue(host.poc_server_config().wsm_g1_production_installed)
        self.assertEqual(before, tuning.call('poc_tuning', read))
        self._stop_child()
        log = self.child.stderr.read()
        self.assertEqual(promoted, 'short->session' in log)
        deliveries = [json.loads(line.split(' ', 1)[1]) for line in log.splitlines()
                      if line.startswith('[accepted-delivery] ')]
        matches = [r for r in deliveries if r['ok'] and digest in r['payload_hashes']]
        self.assertEqual([111111111], [r['station'] for r in matches])

    def test_opaque43_shared_d10(self):
        self._exercise(b'normal-single-submission-with-real-dual-poc')

    def test_scenario005_command_shared_d10(self):
        from apps.console_transport import strip_envelope
        from apps.stage7_mass_roc_controller import _default_agents
        from common import config
        from common.messages import route_instruction as route
        roc, _ = _default_agents()
        points = [route.waypoint_from_degrees(129.076, 35.102, turn_radius_nm=.5, planned_sog_knots=12.5,
                      leg_geometry=route.LEG_GEOMETRY_LEXODROME),
                  route.waypoint_from_degrees(129.333, 35.208, turn_radius_nm=1.2, planned_sog_knots=8.,
                      leg_geometry=route.LEG_GEOMETRY_ORTHODROME),
                  route.waypoint_from_degrees(129.5, 35.3)]
        command = roc.issue_route(config.MASS_MMSI, points, now=1789013088.,
                                 route_type=route.ROUTE_TYPE_MANDATORY)
        self._exercise(strip_envelope(command.payload)[0], 'sc_vde.route_control.command')

    def test_opaque768_native_session_not_limited_to641(self):
        self._exercise(bytes(i % 256 for i in range(768)), promoted=False)
