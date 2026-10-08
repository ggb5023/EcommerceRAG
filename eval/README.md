# M2 Evaluation Sources

This directory contains versioned evaluation definitions, not customer data or
credentials. The first 60-case set uses a deterministic synthetic tenant set
for policy, FAQ, factual mock, and access-control scenarios. Public data sources
for a separate product retrieval track are listed in
`public-data-sources.manifest.json`: Amazon Science ESCI and the
`McAuley-Lab/Amazon-Reviews-2023` dataset distributed through Hugging Face.
They are not part of the 60-case business-policy truth set.

Current aligned synthetic status is revision-1: all 60 source mappings have
been reviewed and approved for this synthetic corpus, validation is `PASS`, and
the deterministic retrieval report is `RUNTIME_PASS`. The reports remain
synthetic-only and carry `real_service_acceptance=false`; this does not change
the immutable 60-case input or claim real merchant, model, or customer-service
quality.

`validate_public_sources.py` validates this manifest locally and without
network access. A source may remain selected while its `revision`, dataset
card/source terms, license, download date, and SHA-256 are pending. The
validator rejects a non-null hash for a `not_downloaded` source, use-scope
overlap, missing restricted-storage declaration, and any
`real_service_acceptance` value other than `false`.

`run_public_data_baseline.py` is a separate public product-data track. It does
not read `synthetic_cases.jsonl`, call a model, or contact the network. Until a
source is downloaded outside the repository and its revision, license, and
original terms are verified, it prints `status=NOT_RUN` with null metrics. For
an approved restricted JSONL/CSV input it performs field mapping, whitespace
cleaning, duplicate removal, and label/rank based Recall@K, MRR, and NDCG
calculations. Reports include `source_id`, `revision`, `license_status`,
`input_sha256`, `pipeline_version`, and `real_service_acceptance=false`.
ESCI is restricted to product retrieval/ranking metrics. Amazon Reviews is
restricted to product metadata/text parsing, mapping, deduplication, and text
retrieval experiments. Neither source is merchant policy, price, inventory,
order, permission, or customer-reply truth.

The public-source manifest, mapping, JSONL, and CSV inputs use strict UTF-8
parsing. Duplicate JSON keys, malformed JSON, non-object JSONL rows, invalid
encoding, duplicate CSV headers, and CSV rows with extra fields fail as
`FAIL input_integrity` before a report is written. A pending source remains
`NOT_RUN` only when its manifest is structurally valid but its revision,
license/terms, or restricted download is not verified.

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
JSONL rows must be objects with unique keys; duplicate keys, non-object rows,
invalid UTF-8, malformed JSON, and malformed metadata fail closed with
input-integrity errors. `--review-output` explicitly writes a separate checklist whose 60 rows start
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

The review checklist is parsed as strict UTF-8 JSON: duplicate object keys,
non-array roots, non-object rows, non-string case IDs, non-string or unknown
review statuses, duplicate case IDs, and evidence drift are rejected. Allowed
review statuses are `pending`, `approved`, `needs_revision`, and `rejected`;
revision or rejection requires a non-empty note. The immutable JSONL is never
modified by review validation.

`run_mock_retrieval.py` produces a read-only deterministic baseline summary,
validates the input SHA-256, and reports retrieval, evidence coverage, refusal,
unauthorized, and multi-turn classification counts. It rejects duplicate JSON
keys, non-object JSONL rows, invalid UTF-8, malformed metadata, duplicate
fixture document IDs, and malformed fixture/gate files before producing a
report. Input failures are reported as `FAIL input_integrity` without a
traceback or partial report. `--include-cases` adds
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

`m2-external-input-gate.json` is the checked-in, non-secret responsibility
record for the seven P7 external-input prerequisites. Every requirement is
currently `ready=false`; it records the accountable role and required evidence
without inventing an identity source, merchant authorization, Provider
credential, or license. Passing the machine check with this manifest reports
`m2_gate.status=BLOCKED` and never changes `real_service_acceptance`.
The manifest uses a strict versioned schema: unknown root fields and unknown
fields inside a requirement are rejected, so credentials, endpoints, or
unreviewed readiness metadata cannot be smuggled into the responsibility
record. Only the declared `ready`, `owner`, and `evidence` fields are accepted
for each requirement.
The optional document-ID fixture measures document presence only: this tool
does not execute Search or Generate, so those values are not query retrieval
recall or runtime answer quality. The current demo corpus uses different IDs
and tenant/shop mappings from the immutable 60-case set. The gated aligned
track is implemented by `build_aligned_fixture.py` and
`run_aligned_retrieval.py`: the builder parses the local
`data/synthetic/ecommerce-demo-v1/manifest.yaml` into actual
document/version/chunk metadata and never copies query or answer text from the
60 cases. It creates a pending `alignment.json`; mappings must use the exact
immutable `expected_doc_ids` as keys and actual corpus document IDs as values.
Only an explicitly reviewed mapping with `status=APPROVED` allows Recall@5,
MRR, and nDCG@5. Missing, drifted, unauthorized, or date-incompatible
mappings remain `NOT_RUN`; an ID alias or derived document is never accepted
as a hit.

