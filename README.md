# Vyonetix AI Spend Guard Budget Gate

Estimate monthly text-inference cost in GitHub Actions before merging a workload change. The action sends a model ID and caller-supplied token/request-volume assumptions to the Vyonetix AI Spend Guard API, reports the estimate and source dates, and fails the job when the configured budget is exceeded.

**Release status: preview.** Version `v0.1.0-preview.1` is a technical preview. As of 13 November 2026, Vyonetix has not independently verified the AI Spend Guard estimate route through a distinct RapidAPI consumer. Do not make this a required branch-protection check until that consumer-path test succeeds in your own repository.

## What it does

- Calls `POST /v1/workloads/estimate` once per workflow run.
- Calculates an estimate from the source catalog's listed USD rates and your workload assumptions.
- Writes estimated monthly cost, configured budget, pass/fail, catalog retrieval time, and model-record update date (when supplied) to the GitHub Actions summary.
- Fails closed when the estimate exceeds the budget or the API response cannot be validated.
- Sends no prompts, provider credentials, customer identifiers, or runtime telemetry.

It is a **pre-deployment estimate**, not runtime spend enforcement, an inference gateway, a provider quote, an invoice, or a model-quality comparison.

## Quick start

1. Subscribe to the [Vyonetix AI Spend Guard API on RapidAPI](https://rapidapi.com/vyonetix-solutions-vyonetix-solutions-default/api/vyonetix-ai-spend-guard) and verify the current plan and consumer signup flow.
2. Add a repository Actions secret named `RAPIDAPI_KEY` containing your RapidAPI consumer key. Do not put the key in workflow YAML, repository files, pull-request bodies, or logs.
3. Copy `examples/workload.example.json` to `.github/ai-spend-guard/workload.json`. Select an exact provider/model ID returned by `GET /v1/models/prices` and replace the synthetic workload assumptions with your own.
4. Add this workflow as `.github/workflows/ai-spend-budget.yml`:

```yaml
name: AI workload budget estimate

on:
  workflow_dispatch:
  pull_request:
    branches: [main]

permissions:
  contents: read

jobs:
  estimate:
    # Fork pull requests are skipped. Same-repository pull requests must come
    # from contributors trusted to access the RAPIDAPI_KEY repository secret.
    if: github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - name: Estimate monthly model cost
        uses: VYONETIX-SOLUTIONS/vyonetix-ai-spend-guard-action@v0.1.0-preview.1
        with:
          rapidapi-key: ${{ secrets.RAPIDAPI_KEY }}
          workload-config: .github/ai-spend-guard/workload.json
```

The default workflow uses one API request per run. A forked pull request is skipped, not passed. GitHub does not provide repository secrets to fork-triggered `pull_request` workflows; this action does not use `pull_request_target` to bypass that rule. Same-repository contributors who can change workflow code must be trusted with the configured secret.

## Workload configuration

The sample file shows the accepted fields:

```json
{
  "provider_id": "openai",
  "model_id": "gpt-4o-2024-08-06",
  "input_tokens": 12000,
  "output_tokens": 1200,
  "cached_input_tokens": 0,
  "cache_write_tokens": 0,
  "requests_per_month": 10000,
  "monthly_budget_usd": "500"
}
```

The model ID is illustrative. Query the live catalog first. `cached_input_tokens` and `cache_write_tokens` are portions of `input_tokens`, not additional volume. The configuration file must be inside the checked-out repository and no larger than 16 KB.

## Estimate behavior and limitations

The calculation uses listed text input, output, and explicitly supplied cache rates. It rejects unsupported complex price tiers and missing cache rates rather than silently substituting a different rate. Each result includes the source catalog retrieval timestamp and the selected model record's update date when available.

The estimate excludes provider/account discounts, some tiered or long-context pricing, batch, tools, image/audio/video, network charges, retries, tax, and minimum charges. Catalog entries can be incomplete or stale. Confirm current provider pricing before making a financial commitment.

## API access and privacy

The action uses the RapidAPI consumer gateway and does not contact the Worker origin directly. The API receives provider/model IDs and numeric token assumptions; it does not need prompts or model-provider keys. Do not submit private package names, customer data, or confidential runtime telemetry.

Vyonetix's listing currently shows a Free BASIC plan with a quota of 1,000 requests/month, but signup/card requirements and independent consumer route behavior remain to be verified. Check the listing for current plan terms before installing this in a recurring workflow.

## Source and licences

The action source code is licensed under MIT (see `LICENSE`). The MIT code licence does not grant a licence to the AI Spend Guard service, RapidAPI, or the Models.dev dataset. Models.dev data is separately attributed by the API under its MIT licence; see the API's [third-party notices](https://github.com/VYONETIX-SOLUTIONS/vyonetix-ai-spend-guard/blob/main/THIRD_PARTY_NOTICES.md).

## Links

- [AI Spend Guard API listing](https://rapidapi.com/vyonetix-solutions-vyonetix-solutions-default/api/vyonetix-ai-spend-guard)
- [API source repository](https://github.com/VYONETIX-SOLUTIONS/vyonetix-ai-spend-guard)
- [GitHub Actions secrets guidance](https://docs.github.com/en/actions/security-for-github-actions/security-guides/using-secrets-in-github-actions)
