# synthetic-m2-aligned-v1

This is an independently authored synthetic source corpus for the local
M2 retrieval baseline. It is not merchant truth, customer content, a
production policy, or real-time business data. All documents are marked
`synthetic`, `provisional`, and `internal-generated`.

Current status: the source corpus and mapping proposal are implemented, but
the aligned review is still `PENDING_REVIEW` because `syn-005` needs evidence
revision. Aligned retrieval remains `NOT_RUN` with null metrics until the
controlled approval gate is satisfied. This corpus does not change the
immutable `synthetic-m2-v1` 60-case baseline.

The `expected_doc_id` field in `manifest.yaml` is a review aid only;
retrieval runs require the separate explicit alignment mapping under
`/var/lib/ecommerce-rag/eval/synthetic-m2-v1-aligned/alignment.json`.
Queries and answer points are never copied into these documents.
