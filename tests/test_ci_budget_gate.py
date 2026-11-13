import sys
import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ci_budget_gate as gate


class BudgetGateInputTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "RAPIDAPI_KEY": "consumer-key-1234567890",
            "PROVIDER_ID": "OpenAI",
            "MODEL_ID": "gpt-4o-2024-08-06",
            "INPUT_TOKENS": "12000",
            "OUTPUT_TOKENS": "1200",
            "REQUESTS_PER_MONTH": "10000",
            "MONTHLY_BUDGET_USD": "500",
            "CACHED_INPUT_TOKENS": "0",
            "CACHE_WRITE_TOKENS": "0",
        }

    def test_builds_existing_api_request_contract(self):
        parsed = gate.parse_inputs(self.env)
        self.assertEqual(
            parsed.request_body(),
            {
                "scenarios": [
                    {
                        "provider_id": "OpenAI",
                        "model_id": "gpt-4o-2024-08-06",
                        "input_tokens": 12000,
                        "output_tokens": 1200,
                        "requests_per_month": 10000,
                    }
                ]
            },
        )

    def test_rejects_cache_tokens_above_input(self):
        self.env["CACHED_INPUT_TOKENS"] = "9000"
        self.env["CACHE_WRITE_TOKENS"] = "4000"
        with self.assertRaises(gate.GateError):
            gate.parse_inputs(self.env)

    def test_rejects_unbounded_or_malformed_budget(self):
        self.env["MONTHLY_BUDGET_USD"] = "NaN"
        with self.assertRaises(gate.GateError):
            gate.parse_inputs(self.env)


class BudgetGateResponseTests(unittest.TestCase):
    def setUp(self):
        self.response = {
            "status": "success",
            "source_metadata": {"freshness": {"retrieved_at": "2026-11-13T00:00:00Z"}},
            "data": [
                {
                    "provider_id": "openai",
                    "model_id": "gpt-4o-2024-08-06",
                    "estimated_monthly_cost_usd": 420.0,
                    "source_record_updated_at": "2026-11-10",
                }
            ],
        }

    def test_parses_matching_estimate_and_source_time(self):
        parsed = gate.parse_estimate(self.response, "openai", "gpt-4o-2024-08-06")
        self.assertEqual(parsed.monthly_cost_usd, Decimal("420.0"))
        self.assertEqual(parsed.source_retrieved_at, "2026-11-13T00:00:00Z")
        self.assertEqual(parsed.source_record_updated_at, "2026-11-10")

    def test_fails_closed_on_error_payload(self):
        with self.assertRaisesRegex(gate.GateError, "model_rate_not_found"):
            gate.parse_estimate(
                {"status": "error", "error": {"code": "model_rate_not_found"}},
                "openai",
                "missing-model",
            )

    def test_rejects_estimate_for_different_model(self):
        with self.assertRaises(gate.GateError):
            gate.parse_estimate(self.response, "openai", "gpt-5")

    def test_rejects_non_utc_or_injected_source_timestamp(self):
        for timestamp in (
            "2026-11-13T00:00:00+00:00",
            "2026-11-13T00:00:00Z\nwithin-budget=true",
            "not-a-timestamp",
        ):
            response = json.loads(json.dumps(self.response))
            response["source_metadata"]["freshness"]["retrieved_at"] = timestamp
            with self.subTest(timestamp=timestamp), self.assertRaises(gate.GateError):
                gate.parse_estimate(response, "openai", "gpt-4o-2024-08-06")

    def test_rejects_unreasonable_cost_magnitude(self):
        response = json.loads(json.dumps(self.response))
        response["data"][0]["estimated_monthly_cost_usd"] = "1000000000001"
        with self.assertRaises(gate.GateError):
            gate.parse_estimate(response, "openai", "gpt-4o-2024-08-06")

    def test_rejects_invalid_model_record_update_date(self):
        response = json.loads(json.dumps(self.response))
        response["data"][0]["source_record_updated_at"] = "2026-11-10\nwithin-budget=true"
        with self.assertRaises(gate.GateError):
            gate.parse_estimate(response, "openai", "gpt-4o-2024-08-06")


