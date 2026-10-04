package main

import (
	"context"
	"encoding/hex"
	"os"
	"path/filepath"
	"testing"
	"time"

	ragv1 "github.com/ggb5023/EcommerceRAG/go/internal/pb/rag/v1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func TestSignedScopeRejectsEnlargementAndExpiredScope(t *testing.T) {
	g := &gateway{signingKey: []byte("synthetic-test-key-with-32-bytes!!")}
	original := &ragv1.RequestContext{RequestId: "signed-test", TenantId: "tenant-a", UserId: "user-a", Role: "operator", AllowedShopIds: []string{"east"}, AllowedDocumentIds: []string{"doc-a"}, PermissionRevision: "auth-v1", EnforceDocumentScope: true, ScopeExpiresAt: time.Now().Add(time.Minute).Unix()}
	original.ScopeSignature = hex.EncodeToString(scopeMAC(g.signingKey, original))
	cases := []func(*ragv1.RequestContext){
		func(s *ragv1.RequestContext) { s.AllowedDocumentIds = append(s.AllowedDocumentIds, "doc-secret") },
		func(s *ragv1.RequestContext) { s.AllowedShopIds = append(s.AllowedShopIds, "west") },
		func(s *ragv1.RequestContext) { s.Role = "owner" },
		func(s *ragv1.RequestContext) { s.PermissionRevision = "auth-v2" },
		func(s *ragv1.RequestContext) { s.TenantId = "tenant-b" },
		func(s *ragv1.RequestContext) { s.EnforceDocumentScope = false },
		func(s *ragv1.RequestContext) {
			s.ScopeExpiresAt = time.Now().Add(-time.Second).Unix()
			s.ScopeSignature = hex.EncodeToString(scopeMAC(g.signingKey, s))
		},
	}
	for _, mutate := range cases {
		scope := proto.Clone(original).(*ragv1.RequestContext)
		mutate(scope)
		if status.Code(g.validateScope(context.Background(), scope)) != codes.PermissionDenied {
			t.Fatal("modified or expired scope was accepted")
		}
	}
}

func TestActiveEvidenceVersion(t *testing.T) {
	active := "version-2"
	tests := []struct {
		name          string
		managed       bool
		activeVersion *string
		evidence      string
		want          bool
	}{
		{name: "ordinary source", evidence: "legacy-version", want: true},
		{name: "current published version", managed: true, activeVersion: &active, evidence: "version-2", want: true},
		{name: "stale version", managed: true, activeVersion: &active, evidence: "version-1", want: false},
		{name: "missing version", managed: true, activeVersion: &active, want: false},
		{name: "missing active version", managed: true, evidence: "version-2", want: false},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := activeEvidenceVersion(test.managed, test.activeVersion, test.evidence); got != test.want {
				t.Fatalf("activeEvidenceVersion() = %v, want %v", got, test.want)
			}
		})
	}
}

func TestSigningKeyRequiresRestrictedOwnedRegularFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "key")
	if err := os.WriteFile(path, []byte("synthetic-test-key-with-32-bytes!!"), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("SCOPE_SIGNING_KEY_FILE", path)
	if _, err := loadScopeSigningKey(); err != nil {
		t.Fatal("restricted key rejected")
	}
	if err := os.Chmod(path, 0644); err != nil {
		t.Fatal(err)
	}
	if _, err := loadScopeSigningKey(); err == nil {
		t.Fatal("public-readable key accepted")
	}
	link := filepath.Join(t.TempDir(), "link")
	if err := os.Symlink(path, link); err != nil {
		t.Fatal(err)
	}
	t.Setenv("SCOPE_SIGNING_KEY_FILE", link)
	if _, err := loadScopeSigningKey(); err == nil {
		t.Fatal("symlink key accepted")
	}
}
