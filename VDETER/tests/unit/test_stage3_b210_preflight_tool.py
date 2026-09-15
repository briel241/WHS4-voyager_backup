import json
import os
import tempfile
import unittest

from tools.run_stage3_b210_no_tx_preflight import (
    _failed_device_summaries,
    _write_json_output,
)


class Stage3B210PreflightToolTests(unittest.TestCase):
    def test_output_parent_directories_are_created(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = os.path.join(
                temporary_directory,
                "nested",
                "results",
                "preflight.json",
            )
            payload = {"outcome": "PASS"}

            absolute_output_path = _write_json_output(output_path, payload)

            self.assertEqual(absolute_output_path, os.path.abspath(output_path))
            with open(output_path, "r", encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), payload)

    def test_failed_device_details_are_in_console_summary(self):
        failure = {
            "role": "ship",
            "serial": "34D03DD",
            "configured": False,
            "configure_result": {"code": "RADIO_CONFIGURATION_FAILED"},
            "cleanup_ok": False,
            "cleanup_result": {"code": "RADIO_NOT_CONFIGURED"},
        }
        success = {
            "role": "control",
            "serial": "34D6357",
            "configured": True,
            "configure_result": {"state": "CONFIGURED"},
            "cleanup_ok": True,
            "cleanup_result": {"state": "STOPPED"},
        }

        self.assertEqual(
            [
                {
                    "role": "ship",
                    "serial": "34D03DD",
                    "configure_result": failure["configure_result"],
                    "cleanup_result": failure["cleanup_result"],
                }
            ],
            _failed_device_summaries([failure, success]),
        )


if __name__ == "__main__":
    unittest.main()