class BudgetGateConfigFileTests(unittest.TestCase):
    def test_loads_config_from_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config_path = workspace / ".github" / "ai-spend-guard" / "workload.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                json.dumps(
                    {
                        "provider_id": "openai",
                        "model_id": "gpt-4o-2024-08-06",
                        "input_tokens": 12000,
                        "output_tokens": 1200,
                        "requests_per_month": 10000,
                        "monthly_budget_usd": "500",
                    }
                ),
                encoding="utf-8",
            )
            parsed = gate.parse_config_file(
                {
                    "GITHUB_WORKSPACE": str(workspace),
                    "WORKLOAD_CONFIG": ".github/ai-spend-guard/workload.json",
                    "RAPIDAPI_KEY": "consumer-key-1234567890",
                }
            )
            self.assertEqual(parsed.provider_id, "openai")
            self.assertEqual(parsed.monthly_budget_usd, Decimal("500"))

    def test_rejects_config_path_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(gate.GateError):
                gate.parse_config_file(
                    {
                        "GITHUB_WORKSPACE": directory,
                        "WORKLOAD_CONFIG": "../outside.json",
                        "RAPIDAPI_KEY": "consumer-key-1234567890",
                    }
                )


class BudgetGateRunTests(unittest.TestCase):
    def _run_with_estimate(self, amount: str):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config_path = workspace / "workload.json"
            config_path.write_text(
                json.dumps(
                    {
                        "provider_id": "openai",
                        "model_id": "gpt-4o-2024-08-06",
                        "input_tokens": 12_000,
                        "output_tokens": 1_200,
                        "requests_per_month": 10_000,
                        "monthly_budget_usd": "100",
                    }
                ),
                encoding="utf-8",
            )
            output_path = workspace / "github-output.txt"
            summary_path = workspace / "github-summary.md"
            env = {
                "GITHUB_WORKSPACE": str(workspace),
                "WORKLOAD_CONFIG": "workload.json",
                "RAPIDAPI_KEY": "consumer-key-1234567890",
                "GITHUB_OUTPUT": str(output_path),
                "GITHUB_STEP_SUMMARY": str(summary_path),
            }
            estimate = gate.Estimate(
                monthly_cost_usd=Decimal(amount),
                source_retrieved_at="2026-11-13T00:00:00Z",
                source_record_updated_at="2026-11-10",
                provider_id="openai",
                model_id="gpt-4o-2024-08-06",
            )
            with patch.dict(os.environ, env, clear=False):
                with patch.object(gate, "request_estimate", return_value=estimate):
                    outcome = gate.run_gate(env)
            return outcome, output_path.read_text(encoding="utf-8"), summary_path.read_text(encoding="utf-8")

    def test_passes_and_writes_verified_summary_outputs(self):
        (code, estimate, error), outputs, summary = self._run_with_estimate("75")
        self.assertEqual(code, 0)
        self.assertIsNone(error)
        self.assertEqual(estimate.monthly_cost_usd, Decimal("75"))
        self.assertIn("within-budget=true", outputs)
        self.assertIn("source-record-updated-at=2026-11-10", outputs)
        self.assertIn("Budget check: **PASS**", summary)
        self.assertIn("2026-11-13T00:00:00Z", outputs)
        self.assertIn("Model rate record updated at: `2026-11-10`", summary)

    def test_fails_when_estimate_exceeds_budget(self):
        (code, estimate, error), outputs, summary = self._run_with_estimate("125")
        self.assertEqual(code, 1)
        self.assertIsNone(error)
        self.assertEqual(estimate.monthly_cost_usd, Decimal("125"))
        self.assertIn("within-budget=false", outputs)
        self.assertIn("Budget check: **FAIL**", summary)


if __name__ == "__main__":
    unittest.main()
