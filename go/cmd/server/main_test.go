package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func TestSessionIgnoresClientAuthorizationHeaders(t *testing.T) {
	g := &gateway{session: mockSession{tenant: "m1-tenant", user: "m1-user", shop: "shop-demo", role: "operator"}}
	req := httptest.NewRequest(http.MethodGet, "/v1/session", nil)
	req.Header.Set("X-Tenant-ID", "attacker-tenant")
	req.Header.Set("X-Shop-ID", "attacker-shop")
	response := httptest.NewRecorder()
	g.routes().ServeHTTP(response, req)
	if response.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", response.Code)
	}
	body := response.Body.String()
	for _, expected := range []string{`"tenantId":"m1-tenant"`, `"userId":"m1-user"`, `"shopId":"shop-demo"`, `"is_mock":true`} {
		if !strings.Contains(body, expected) {
			t.Errorf("session response missing %s: %s", expected, body)
		}
	}
	if strings.Contains(body, "attacker-") {
		t.Fatalf("client authorization headers were reflected: %s", body)
	}
}

func TestTurnStatusTerminalSet(t *testing.T) {
	for _, state := range []string{"DONE", "FAILED", "CANCELLED"} {
		if !terminal(state) {
			t.Errorf("%s should be terminal", state)
		}
	}
	for _, state := range []string{"PENDING", "EXECUTING", "CANCEL_REQUESTED"} {
		if terminal(state) {
			t.Errorf("%s should not be terminal", state)
		}
	}
}

func TestGRPCFailureProducesStructuredM1Codes(t *testing.T) {
	cases := []struct {
		code codes.Code
		want string
	}{
		{codes.PermissionDenied, "permission_denied"},
		{codes.ResourceExhausted, "rate_limited"},
		{codes.Unavailable, "rag_unavailable"},
	}
	for _, tc := range cases {
		_, got := grpcFailure(context.Background(), status.Error(tc.code, "details"))
		if got != tc.want {
			t.Errorf("grpcFailure(%s) = %q, want %q", tc.code, got, tc.want)
		}
	}
}

func TestAuthorizationFailureCodes(t *testing.T) {
	cases := []struct {
		err  error
		want string
	}{
		{status.Error(codes.PermissionDenied, "identity_revoked"), "identity_revoked"},
		{status.Error(codes.PermissionDenied, "permission_changed"), "permission_changed"},
		{status.Error(codes.PermissionDenied, "shop_not_authorized"), "shop_not_authorized"},
		{status.Error(codes.PermissionDenied, "no_shop_authorized"), "no_shop_authorized"},
		{status.Error(codes.Unavailable, "authorization_unavailable"), "authorization_unavailable"},
	}
	for _, tc := range cases {
		if got := authorizationFailureCode(tc.err); got != tc.want {
			t.Errorf("authorizationFailureCode(%v) = %q, want %q", tc.err, got, tc.want)
		}
	}
}

func TestViewerCannotWrite(t *testing.T) {
	g := &gateway{session: mockSession{role: "viewer"}}
	response := httptest.NewRecorder()
	if g.requireWriteRole(response) {
		t.Fatal("viewer was allowed to write")
	}
	if response.Code != http.StatusForbidden || !strings.Contains(response.Body.String(), "role_forbidden") {
		t.Fatalf("unexpected viewer response: %d %s", response.Code, response.Body.String())
	}
}

func TestAuthorizationCacheKeyBindsPermissionRevision(t *testing.T) {
	oldKey := authorizationCacheKey("tenant-a", "user-a", "auth-v1", "shop-east", "retrieval", "query-hash")
	newKey := authorizationCacheKey("tenant-a", "user-a", "auth-v2", "shop-east", "retrieval", "query-hash")
	if oldKey == newKey {
		t.Fatal("permission revision must change authorization cache key")
	}
	if !strings.Contains(oldKey, ":auth-v1:") {
		t.Fatalf("cache key does not expose revision component: %q", oldKey)
	}
}

func TestFirst24CountsUnicodeRunes(t *testing.T) {
	got := first24("甲乙丙丁戊己庚辛壬癸甲乙丙丁戊己庚辛壬癸甲乙丙丁戊")
	if len([]rune(got)) != 24 {
		t.Fatalf("got %d runes, want 24", len([]rune(got)))
	}
}
