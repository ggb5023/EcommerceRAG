package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"testing"

	ragv1 "github.com/ggb5023/EcommerceRAG/go/internal/pb/rag/v1"
)

func parsedPackageFixture() (*ingestClaim, *ragv1.ParsePackageResponse) {
	content := []byte("hello")
	sourceDigest := sha256.Sum256(content)
	sourceHash := hex.EncodeToString(sourceDigest[:])
	claim := &ingestClaim{
		TenantID: 7, ShopID: "shop-1", ManifestSHA: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
		Files: []ingestClaimFile{{ItemID: 1, DocumentID: "doc-1", Path: "docs/guide.md", SourceHash: sourceHash, Content: content}},
	}
	parsed := &ragv1.ParsePackageResponse{Documents: []*ragv1.ParsedDocument{{
		DocumentId: "doc-1", Title: "Guide", Format: "markdown", Path: "docs/guide.md", SourceHash: sourceHash,
		DisclosureClass: "internal_only", ExternalAllowed: false,
		Chunks: []*ragv1.ParsedChunk{{
			ChunkIndex: 0, SectionSeq: 1, SectionChunkIndex: 0, CharStart: 0, CharEnd: 5,
			Content: "hello", TokenCount: 1, ContentType: "text", SplitReason: "element_boundary",
			ChunkHash:    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			MetadataJson: `{"tenant_id":"7","shop_id":"shop-1","source_hash":"` + sourceHash + `","disclosure_class":"internal_only","effective_from":null,"effective_to":null}`,
		}},
	}}}
	parsed.ManifestSha256 = claim.ManifestSHA
	parsed.PipelineVersion = "pipeline-v1"
	datasetHash, err := parsedDatasetSHA256(parsed.ManifestSha256, parsed.Documents)
	if err != nil {
		panic(err)
	}
	parsed.DatasetSha256 = datasetHash
	return claim, parsed
}

func TestIngestWorkerTestHoldDisabledOutsideTest(t *testing.T) {
	t.Setenv("APP_ENV", "development")
	t.Setenv("INGEST_WORKER_TEST_HOLD_MS", "invalid")
	if err := ingestWorkerTestHold(context.Background()); err != nil {
		t.Fatalf("test hold must be ignored outside APP_ENV=test: %v", err)
	}
}

func TestIngestWorkerTestHoldRejectsInvalidValue(t *testing.T) {
	t.Setenv("APP_ENV", "test")
	t.Setenv("INGEST_WORKER_TEST_HOLD_MS", "0")
	if err := ingestWorkerTestHold(context.Background()); err == nil {
		t.Fatal("expected invalid test hold to be rejected")
	}
}

func TestIngestWorkerTestHoldCanBeCancelled(t *testing.T) {
	t.Setenv("APP_ENV", "test")
	t.Setenv("INGEST_WORKER_TEST_HOLD_MS", "1000")
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := ingestWorkerTestHold(ctx); err != context.Canceled {
		t.Fatalf("expected context cancellation, got %v", err)
	}
}

func TestValidateParsedPackageAcceptsBoundAndNonEmptyChunks(t *testing.T) {
	claim, parsed := parsedPackageFixture()
	if err := validateParsedPackage(claim, parsed); err != nil {
		t.Fatalf("valid parser response rejected: %v", err)
	}
}

func TestValidateParsedPackageRejectsEmptyChunks(t *testing.T) {
	claim, parsed := parsedPackageFixture()
	parsed.Documents[0].Chunks = nil
	qualityErr, ok := validateParsedPackage(claim, parsed).(*ingestParseQualityError)
	if !ok || qualityErr.code != "parse_empty_chunks" {
		t.Fatalf("expected parse_empty_chunks, got %T %v", validateParsedPackage(claim, parsed), validateParsedPackage(claim, parsed))
	}
}

func TestValidateParsedPackageRejectsEmptyContentAndInvalidMetadata(t *testing.T) {
	claim, parsed := parsedPackageFixture()
	parsed.Documents[0].Chunks[0].Content = "   "
	qualityErr, ok := validateParsedPackage(claim, parsed).(*ingestParseQualityError)
	if !ok || qualityErr.code != "parse_empty_chunk" {
		t.Fatalf("expected parse_empty_chunk, got %T %v", validateParsedPackage(claim, parsed), validateParsedPackage(claim, parsed))
	}

	claim, parsed = parsedPackageFixture()
	parsed.Documents[0].Chunks[0].MetadataJson = "not-json"
	qualityErr, ok = validateParsedPackage(claim, parsed).(*ingestParseQualityError)
	if !ok || qualityErr.code != "parse_chunk_metadata_invalid" {
		t.Fatalf("expected parse_chunk_metadata_invalid, got %T %v", validateParsedPackage(claim, parsed), validateParsedPackage(claim, parsed))
	}
}