`validate_alignment.py` is the read-only precheck for that mapping. It accepts
explicit paths from any working directory, verifies the immutable 60-case
input hash, corpus document and chunk identity, mapping keys and source IDs,
tenant/shop scope, and effective-date fields. It reports
`PASS`, `PENDING_REVIEW`, or `FAIL` and never changes `alignment.json`, approves
a mapping, copies case text into the corpus, or calculates retrieval metrics.
The independent `ecommerce-m2-aligned-v1` corpus contains 37 synthetic,
provisional documents. Its proposal and review history are kept outside the
repository under
`/var/lib/ecommerce-rag/eval/synthetic-m2-v1-aligned-provisional/`. The current
controlled artifacts are `alignment-revision-1.json`, its `PASS` validation
report, and its deterministic retrieval report; earlier pending proposals are
retained only as historical evidence.

To reproduce the approved revision without accidentally selecting the older
pending directory, pass its root explicitly:

```bash
python3 eval/run_aligned_retrieval.py \
  --revision-root /var/lib/ecommerce-rag/eval/synthetic-m2-v1-aligned-provisional \
  --output /var/lib/ecommerce-rag/eval/synthetic-m2-v1-aligned-provisional/retrieval-report-current.json
```

`--revision-root` selects the standard revision-1 cases, metadata, corpus, and
approved alignment filenames as a single set. Explicit `--cases`, `--metadata`,
`--corpus`, or `--alignment` arguments still override those defaults. Without
the option, the runner retains the immutable baseline cases and the historical
pending alignment path, so an unapproved mapping remains `NOT_RUN`.

`alignment_policy.py` is the shared disclosure and effective-date policy
check. Each source chunk must declare `external_allowed`, `internal_only`, or
`unclassified`, plus tenant/shop scope and effective dates. Operator access to
internal or unclassified material, expired material, and not-yet-effective
material is reported separately. `run_aligned_retrieval.py` counts a policy
refusal only when the case explicitly expects an unauthorized or unanswerable
result; ordinary Recall/MRR/nDCG never includes a refused case, and an
unexpected policy block keeps the run `NOT_RUN`.

`review_alignment.py` creates a separate 60-row source-mapping checklist with
`pending`, `approved`, `needs_revision`, or `rejected` states. It verifies the
proposal hash and every copied evidence field, detects tampering, and reports
`REVIEWED` only when all rows are approved. `REVIEWED` is still not an
`alignment.json` approval; the retrieval runner continues to require a
separately controlled `status=APPROVED` mapping.

`build_alignment_revision_proposal.py` creates a read-only decision aid for
unresolved mapping rows. It records immutable case, review, and source hashes,
the current evidence wording, and explicit options to narrow the claim, add
traceable source evidence, or keep the row unresolved. It never edits the
evaluation JSONL, review checklist, source corpus, or `alignment.json`; a
revised evaluation input must receive a new version and SHA-256.

`approve_alignment.py` is the separate explicit approval gate. It requires a
`PENDING_REVIEW` proposal, an exact proposal hash in the review checklist,
zero `pending`/`needs_revision`/`rejected` rows, and unchanged evidence fields.
Only `--approve` writes a new artifact, atomically and without overwriting an
existing file. It refuses any checklist with unresolved rows and never
modifies `alignment.json` or the immutable evaluation input. The historical
provisional 59/1 review is not the current baseline.
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

## Isolated Bailian evaluation

`run_bailian_retrieval.py` is an explicit, metadata-only provider experiment.
Without `--live` it performs input checks and reports `NOT_RUN`; with
`--live --limit N` it reads the approved synthetic aligned corpus, calls the
Alibaba Bailian embedding, rerank, and generation slots, and writes only
case/document IDs, ranks, scores, latency, redacted model/request-ID
fingerprints, usage presence, and structured-output counts. It never writes
vectors, prompts, generated text, customer data, or database/M1 state. The
default limit is three cases to keep the experiment within the reviewed quota.

