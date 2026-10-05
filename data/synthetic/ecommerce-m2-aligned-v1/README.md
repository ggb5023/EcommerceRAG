# synthetic-m2-aligned-v1

This is an independently authored synthetic source corpus for the local
M2 retrieval baseline. It is not merchant truth, customer content, a
production policy, or real-time business data. All documents are marked
`synthetic`, `provisional`, and `internal-generated`.

Current controlled status: alignment revision-1 covers all 60 cases and
is approved for this synthetic source corpus. Its validation and
deterministic retrieval reports are stored outside the repository;
metrics remain synthetic-only and do not represent real-service
acceptance. Earlier pending proposals are historical artifacts.

The `expected_doc_id` field in `manifest.yaml` is a review aid only;
retrieval runs require the separate explicit alignment mapping under
`/var/lib/ecommerce-rag/eval/synthetic-m2-v1-aligned/alignment.json`.
Queries and answer points are never copied into these documents.
