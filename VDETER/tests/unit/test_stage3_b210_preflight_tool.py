import json
import os
import tempfile
import unittest

from tools.run_stage3_b210_no_tx_preflight import _write_json_output


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


if __name__ == "__main__":
    unittest.main()