The prior revision-1 v2 report is outside the repository at
`/var/lib/ecommerce-rag/real-docs/reports/bailian-retrieval-revision-1-current.json`.
The new five-case v3 report is
`/var/lib/ecommerce-rag/real-docs/reports/bailian-retrieval-revision-1-context-v3.json`.
It verifies actual delivery of the reviewed source bodies for all five cases.
Reports retain `real_service_acceptance=false`,
`model_quality_claim=false`, and `answer_quality_status=NOT_RUN`. A
successful transport and retrieval ranking run does not approve model quality,
answer quality, real merchant data, identity, or online customer service.

### Answer-point evidence sidecar

`answer_point_evidence.py` validates a separate, metadata-only review sidecar
that binds each expected answer point to chunks already present in the
approved alignment. It verifies the immutable case, alignment, and corpus
hashes, rejects duplicate keys and cross-alignment chunks, and never modifies
`synthetic_cases.jsonl` or source artifacts. The current approved revision-1
sidecar is outside the repository at
`/var/lib/ecommerce-rag/eval/synthetic-m2-v1-aligned-provisional/answer-point-evidence-review-revision-1.json`;
all 60 points are approved, `unresolved_point_count=0`, and the sidecar status
is `APPROVED`. The older v1 sidecar with 59 of 60 points and `syn-005` marked
`needs_revision` remains historical evidence and is not overwritten.

When supplied with `--answer-evidence`, the v3 evaluator distinguishes approved
source support from approved chunks actually delivered to generation. Every
case ranks only its own tenant/shop, `external_allowed`, and business-date
eligible chunks. Effective end dates are exclusive. Document embeddings may
be batched across cases, but ranking, rerank candidates, and generation
contexts stay within each case's scope. Generation uses only chunks returned
by rerank; an empty rerank result fails without calling generation.

The metadata-only `generation_evidence` records chunk/document/version IDs and
chunk hashes. `approved_support_chunk_coverage` compares each answer point's
sidecar bindings against that actual context. The conservative
`all_bound_chunks_v1` method requires every bound chunk to be delivered;
approved source support alone cannot count as delivered support. Missing
bound chunks produce `approved_evidence_not_delivered`, separately from
`evidence_unsupported`, `generation_empty`, and `generation_rewrite_mismatch`.
Lexical exact/token matches do not assess semantic answer quality.

The prior revision-1 v2 report recorded source approval for all five selected
cases and `generation_rewrite_mismatch` with exact/token match 0/5. It did not
prove that all approved support reached the generation context. The original
sidecar includes frontmatter/headings, and four points bind six chunks each.
Separate `answer-point-evidence-review-revision-1-context-v1.json` and
`answer-point-evidence-review-revision-1-context-v2.json` artifacts retain
lineage to that review. Eight points were re-reviewed against source bodies;
the other 52 bindings remain unchanged. The first five cases have the same
body bindings in both new artifacts. Their v3 live report uses context-v1 and
verifies actual support delivery for 5/5; exact/token matching remains 0/5.
Neither lexical mismatch nor delivered evidence establishes semantic answer
quality. Retain prior reports and immutable inputs unchanged.

### Offline Context Audit

`--audit-context` is mutually exclusive with `--live`. It checks approved
bindings against tenant/shop, disclosure, business dates, and the union of
required chunks across answer points. It never loads Provider configuration,
selects ranking evidence, approves a review, or calls a model. Use `--limit 60`
for the complete input; results distinguish `WITHIN_BUDGET`, `BUDGET_EXCEEDED`,
`INELIGIBLE_REQUIRED_CHUNKS`, and `UNREVIEWED`. A feasible binding does not
prove that retrieval will deliver it.

The context-v2 audit has 53 `WITHIN_BUDGET`, zero `BUDGET_EXCEEDED`, and seven
`INELIGIBLE_REQUIRED_CHUNKS`. Those seven are the five scoped denials and two
revoked/expired cases; their evidence must not enter generation. The audit
retains `generation_context_selection=NOT_RUN` and
`answer_quality_status=NOT_RUN`.

### Bounded context-v2 batch runner

`run_bailian_batch.py` is the next step after the offline audit. It selects
only the 53 `WITHIN_BUDGET` revision-1 cases and keeps all seven
`INELIGIBLE_REQUIRED_CHUNKS` cases out of provider calls. It uses the same
scope, disclosure, business-date, alignment and evidence-sidecar checks as
the single-window evaluator, then groups cases deterministically by `case_id`.

