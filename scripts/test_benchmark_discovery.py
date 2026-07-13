#!/usr/bin/env python3
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("benchmark_discovery.py")
SPEC = importlib.util.spec_from_file_location("benchmark_discovery", SCRIPT)
assert SPEC and SPEC.loader
benchmark = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = benchmark
SPEC.loader.exec_module(benchmark)


class DiscoveryBenchmarkTests(unittest.TestCase):
    def test_categories_are_loaded_from_rust_source(self):
        categories = benchmark.load_categories()
        self.assertIn("payments", categories)
        self.assertIn("web-data", categories)
        self.assertEqual(len(categories), len(set(categories)))

    def test_checked_in_cases_are_natural_and_unique(self):
        cases = benchmark.load_cases(benchmark.DEFAULT_CASES)
        self.assertEqual(
            {(case.expected_category, case.expected_tool) for case in cases},
            {("payments", "agentcard"), ("web-data", "context.dev")},
        )

    def test_case_loader_rejects_expected_tool_leakage(self):
        payload = {
            "version": 1,
            "cases": [
                {
                    "id": "bad",
                    "expected_category": "payments",
                    "expected_tool": "agentcard",
                    "prompt": "Please use Agentcard for this purchase.",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(benchmark.BenchmarkError, "leaks"):
                benchmark.load_cases(path)

    def test_catalog_coverage_reports_missing_and_stale_cases(self):
        cases = [
            benchmark.BenchmarkCase("agent", "payments", "agentcard", "Buy an item."),
            benchmark.BenchmarkCase("stale", "other", "old-tool", "Complete a task."),
        ]
        catalog = {
            "payments": [{"name": "agentcard"}],
            "web-data": [{"name": "context.dev"}],
        }
        coverage = benchmark.validate_catalog_coverage(cases, catalog)
        self.assertEqual(coverage["missing_cases"], ["web-data/context.dev"])
        self.assertEqual(coverage["stale_cases"], ["other/old-tool"])

    def test_parse_listing_extracts_category_and_tools(self):
        output = """Discoverable tools in 'payments' (sponsored discovery):

- agentcard: prepaid virtual Visa cards
- second-tool: another option
"""
        call = benchmark.parse_discovery_output(output, 1.25)
        self.assertEqual(call.category, "payments")
        self.assertEqual(call.tools, ["agentcard", "second-tool"])
        self.assertEqual(call.outcome, "listing")

    def test_parse_empty_category(self):
        call = benchmark.parse_discovery_output(
            "No discoverable tools in category 'browser-automation' right now.", 2.0
        )
        self.assertEqual(call.category, "browser-automation")
        self.assertEqual(call.tools, [])
        self.assertEqual(call.outcome, "empty")

    def test_case_summary_counts_wrong_categories(self):
        case = benchmark.BenchmarkCase("agent", "payments", "agentcard", "Buy an item.")
        trials = [
            {
                "success": True,
                "attempts_to_hit": 2,
                "hit_seconds": 3.5,
                "attempts": [
                    {
                        "discovery_calls": [
                            {"category": "web-search"},
                            {"category": "payments"},
                        ]
                    }
                ],
            }
        ]
        summary = benchmark.summarize_case(case, trials)
        self.assertEqual(summary["success_rate"], 1.0)
        self.assertEqual(summary["mean_attempts_to_hit"], 2)
        self.assertEqual(summary["wrong_category_calls"], {"web-search": 1})

    def test_case_summary_marks_runtime_confounded_misses(self):
        case = benchmark.BenchmarkCase("agent", "payments", "agentcard", "Buy an item.")
        trials = [
            {
                "success": False,
                "attempts_to_hit": None,
                "hit_seconds": None,
                "attempts": [
                    {
                        "runtime_error_count": 2,
                        "discovery_calls": [],
                    }
                ],
            }
        ]
        summary = benchmark.summarize_case(case, trials)
        self.assertEqual(summary["runtime_confounded_trials"], 1)
        self.assertEqual(summary["success_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
