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

func TestFirst24CountsUnicodeRunes(t *testing.T) {
	got := first24("甲乙丙丁戊己庚辛壬癸甲乙丙丁戊己庚辛壬癸甲乙丙丁戊")
	if len([]rune(got)) != 24 {
		t.Fatalf("got %d runes, want 24", len([]rune(got)))
	}
}
