# M2 Evaluation Sources

This directory contains versioned evaluation definitions, not customer data or
credentials. The first set uses `ESCI-lite` for product retrieval and a
deterministic synthetic tenant set for policy, FAQ, factual mock, and access
control scenarios. Amazon data and live web snapshots are out of scope for the
first set.

`build_synthetic_eval.py` creates 60 deterministic cases. It records source,
license state, generated date, SHA-256, `eval_set_version`, pipeline version,
and model version in the output metadata. The generated set is a development
fixture and cannot be used to claim M2 real-service acceptance.

Each case includes a query, expected document IDs, answer points, intent,
information source, tags, authorization context, business date, and source
metadata. A real evaluation run must add human verification, retrieval and
answer metrics, usage, latency, and cost without writing secrets or customer
content.