func TestValidateParsedPackageRejectsMalformedIdentityAndBindings(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*ingestClaim, *ragv1.ParsePackageResponse)
		code   string
	}{
		{"nil claim", func(claim *ingestClaim, parsed *ragv1.ParsePackageResponse) {}, "parse_claim_invalid"},
		{"manifest mismatch", func(claim *ingestClaim, parsed *ragv1.ParsePackageResponse) {
			parsed.ManifestSha256 = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
		}, "parse_manifest_binding_invalid"},
		{"dataset mismatch", func(claim *ingestClaim, parsed *ragv1.ParsePackageResponse) {
			parsed.DatasetSha256 = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
		}, "parse_dataset_hash_mismatch"},
		{"source hash mismatch", func(claim *ingestClaim, parsed *ragv1.ParsePackageResponse) {
			claim.Files[0].Content = []byte("changed")
		}, "parse_source_hash_mismatch"},
		{"invalid source range", func(claim *ingestClaim, parsed *ragv1.ParsePackageResponse) {
			parsed.Documents[0].Chunks[0].CharEnd = 4
		}, "parse_chunk_source_invalid"},
		{"invalid chunk hash", func(claim *ingestClaim, parsed *ragv1.ParsePackageResponse) {
			parsed.Documents[0].Chunks[0].ChunkHash = "bad"
		}, "parse_chunk_hash_invalid"},
		{"scope mismatch", func(claim *ingestClaim, parsed *ragv1.ParsePackageResponse) {
			parsed.Documents[0].Chunks[0].MetadataJson = `{"tenant_id":"8","shop_id":"shop-1","source_hash":"` + parsed.Documents[0].SourceHash + `","disclosure_class":"internal_only","effective_from":null,"effective_to":null}`
		}, "parse_chunk_metadata_binding_invalid"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			claim, parsed := parsedPackageFixture()
			if test.name == "nil claim" {
				claim = nil
			} else {
				test.mutate(claim, parsed)
			}
			qualityErr, ok := validateParsedPackage(claim, parsed).(*ingestParseQualityError)
			if !ok || qualityErr.code != test.code {
				t.Fatalf("expected %s, got %T %v", test.code, validateParsedPackage(claim, parsed), validateParsedPackage(claim, parsed))
			}
		})
	}
}

func TestValidateParsedPackageRejectsDuplicateAndInvalidOrdering(t *testing.T) {
	claim, parsed := parsedPackageFixture()
	original := parsed.Documents[0].Chunks[0]
	duplicate := &ragv1.ParsedChunk{
		ChunkIndex: 1, SectionSeq: original.SectionSeq, SectionChunkIndex: 1,
		CharStart: original.CharStart, CharEnd: original.CharEnd, Content: original.Content,
		TokenCount: original.TokenCount, ContentType: original.ContentType, SplitReason: original.SplitReason,
		ChunkHash: original.ChunkHash, MetadataJson: original.MetadataJson,
	}
	parsed.Documents[0].Chunks = append(parsed.Documents[0].Chunks, duplicate)
	qualityErr, ok := validateParsedPackage(claim, parsed).(*ingestParseQualityError)
	if !ok || qualityErr.code != "parse_duplicate_chunk" {
		t.Fatalf("expected parse_duplicate_chunk, got %T %v", validateParsedPackage(claim, parsed), validateParsedPackage(claim, parsed))
	}

	claim, parsed = parsedPackageFixture()
	metadata, _ := json.Marshal(map[string]any{
		"tenant_id": "7", "shop_id": "shop-1", "source_hash": parsed.Documents[0].SourceHash,
		"disclosure_class": "internal_only", "effective_from": nil, "effective_to": nil,
	})
	parsed.Documents[0].Chunks[0].MetadataJson = string(metadata)
	parsed.Documents[0].Chunks[0].SectionSeq = 2
	qualityErr, ok = validateParsedPackage(claim, parsed).(*ingestParseQualityError)
	if !ok || qualityErr.code != "parse_chunk_position_invalid" {
		t.Fatalf("expected parse_chunk_position_invalid, got %T %v", validateParsedPackage(claim, parsed), validateParsedPackage(claim, parsed))
	}
}
