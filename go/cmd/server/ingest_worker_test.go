package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"strings"
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

func artifactBoundDocument() *ragv1.ParsedDocument {
	_, parsed := parsedPackageFixture()
	document := parsed.Documents[0]
	versionID := "v-0123456789abcdef0123"
	records := make([]artifactRecordCanonical, 0, 4)
	for _, artifactType := range []string{"chunks", "parse-report", "parsed", "raw"} {
		dataDigest := sha256.Sum256([]byte(artifactType + "-bytes"))
		digest := hex.EncodeToString(dataDigest[:])
		records = append(records, artifactRecordCanonical{
			ArtifactType: artifactType,
			ContentType:  "application/octet-stream",
			ObjectKey:    "tenant-a/shop-a/" + versionID + "/" + artifactType + "/" + digest,
			SHA256:       digest,
			SizeBytes:    int64(len(artifactType) + 6),
		})
	}
	setBytes, _ := json.Marshal(artifactSetCanonical{Artifacts: records})
	setDigest := sha256.Sum256(setBytes)
	manifestDigest := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	reference := &ragv1.ArtifactBundleReference{
		DocumentVersionId: versionID,
		ManifestObjectKey: "tenant-a/shop-a/" + versionID + "/artifact-manifest/" + manifestDigest,
		ManifestSha256:    manifestDigest,
		ArtifactSetSha256: hex.EncodeToString(setDigest[:]),
	}
	for _, record := range records {
		reference.Artifacts = append(reference.Artifacts, &ragv1.ArtifactRecord{
			ArtifactType: record.ArtifactType,
			ObjectKey:    record.ObjectKey,
			Sha256:       record.SHA256,
			SizeBytes:    record.SizeBytes,
			ContentType:  record.ContentType,
		})
	}
	document.ArtifactBundle = reference
	return document
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

func TestValidateArtifactBundleAcceptsCanonicalReference(t *testing.T) {
	document := artifactBoundDocument()
	binding, err := validateArtifactBundle(document)
	if err != nil {
		t.Fatalf("valid artifact bundle rejected: %v", err)
	}
	if binding.RawObjectKey == "" || binding.ParsedObjectKey == "" || len(binding.ArtifactTypes) != 4 {
		t.Fatalf("artifact binding did not expose required artifacts: %#v", binding)
	}
}

func TestValidateArtifactBundleRejectsManifestHashMismatch(t *testing.T) {
	document := artifactBoundDocument()
	document.ArtifactBundle.ManifestObjectKey = strings.Replace(document.ArtifactBundle.ManifestObjectKey,
		"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", 1)
	if _, err := validateArtifactBundle(document); err == nil {
		t.Fatal("expected manifest object key/hash mismatch to fail")
	}
}

func TestValidateArtifactBundleRejectsArtifactSetHashMismatch(t *testing.T) {
	document := artifactBoundDocument()
	document.ArtifactBundle.ArtifactSetSha256 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	if _, err := validateArtifactBundle(document); err == nil {
		t.Fatal("expected artifact set hash mismatch to fail")
	}
}

func TestValidateArtifactBundleRejectsMissingRequiredArtifact(t *testing.T) {
	document := artifactBoundDocument()
	document.ArtifactBundle.Artifacts = document.ArtifactBundle.Artifacts[:3]
	if _, err := validateArtifactBundle(document); err == nil {
		t.Fatal("expected missing artifact to fail")
	}
}

func TestValidateArtifactBundleRejectsRealServiceAcceptance(t *testing.T) {
	document := artifactBoundDocument()
	document.ArtifactBundle.RealServiceAcceptance = true
	if _, err := validateArtifactBundle(document); err == nil {
		t.Fatal("expected real service acceptance claim to fail")
	}
}

func TestValidateArtifactBundleRejectsUnsafeObjectKey(t *testing.T) {
	document := artifactBoundDocument()
	document.ArtifactBundle.Artifacts[0].ObjectKey = "tenant-a/../shop-a/v-0123456789abcdef0123/chunks/" + document.ArtifactBundle.Artifacts[0].Sha256
	if _, err := validateArtifactBundle(document); err == nil {
		t.Fatal("expected unsafe artifact object key to fail")
	}
}
