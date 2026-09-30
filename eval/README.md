# M2 Evaluation Sources

This directory contains versioned evaluation definitions, not customer data or
credentials. The first set uses `ESCI-lite` for product retrieval and a
deterministic synthetic tenant set for policy, FAQ, factual mock, and access
control scenarios. Amazon data and live web snapshots are out of scope for the
first set.

`build_synthetic_eval.py` creates 60 deterministic cases with concrete product,
policy, factual, freshness, refusal, multi-turn, and authorization scenarios.
`validate_eval.py` is read-only by default: it checks fields, coverage,
non-placeholder semantics, dates, source versions, and SHA-256. Passing
`--review-output` explicitly writes a separate checklist whose 60 rows start
with `review_status=pending`; the checklist never changes the JSONL hash.

The generated set is a development fixture and cannot be used to claim M2
real-service acceptance. `esci-lite.manifest.json` locks the intended public
source metadata only; no ESCI raw data is downloaded or committed in this
stage.

Each case includes a query, expected document IDs, answer points, intent,
information source, tags, authorization context, business date, and source
metadata. A real evaluation run must add human verification, retrieval and
answer metrics, usage, latency, and cost without writing secrets or customer
content.
