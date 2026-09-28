# Metric label-cardinality admission gate

Prometheus and OpenTelemetry series are identified by a metric name plus the
complete label set. A single unbounded label such as `request_id`, `user_id`, or
`trace_id` can create a new series per request, increasing collector memory,
storage cost, query latency, and incident risk.

`dq.metric_cardinality` validates a bounded active-series snapshot against a
server-owned policy before a new metric schema or collector configuration is
promoted. The policy declares every permitted metric, its allowed and required
labels, a per-metric series budget, and distinct-value budgets for selected
labels. Global hard limits bound the snapshot, labels per series, label-value
bytes, age, clock skew, and report size.

The gate rejects malformed, stale, future-dated, duplicate, or over-budget
evidence. Valid policy breaches return stable findings for undeclared metrics,
unexpected or missing labels, forbidden identity labels, excessive series, and
excessive label-value cardinality. Reports expose only metric and label names
plus aggregate counts; collector identity and raw label values remain inside a
deterministic SHA-256 evidence binding.

```bash
python -m dq.metric_cardinality snapshot.json \
  --policy cardinality-policy.json \
  --output cardinality-report.json
```

Exit `0` accepts the snapshot, exit `2` reports a valid policy rejection, and
exit `3` identifies malformed or stale evidence.

## Trust boundary and limitations

The gate trusts the collector to provide a complete active-series snapshot and
does not query a live Prometheus TSDB. Cardinality in a bounded observation
window can understate rare or seasonal values. Hashing evidence is not producer
authentication. Metric and label names are intentionally visible for incident
triage, so names must not contain secrets. Production rollout should collect
snapshots with least-privilege credentials, sign them, calibrate budgets from
real retention windows, and retain accepted report digests with the exact
collector and service revision.