The runner has explicit `--max-requests`, `--max-requests-per-minute`, and
`--max-cost-units` gates. A cost unit is one provider request equivalent, not
a currency estimate. Document embedding calls are counted by document batch;
each eligible case additionally reserves one query embedding, one rerank and
one generation request. The default plan is 53 cases in batches of five, an
estimated 236 request equivalents, and a 240 request/cost ceiling. The
rolling RPM gate waits before making a request; total or cost exhaustion
fails closed.

Without `--live`, the command prints the partition and estimate and makes no
network request or credential read:

```bash
python3 eval/run_bailian_batch.py --plan
```

Live execution is an independently approved, metadata-only experiment. It
stores a restricted checkpoint after each successful batch and stops on the
first provider, budget, or case failure, without calling later cases in that
batch. New failed checkpoints retain only case IDs, known error classifications,
and consumed request counts; exception messages and responses are discarded.
`--resume` requires an exact input
hash and plan identity; a changed case, corpus, alignment, review, or batch
size cannot reuse the checkpoint. Reports and checkpoints contain IDs,
counts, redacted metadata and hashes only, with
`real_service_acceptance=false`, `m1_connected=false`, and
`answer_quality_status=NOT_RUN`. A passing batch run still does not mean
semantic answer quality, real merchant data, or M2 customer-service
acceptance.

For a stopped run, export its verified progress without reading Provider
configuration or making requests:

```bash
python3 eval/run_bailian_batch.py --report-checkpoint \
  --checkpoint /restricted/path/run.checkpoint.json \
  --output /restricted/path/run-failed.json
```

This does not modify the checkpoint. The report distinguishes completed cases,
unmeasured cases, excluded cases, and missing legacy failure details. It retains
the failed status and reports the estimated requests required to rerun the
unfinished batch and finish the plan. Already reserved requests remain spent;
resume can exceed the original window even when the original plan fit it.
The exporter checks input identity and completed payload hashes, uses mode
`0600`, and refuses to overwrite existing evidence. A successful export is
not a successful evaluation or authorization to raise the request limit.

### Answer Composition Diagnostics

Each case also reports `answer-point-composition-v1`, a metadata-only heuristic
that checks whether expected-point token overlap appears only after combining
multiple generated entries, whether a generated entry overlaps multiple
expected points, script relation (`same_script`, `opposite_script`, `mixed`,
or `unclassified`), and counts for qualifier markers, numeric literals, and
unit categories. Reports retain short fingerprints and counts, never generated
or expected text. Fake-provider tests exercise split, combined, Chinese/English,
and qualifier cases, including an existing immutable synthetic answer point.

These signals do not establish entailment, contradiction, correct negation, or
semantic answer quality. A unit-category match does not prove that the unit is
attached to the right value. They do not change the existing exact/token
diagnostics or `answer_quality_status=NOT_RUN`. The isolated v4 diagnostic run
used five approved synthetic cases and context-v2 evidence review; its
metadata-only report is outside the repository at
`/var/lib/ecommerce-rag/real-docs/reports/bailian-retrieval-revision-1-context-diagnostic-v4.json`.

Without `--output` the audit only prints metadata. Explicit output creates a
new report atomically with mode `0600`; both audit and live mode refuse an
existing output path before making Provider calls, preserving historical
evidence. Choose a new report filename for every controlled rerun.

## Isolated Bailian pgvector evaluation

`run_bailian_pgvector.py` is an explicit embedding-to-database experiment.
Without `--live` it reports `NOT_RUN` and makes no network or database request.
With `--live`, it requires `/etc/ecommerce-rag/m1-review.env` (or an explicit
`--db-env`) to name an `ecr_m1_test_*` database and the non-superuser
`rag_app`. It embeds only the approved synthetic aligned corpus, inserts
vectors into PostgreSQL TEMP tables, applies tenant/shop, `external_allowed`,
and business-date filters, emits metadata-only top-5 results, and rolls back
the transaction. The report never stores vectors, document text, prompts,
credentials, or customer data. This proves an isolated provider/database
contract only; it is not persistent vector quality, M1 integration, or real
M2 service acceptance.

The current report is outside the repository at
`/var/lib/ecommerce-rag/real-docs/reports/bailian-pgvector-current.json` and
has `real_service_acceptance=false`.

Both isolated Provider evaluators now require an explicit `document_version_id`
for every corpus document and chunk. They reject duplicate chunk IDs, parent
document/version drift, and tenant/shop mismatches before sending embeddings.
The retrieval and pgvector reports retain document/version/chunk IDs in their
metadata-only result records. This binds ranking evidence to an immutable
document version; it does not publish the version, persist production vectors,
or make the experiment M2 real-service acceptance.
