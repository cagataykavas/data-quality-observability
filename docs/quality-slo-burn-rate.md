# Multi-window data-quality SLO burn gate

A single failed batch is often too noisy for an incident, while a sustained stream of smaller contract failures can exhaust a quality budget without any one run looking exceptional. This gate applies multi-window SLO burn-rate logic to fixed-period counts of data-quality evaluations.

It is independent of the history-integrity, temporal-coverage and metric-cardinality changes currently under review.

## Evidence contract

The audit consumes aggregate counts rather than source rows or complete quality reports:

```json
{
  "schema_version": 1,
  "dataset": "transactions",
  "period_seconds": 300,
  "as_of": "2026-11-13T12:00:00Z",
  "buckets": [
    {
      "end": "2026-11-13T11:55:00Z",
      "eligible_evaluations": 100,
      "failed_evaluations": 0
    },
    {
      "end": "2026-11-13T12:00:00Z",
      "eligible_evaluations": 100,
      "failed_evaluations": 5
    }
  ]
}
```

An evaluation unit should be defined consistently before producing evidence. For example, it may represent one required contract check or one governed dataset run. Mixing row failures, check failures and batch failures in the same series makes the objective meaningless.

Buckets must be UTC, ordered, contiguous, and exactly `period_seconds` apart. A bucket may have no eligible evaluations, but each alert profile requires enough evaluations across its long window.

## Default policy

The default objective is 99.5%, leaving a 0.5% error budget. Burn rate is:

```text
observed failure rate / (1 - objective)
```

Two profiles are evaluated:

| Profile | Short window | Long window | Threshold | Intended response |
|---|---:|---:|---:|---|
| `fast` | 1 bucket | 12 buckets | 14.4× | page |
| `slow` | 6 buckets | 72 buckets | 6× | ticket |

A profile triggers only when **both** its short- and long-window burn rates exceed the threshold. This prevents a short-window blip from alerting when the broader window remains healthy, while still escalating a sufficiently large incident.

The audit also fails closed when:

- evidence is older than one bucket period;
- a profile lacks its complete long window;
- a long window has fewer than 100 eligible evaluations;
- counts are negative, boolean, inconsistent or unbounded;
- timestamps are non-UTC, duplicated, out of order or discontinuous.

The report contains only aggregate windows, findings, and canonical SHA-256 evidence/decision digests. It does not repeat every input bucket. Output is atomically replaced.

## Usage

```bash
python -m dq.slo_burn_rate evidence/quality-slo.json \
  --output artifacts/quality-slo-report.json
```

Override the objective and evidence minimum explicitly:

```bash
python -m dq.slo_burn_rate evidence/quality-slo.json \
  --objective 0.999 \
  --minimum-evaluations 500
```

Exit codes:

- `0`: evidence is valid and no gate fired;
- `1`: evidence is valid but freshness, sufficiency, or burn policy rejected it;
- `2`: input or policy is malformed.

The loader rejects symbolic links, files larger than 2 MiB, duplicate JSON keys, non-finite numbers and more than 10,000 buckets.

## Limitations

- Default objectives and burn thresholds are engineering examples, not universal incident policy.
- Aggregate failed-check counts treat each evaluation as equally important. Critical checks may need a separate SLO or weighted evidence producer.
- This gate observes contract outcomes; it does not validate the correctness of the contract itself.
- Contiguous buckets do not prove the upstream scheduler produced every expected dataset run. Volume/completeness monitoring remains separate.
- SHA-256 binds evidence but does not authenticate its producer. Signed evidence and immutable retention are deployment responsibilities.

The next increment should adapt persisted quality history into these bounded buckets and bind the accepted burn report to the downstream pipeline-release decision.
