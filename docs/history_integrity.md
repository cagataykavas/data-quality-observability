# Quality-history integrity

The history store appends each canonical quality report to a per-dataset SHA-256 chain.
Every run commits:

- a digest of the exact canonical report bytes;
- the previous chain digest for the same dataset; and
- a domain-separated chain digest binding dataset, check time, report, and predecessor.

Appending uses `BEGIN IMMEDIATE`, so separate CLI processes serialize the read of the
current head and the new insert. History reads replay the complete dataset chain before
returning a bounded recent window. Mutated report bytes, rewritten chain fields, and
deleted interior runs therefore fail closed with CLI exit code `2`.

Existing databases are upgraded in place. The migration adds the integrity columns and
chains legacy rows in their original ID order; it does not discard prior reports.

## Trust boundary

This is corruption and accidental-tampering evidence, not publisher authentication. A
database administrator able to rewrite the entire history can recompute every digest,
and deleting the newest suffix cannot be detected without an external checkpoint.
Production deployments should periodically sign or externally retain the latest chain
head and should use a managed database transaction with equivalent serialization.
