#!/usr/bin/env python3
"""GitHub Actions budget gate for Vyonetix AI Spend Guard."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_URL = "https://vyonetix-ai-spend-guard.p.rapidapi.com/v1/workloads/estimate"
API_HOST = "vyonetix-ai-spend-guard.p.rapidapi.com"
MAX_REQUESTS_PER_MONTH = 10_000_000
MAX_TOKENS_PER_REQUEST = 5_000_000
DECIMAL_PATTERN = re.compile(r"^(?:0|[1-9]\d*)(?:\.\d{1,8})?$")
UTC_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
SOURCE_RECORD_DATE_PATTERN = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z)$"
)
MAX_MONTHLY_COST_USD = Decimal("1000000000000")


class GateError(Exception):
    """A safe, user-facing configuration or API error."""


@dataclass(frozen=True)
class GateInputs:
    rapidapi_key: str
    provider_id: str
    model_id: str
    input_tokens: int
    output_tokens: int
    requests_per_month: int
    monthly_budget_usd: Decimal
    cached_input_tokens: int
    cache_write_tokens: int

    def request_body(self) -> dict[str, Any]:
        scenario: dict[str, Any] = {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "requests_per_month": self.requests_per_month,
        }
        if self.cached_input_tokens:
            scenario["cached_input_tokens"] = self.cached_input_tokens
        if self.cache_write_tokens:
            scenario["cache_write_tokens"] = self.cache_write_tokens
        return {"scenarios": [scenario]}


@dataclass(frozen=True)
class Estimate:
    monthly_cost_usd: Decimal
    source_retrieved_at: str
    source_record_updated_at: str | None
    provider_id: str
    model_id: str


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise GateError(f"Required input {name} is missing.")
    return value


def _bounded_int(env: Mapping[str, str], name: str, maximum: int) -> int:
    raw = _required(env, name)
    if not raw.isascii() or not raw.isdigit():
        raise GateError(f"Input {name} must be a whole number.")
    value = int(raw)
    if value > maximum:
        raise GateError(f"Input {name} exceeds the supported maximum ({maximum}).")
    return value


def _optional_bounded_int(env: Mapping[str, str], name: str, maximum: int) -> int:
    raw = env.get(name, "0").strip() or "0"
    if not raw.isascii() or not raw.isdigit():
        raise GateError(f"Input {name} must be a whole number.")
    value = int(raw)
    if value > maximum:
        raise GateError(f"Input {name} exceeds the supported maximum ({maximum}).")
    return value


def _budget(env: Mapping[str, str]) -> Decimal:
    raw = _required(env, "MONTHLY_BUDGET_USD")
    if not DECIMAL_PATTERN.fullmatch(raw):
        raise GateError("Input MONTHLY_BUDGET_USD must be a non-negative USD amount with up to 8 decimal places.")
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise GateError("Input MONTHLY_BUDGET_USD is invalid.") from exc


def parse_inputs(env: Mapping[str, str]) -> GateInputs:
    rapidapi_key = _required(env, "RAPIDAPI_KEY")
    if len(rapidapi_key) < 16:
        raise GateError("Input RAPIDAPI_KEY is too short to be a valid consumer key.")

    provider_id = _required(env, "PROVIDER_ID")
    if len(provider_id) > 64 or not re.fullmatch(r"[A-Za-z0-9_-]+", provider_id):
        raise GateError("Input PROVIDER_ID must be an exact provider identifier from the model catalog.")

    model_id = _required(env, "MODEL_ID")
    if len(model_id) > 180 or any(ord(char) < 32 for char in model_id):
        raise GateError("Input MODEL_ID must be an exact model identifier from the model catalog.")

    input_tokens = _bounded_int(env, "INPUT_TOKENS", MAX_TOKENS_PER_REQUEST)
    output_tokens = _bounded_int(env, "OUTPUT_TOKENS", MAX_TOKENS_PER_REQUEST)
    requests_per_month = _bounded_int(env, "REQUESTS_PER_MONTH", MAX_REQUESTS_PER_MONTH)
    cached_input_tokens = _optional_bounded_int(env, "CACHED_INPUT_TOKENS", MAX_TOKENS_PER_REQUEST)
    cache_write_tokens = _optional_bounded_int(env, "CACHE_WRITE_TOKENS", MAX_TOKENS_PER_REQUEST)
    if cached_input_tokens + cache_write_tokens > input_tokens:
        raise GateError("Cached-read plus cache-write tokens cannot exceed INPUT_TOKENS.")

    return GateInputs(
        rapidapi_key=rapidapi_key,
        provider_id=provider_id,
        model_id=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        requests_per_month=requests_per_month,
        monthly_budget_usd=_budget(env),
        cached_input_tokens=cached_input_tokens,
        cache_write_tokens=cache_write_tokens,
    )


def parse_config_file(env: Mapping[str, str]) -> GateInputs:
    workspace_raw = _required(env, "GITHUB_WORKSPACE")
    config_raw = _required(env, "WORKLOAD_CONFIG")
    workspace = Path(workspace_raw).resolve()
    config_path = (workspace / config_raw).resolve()
    if config_path == workspace or workspace not in config_path.parents:
        raise GateError("WORKLOAD_CONFIG must point to a file inside the checked-out repository.")
    if not config_path.is_file():
        raise GateError("The workload configuration file does not exist in the checked-out repository.")
    if config_path.stat().st_size > 16_384:
        raise GateError("The workload configuration file exceeds the 16 KB limit.")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateError("The workload configuration file is not valid UTF-8 JSON.") from exc
    required = {
        "provider_id",
        "model_id",
        "input_tokens",
        "output_tokens",
        "requests_per_month",
        "monthly_budget_usd",
    }
    allowed = required | {"cached_input_tokens", "cache_write_tokens"}
    if not isinstance(config, dict) or not required.issubset(config) or set(config) - allowed:
        raise GateError("Workload config must contain the documented fields and no unknown keys.")

    converted: dict[str, str] = {"RAPIDAPI_KEY": _required(env, "RAPIDAPI_KEY")}
    for config_key, env_key in (
        ("provider_id", "PROVIDER_ID"),
        ("model_id", "MODEL_ID"),
        ("input_tokens", "INPUT_TOKENS"),
        ("output_tokens", "OUTPUT_TOKENS"),
        ("requests_per_month", "REQUESTS_PER_MONTH"),
        ("monthly_budget_usd", "MONTHLY_BUDGET_USD"),
    ):
        value = config[config_key]
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise GateError(f"Workload config field {config_key} has the wrong type.")
        converted[env_key] = str(value)
    for config_key, env_key in (
        ("cached_input_tokens", "CACHED_INPUT_TOKENS"),
        ("cache_write_tokens", "CACHE_WRITE_TOKENS"),
    ):
        value = config.get(config_key, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise GateError(f"Workload config field {config_key} has the wrong type.")
        converted[env_key] = str(value)
    return parse_inputs(converted)


def parse_estimate(payload: Any, provider_id: str, model_id: str) -> Estimate:
    if not isinstance(payload, dict) or payload.get("status") != "success":
        code = "invalid_response"
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            candidate = payload["error"].get("code")
            if isinstance(candidate, str) and re.fullmatch(r"[a-z0-9_]{1,64}", candidate):
                code = candidate
        raise GateError(f"AI Spend Guard returned an API error ({code}).")

    rows = payload.get("data")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise GateError("AI Spend Guard returned an unexpected estimate response.")
    row = rows[0]
    amount = row.get("estimated_monthly_cost_usd")
    if isinstance(amount, bool) or not isinstance(amount, (int, float, str)):
        raise GateError("The API response did not contain a monthly cost estimate.")
    try:
        monthly_cost = Decimal(str(amount))
    except InvalidOperation as exc:
        raise GateError("The API returned an invalid monthly cost estimate.") from exc
    if not monthly_cost.is_finite() or monthly_cost < 0 or monthly_cost > MAX_MONTHLY_COST_USD:
        raise GateError("The API returned an invalid monthly cost estimate.")

    source = payload.get("source_metadata")
    freshness = source.get("freshness") if isinstance(source, dict) else None
    retrieved_at = freshness.get("retrieved_at") if isinstance(freshness, dict) else None
    if not isinstance(retrieved_at, str) or not UTC_TIMESTAMP_PATTERN.fullmatch(retrieved_at):
        raise GateError("The API response did not include a valid source retrieval timestamp.")
    try:
        datetime.fromisoformat(retrieved_at[:-1] + "+00:00")
    except ValueError as exc:
        raise GateError("The API response did not include a valid source retrieval timestamp.") from exc

    record_updated_at = row.get("source_record_updated_at")
    if record_updated_at is not None:
        if not isinstance(record_updated_at, str) or not SOURCE_RECORD_DATE_PATTERN.fullmatch(record_updated_at):
            raise GateError("The API response included an invalid model-record update date.")
        try:
            if "T" in record_updated_at:
                datetime.fromisoformat(record_updated_at[:-1] + "+00:00")
            else:
                date.fromisoformat(record_updated_at)
        except ValueError as exc:
            raise GateError("The API response included an invalid model-record update date.") from exc

    returned_provider = row.get("provider_id")
    returned_model = row.get("model_id")
    if returned_provider != provider_id.lower() or returned_model != model_id:
        raise GateError("The API response model did not match the requested model.")

    return Estimate(
        monthly_cost_usd=monthly_cost,
        source_retrieved_at=retrieved_at,
        source_record_updated_at=record_updated_at,
        provider_id=returned_provider,
        model_id=returned_model,
    )


def request_estimate(inputs: GateInputs) -> Estimate:
    body = json.dumps(inputs.request_body(), separators=(",", ":")).encode("utf-8")
    request = Request(
        API_URL,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-RapidAPI-Key": inputs.rapidapi_key,
            "X-RapidAPI-Host": API_HOST,
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise GateError("AI Spend Guard returned an oversized response.")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise GateError("AI Spend Guard returned invalid JSON.") from exc
    except HTTPError as exc:
        raise GateError(f"AI Spend Guard request failed with HTTP {exc.code}.") from None
    except URLError as exc:
        reason = getattr(exc, "reason", None)
        reason_name = type(reason).__name__ if reason is not None else "network error"
        raise GateError(f"Could not reach AI Spend Guard ({reason_name}).") from None
    except TimeoutError:
        raise GateError("AI Spend Guard request timed out after 20 seconds.") from None

    return parse_estimate(payload, inputs.provider_id.lower(), inputs.model_id)


def _append_github_output(key: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(f"{key}={value}\n")


def _append_summary(text: str) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
    else:
        print(text, end="")


def _display_amount(amount: Decimal) -> str:
    return f"{amount.quantize(Decimal('0.00000001')):f}"


def run_gate(env: Mapping[str, str]) -> tuple[int, Estimate | None, GateError | None]:
    try:
        inputs = parse_config_file(env)
        estimate = request_estimate(inputs)
    except GateError as exc:
        return 2, None, exc

    within_budget = estimate.monthly_cost_usd <= inputs.monthly_budget_usd
    _append_github_output("estimated-monthly-cost-usd", _display_amount(estimate.monthly_cost_usd))
    _append_github_output("within-budget", str(within_budget).lower())
    _append_github_output("source-retrieved-at", estimate.source_retrieved_at)
    _append_github_output("source-record-updated-at", estimate.source_record_updated_at or "not-provided")
    _append_summary(
        "## Vyonetix AI Spend Guard budget estimate\n\n"
        f"- Provider/model: `{estimate.provider_id}/{estimate.model_id}`\n"
        f"- Estimated monthly cost: **US${_display_amount(estimate.monthly_cost_usd)}**\n"
        f"- Monthly budget: **US${_display_amount(inputs.monthly_budget_usd)}**\n"
        f"- Budget check: **{'PASS' if within_budget else 'FAIL'}**\n"
        f"- Models.dev catalog retrieved at: `{estimate.source_retrieved_at}`\n\n"
        f"- Model rate record updated at: `{estimate.source_record_updated_at or 'not provided by source'}`\n\n"
        "This is a catalog-rate estimate based on the supplied average tokens and request volume. It is not an invoice, "
        "provider quote, runtime spend control, or model-quality comparison.\n"
    )
    if within_budget:
        print(f"AI Spend Guard: estimate is within the monthly budget (US${_display_amount(estimate.monthly_cost_usd)}).")
        return 0, estimate, None
    print(f"AI Spend Guard: estimate exceeds the monthly budget (US${_display_amount(estimate.monthly_cost_usd)}).")
    return 1, estimate, None


def main() -> int:
    exit_code, _, error = run_gate(os.environ)
    if error is not None:
        print(f"AI Spend Guard budget gate error: {error}", file=sys.stderr)
        return exit_code
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
