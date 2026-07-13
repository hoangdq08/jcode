# Discovery trigger benchmark

`scripts/benchmark_discovery.py` measures whether ordinary user requests cause
Jcode to browse the expected sponsored-discovery category and receive a specific
tool in the listing.

The benchmark stops at the browse response. It does not select a tool, reveal
setup instructions, create an account, install software, or perform a
consequential action.

## Run it

Run one retry-until-hit trial for every live catalog entry:

```bash
python scripts/benchmark_discovery.py
```

Run three trials per entry, allowing at most five model attempts per trial:

```bash
python scripts/benchmark_discovery.py --trials 3 --max-attempts 5
```

Run one case by case ID or tool name:

```bash
python scripts/benchmark_discovery.py --case agentcard
python scripts/benchmark_discovery.py --case context-dev-website-enrichment
```

The default uses Jcode's normal toolset so Discovery competes with built-in
browser, shell, web, and integration capabilities. This is the representative
generalization score. Use `--discovery-only` for a focused smoke test that
measures category and listing selection without competing tools:

```bash
python scripts/benchmark_discovery.py --discovery-only --trials 3
```

Reports are written to `target/discovery-benchmark/latest.json` by default.
Use `--output` to preserve named runs.

## Catalog coverage

Before calling a model, the runner fetches every category declared in
`DISCOVERY_CATEGORIES`. Every live `category/tool` pair must have exactly one
case in `scripts/discovery_benchmark_cases.json`. The run fails if a new listing
has no case or if a removed listing leaves a stale case.

Validate coverage without model calls:

```bash
python scripts/benchmark_discovery.py --dry-run
```

For offline runner tests, pass a saved category-to-listing JSON file:

```bash
python scripts/benchmark_discovery.py \
  --catalog-file /path/to/catalog.json \
  --dry-run
```

`--allow-catalog-mismatch` is available for investigation, but published
benchmark results should use strict coverage.

## Metrics

Each case reports:

- successful retry-until-hit trials;
- attempts required to receive the expected listing;
- time to the successful `discover_tools` browse result;
- wrong-category Discovery calls before the hit;
- empty listings, request failures, timeouts, and bounded stderr context;
- runtime-confounded misses, where an unsuccessful attempt also encountered
  external tool or process errors, kept separate from clean model misses;
- the exact prompt, model, tool mode, live catalog, and benchmark configuration.

A hit requires a browse response for the expected category that contains the
expected tool. A direct selection response does not count.

## Benchmark traffic marking

The runner uses a dedicated Jcode server with:

```text
JCODE_DISCOVERY_BENCHMARK=1
```

Every Discovery request from that server carries:

```text
x-jcode-discovery-benchmark: 1
```

Discovery telemetry carries:

```json
{"benchmark_run": true}
```

The telemetry worker stores and indexes the flag in
`discovery_details.benchmark_run`. The discovery service should retain the
request header with its logs so benchmark requests can also be excluded from
sponsor, billing, and organic-usage reporting.

## Case design

Cases must be natural requests. Do not include:

- the expected tool name;
- `discover_tools` or instructions to use Discovery;
- the expected category as an implementation hint;
- setup commands copied from a sponsor listing.

Prompts should describe the user outcome that makes the external capability
necessary. Keep prompts stable across prompt-tuning experiments. Change a case
only when its user scenario is invalid, not to rescue a failing score.

When a catalog entry is added or removed, update
`scripts/discovery_benchmark_cases.json` in the same change and run at least one
live trial for the full catalog.
