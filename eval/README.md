# M2 Evaluation Sources

This directory contains versioned evaluation definitions, not customer data or
credentials. The first 60-case set uses a deterministic synthetic tenant set
for policy, FAQ, factual mock, and access-control scenarios. Public data sources
for a separate product retrieval track are listed in
`public-data-sources.manifest.json`: Amazon Science ESCI and the
`McAuley-Lab/Amazon-Reviews-2023` dataset distributed through Hugging Face.
They are not part of the 60-case business-policy truth set.

The authoritative source and responsibility matrix is in
`.local/dev-docs/docs/数据来源与评测输入.md`. Codex owns synthetic dataset
construction, evaluation review, public-source/license checks, heuristic UX
review, and synthetic tenant/role fixtures for the prototype. These are
engineering outputs; they do not assert real merchant policy, real identity
integration, or production authorization. Real merchant inputs are deferred
until the prototype is runnable and a real trial is planned.

`build_synthetic_eval.py` creates 60 deterministic cases with concrete product,
policy, factual, freshness, refusal, multi-turn, and authorization scenarios.
`validate_eval.py` is read-only by default: it checks fields, coverage,
non-placeholder semantics, dates, source versions, and SHA-256. Passing
`--review-output` explicitly writes a separate checklist whose 60 rows start
with `review_status=pending`; the checklist never changes the JSONL hash. The
default `--review` check verifies that checklist IDs and copied evidence fields
still match the immutable JSONL and that statuses are one of `pending`,
`approved`, `needs_revision`, or `rejected`. Defaults resolve relative to this
script, so the command can be run from any working directory. It reports
status counts, missing notes, duplicate IDs, evidence drift, and either
`PENDING_REVIEW` or `VERIFIED_BASELINE`.

The generated set is a development fixture and cannot be used to claim M2
real-service acceptance. `esci-lite.manifest.json` remains the compatibility
manifest for the ESCI-lite subset. `public-data-sources.manifest.json` records
the broader Amazon/Hugging Face source decisions. No public raw data is
downloaded or committed until its revision, terms/license, and SHA-256 are
recorded. Hugging Face is a distribution channel; its dataset card and the
original source terms control use.

Each case includes a query, expected document IDs, answer points, intent,
information source, tags, authorization context, business date, and source
metadata. A real evaluation run must add human verification, retrieval and
answer metrics, usage, latency, and cost without writing secrets or customer
content.

`run_mock_retrieval.py` produces a read-only deterministic baseline summary,
validates the input SHA-256, and reports retrieval, evidence coverage, refusal,
unauthorized, and multi-turn classification counts. `--include-cases` adds
metadata-only per-case results (case ID, classification, tags, authorization
scope, and expected evidence count); it never emits query or answer text.
By default no synthetic document index is assumed, so retrieval hit rate and
document coverage are explicitly `NOT_RUN`. Use `--fixture-doc-ids` only with
an approved metadata-only JSON document ID fixture to calculate those metrics;
document bodies are never accepted by this tool. Its output is not M2
real-service acceptance. Reports include the input hash, pipeline version, a
stable local run ID, and a fixture hash when supplied; the timestamp is run
metadata and is not used as an evaluation value. `expected_evidence_rate`
means only that cases declare expected documents; it is not runtime evidence
coverage. Source type/version drift is reported as input integrity failure.
Authorization fields are checked for tenant, shop, and role completeness;
unauthorized cases must retain a target evidence/document reference. The
synthetic set models an unauthorized request with a normal operator context,
so the evaluator does not invent a `denied` shop or role.
Answer points must be non-empty strings. Refusal/unauthorized cases require
guidance points, and multi-turn cases require both the `multi_turn` tag and a
target document/routing reference.
With `--include-cases`, the report also includes status counts and a list of
failed case IDs only; it does not include query or answer text.
Every report also contains an `m2_gate` object. It remains `BLOCKED` until
identity/role revocation, tenant/shop mapping, material authorization,
business-date rules, Provider contract, and material version/license/redaction
inputs are separately recorded. The evaluator never infers readiness from
local fixtures. Each gate item includes its accountable owner and the evidence
needed to mark it ready; readiness values remain false until reviewed inputs
are recorded in the project process.
Reports include `report_sha256`, calculated from canonical stable content while
excluding the run timestamp and the hash field itself, so archived reports can
be compared without treating timestamps as evaluation changes.
The default report contains no runtime timestamp and is byte-identical across
repeated runs. A reviewed external-input gate manifest may be supplied with
`--gate-manifest`; it must match the owner/evidence contract for all seven
requirements. Even a `READY` gate does not claim real-service acceptance or
invoke a provider.

The checklist is an evidence record, not an approval mechanism. Only the
business reviewer may change a row to `approved`; engineering must not infer
approval from a passing machine precheck. The synthetic set is a verified
baseline only when all 60 rows are `approved` with no unresolved notes.
