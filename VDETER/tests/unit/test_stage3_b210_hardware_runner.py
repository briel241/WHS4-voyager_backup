import unittest

from tools.run_stage3_b210_hardware_tests import _case, _tests_without


class Stage3B210HardwareRunnerTests(unittest.TestCase):
    def test_excluded_disconnect_is_partial_not_full_pass(self):
        records = [
            {
                "test_id": f"test-{index}",
                "test_case": "TC-RADIO-012",
                "outcome": "PASS",
            }
            for index in range(5)
        ]
        evidence = []
        for mode, count in {
            "lifecycle": 20,
            "negative_admission": 1,
            "no_tx_window": 1,
            "async_prequeue": 1,
            "async_error_distinction": 1,
        }.items():
            evidence.extend(
                {"test_case": "TC-RADIO-012", "mode": mode}
                for _ in range(count)
            )

        result = _case(
            "TC-RADIO-012",
            records,
            evidence,
            ("controlled_usb_disconnect",),
        )

        self.assertEqual("PARTIAL_PASS", result["outcome"])
        self.assertTrue(result["requested_scope_complete"])
        self.assertFalse(result["evidence_complete"])
        self.assertEqual(
            ["controlled_usb_disconnect"], result["excluded_evidence_modes"]
        )

    def test_suite_filter_removes_only_named_test(self):
        class Sample(unittest.TestCase):
            def test_keep(self):
                pass

            def test_remove(self):
                pass

        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Sample)
        remove_id = f"{Sample.__module__}.{Sample.__qualname__}.test_remove"

        selected = tuple(_tests_without(suite, {remove_id}))

        self.assertEqual(1, len(selected))
        self.assertTrue(selected[0].id().endswith(".test_keep"))


if __name__ == "__main__":
    unittest.main()
