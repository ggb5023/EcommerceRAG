package main

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	ragv1 "github.com/ggb5023/EcommerceRAG/go/internal/pb/rag/v1"
	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
)

// A barrier places revocation inside a blocked Generate Recv, without timing guesses.
type barrierRAG struct {
	ragv1.UnimplementedRagServiceServer
	entered chan struct{}
	release chan struct{}
}

func TestPythonRequiresCurrentSignedGoScope(t *testing.T) {
	if os.Getenv("ECR_AUTH_INTEGRATION") != "1" {
		t.Skip("requires seeded isolated m1_test database")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	app, err := pgxpool.New(ctx, os.Getenv("M1_APP_DATABASE_URL"))
	if err != nil {
		t.Fatal("app pool unavailable")
	}
	defer app.Close()
	admin, err := pgxpool.New(ctx, os.Getenv("M1_MIGRATION_DATABASE_URL"))
	if err != nil {
		t.Fatal("migration pool unavailable")
	}
	defer admin.Close()
	var dbName string
	if err = app.QueryRow(ctx, `SELECT current_database()`).Scan(&dbName); err != nil || !strings.Contains(dbName, "m1_test") {
		t.Fatal("isolated database required")
	}
	identity := syntheticIdentityAdapter{db: app, userID: "demo-agent-east"}
	session, err := identity.Resolve(ctx)
	if err != nil {
		t.Fatal("fixture identity unavailable")
	}
	key := make([]byte, 32)
	if _, err = rand.Read(key); err != nil {
		t.Fatal(err)
	}
	g := &gateway{db: app, session: session, signingKey: key, cancels: map[string]context.CancelFunc{}}
	authority := httptest.NewServer(g.routes())
	defer authority.Close()
	scope, err := g.issueScope(ctx, "direct-python-scope")
	if err != nil {
		t.Fatal("issue scope failed")
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	address := listener.Addr().String()
	listener.Close()
	repo, err := filepath.Abs("../../..")
	if err != nil {
		t.Fatal(err)
	}
	process := exec.Command(filepath.Join(repo, "python/.venv/bin/python"), "-m", "app.server")
	process.Dir = repo
	process.Env = []string{"PATH=" + os.Getenv("PATH"), "PYTHONPATH=" + filepath.Join(repo, "python"), "APP_ENV=test", "GRPC_ADDR=" + address,
		"SYNTHETIC_MANIFEST=" + filepath.Join(repo, "data/synthetic/ecommerce-demo-v1/manifest.yaml"), "AUTHORITY_HTTP_BASE=" + authority.URL}
	if err = process.Start(); err != nil {
		t.Fatal("Python service unavailable")
	}
	defer func() { process.Process.Signal(os.Interrupt); process.Wait() }()
	connection, err := grpc.NewClient(address, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	client := ragv1.NewRagServiceClient(connection)
	for {
		_, err = client.Understand(ctx, &ragv1.UnderstandRequest{Context: scope, Query: "配送规则"})
		if err == nil {
			break
		}
		if status.Code(err) != codes.Unavailable {
			t.Fatalf("signed scope rejected: %s", status.Code(err))
		}
		select {
		case <-time.After(30 * time.Millisecond):
		case <-ctx.Done():
			t.Fatal("Python startup timed out")
		}
	}
	unsigned := &ragv1.RequestContext{RequestId: "forged", TenantId: scope.TenantId, UserId: scope.UserId, Role: "owner", ShopId: scope.ShopId, AllowedShopIds: scope.AllowedShopIds, PermissionRevision: scope.PermissionRevision, EnforceDocumentScope: true, AllowedDocumentIds: []string{"syn-restricted-a"}}
	if _, err = client.Understand(ctx, &ragv1.UnderstandRequest{Context: unsigned, Query: "配送规则"}); status.Code(err) != codes.PermissionDenied {
		t.Fatal("unsigned Python scope accepted")
	}
	search, err := client.Search(ctx, &ragv1.SearchRequest{Context: scope, Query: "配送规则"})
	if err != nil {
		t.Fatal(err)
	}
	var evidence []*ragv1.Evidence
	for {
		part, e := search.Recv()
		if e == io.EOF {
			break
		}
		if e != nil {
			t.Fatal("authorized Search failed")
		}
		for _, item := range part.Evidence {
			if item.DocumentId == "syn-restricted-a" || item.ShopId != "demo-shop-east" || item.TenantId != "demo-tenant-a" {
				t.Fatal("manifest enlarged signed scope")
			}
			evidence = append(evidence, item)
		}
	}
	if len(evidence) == 0 {
		t.Fatal("authorized Search returned no evidence")
	}
	evidence[0].Content = "forged source content"
	generate, err := client.Generate(ctx, &ragv1.GenerateRequest{Context: scope, Evidence: evidence})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = generate.Recv(); status.Code(err) != codes.InvalidArgument {
		t.Fatal("forged generation evidence accepted")
	}
	defer func() {
		if _, restoreErr := admin.Exec(context.Background(), `UPDATE app_user SET revoked_at=NULL WHERE tenant_id=$1 AND id=$2`, session.tenantID, session.userID); restoreErr != nil {
			t.Error("fixture restore failed")
		}
	}()
	if _, err = admin.Exec(ctx, `UPDATE app_user SET revoked_at=now() WHERE tenant_id=$1 AND id=$2`, session.tenantID, session.userID); err != nil {
		t.Fatal("revoke failed")
	}
	if _, err = client.Understand(ctx, &ragv1.UnderstandRequest{Context: scope, Query: "配送规则"}); status.Code(err) != codes.PermissionDenied {
		t.Fatal("old signed revision still accepted by Python")
	}
}

func (b *barrierRAG) Understand(_ context.Context, r *ragv1.UnderstandRequest) (*ragv1.UnderstandResponse, error) {
	return &ragv1.UnderstandResponse{RewrittenQuery: r.Query, Intent: "knowledge", InformationSource: "knowledge", Confidence: 1}, nil
}
func (b *barrierRAG) Search(r *ragv1.SearchRequest, s grpc.ServerStreamingServer[ragv1.SearchResponse]) error {
	return s.Send(&ragv1.SearchResponse{Complete: true, RequestId: r.Context.RequestId, IsMock: true, Evidence: []*ragv1.Evidence{{
		Id: "barrier-evidence", DocumentId: "syn-policy-a", VersionId: "barrier-version", TenantId: r.Context.TenantId,
		ShopId: r.Context.ShopId, Content: "synthetic barrier policy", SourceRef: "synthetic://barrier",
		DisclosureClass: "external_allowed", CustomerEligible: true, Rank: 1}}})
}
func (b *barrierRAG) Generate(_ *ragv1.GenerateRequest, s grpc.ServerStreamingServer[ragv1.GenerateResponse]) error {
	close(b.entered)
	select {
	case <-b.release:
	case <-s.Context().Done():
		return s.Context().Err()
	}
	if err := s.Send(&ragv1.GenerateResponse{Sequence: 1, Event: &ragv1.GenerateResponse_Delta{Delta: "revoked output must never be published"}}); err != nil {
		return err
	}
	return s.Send(&ragv1.GenerateResponse{Sequence: 2, Event: &ragv1.GenerateResponse_Done{Done: true}})
}

func TestRevocationDuringBlockedGenerateAndOpenSSE(t *testing.T) {
	if os.Getenv("ECR_AUTH_INTEGRATION") != "1" {
		t.Skip("requires seeded isolated m1_test database")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	app, err := pgxpool.New(ctx, os.Getenv("M1_APP_DATABASE_URL"))
	if err != nil {
		t.Fatal("open app pool failed")
	}
	defer app.Close()
	admin, err := pgxpool.New(ctx, os.Getenv("M1_MIGRATION_DATABASE_URL"))
	if err != nil {
		t.Fatal("open migration pool failed")
	}
	defer admin.Close()
	var name string
	if err = admin.QueryRow(ctx, `SELECT current_database()`).Scan(&name); err != nil || !strings.Contains(name, "m1_test") {
		t.Fatal("isolated test database required")
	}
	session, err := (syntheticIdentityAdapter{db: app, userID: "demo-agent-east"}).Resolve(ctx)
	if err != nil {
		t.Fatal("resolve fixture identity failed")
	}
	if session.role != "operator" {
		t.Fatal("fixture operator required")
	}
	defer func() {
		if _, restoreErr := admin.Exec(context.Background(), `UPDATE app_user SET role='operator',revoked_at=NULL WHERE tenant_id=$1 AND id=$2`, session.tenantID, session.userID); restoreErr != nil {
			t.Error("restore synthetic fixture failed")
		}
	}()
	barrier := &barrierRAG{entered: make(chan struct{}), release: make(chan struct{})}
	listener := bufconn.Listen(1 << 20)
	server := grpc.NewServer()
	ragv1.RegisterRagServiceServer(server, barrier)
	go server.Serve(listener)
	defer server.Stop()
	connection, err := grpc.NewClient("passthrough:///barrier", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }))
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	key := make([]byte, 32)
	if _, err = rand.Read(key); err != nil {
		t.Fatal(err)
	}
	g := &gateway{db: app, rag: ragv1.NewRagServiceClient(connection), session: session, signingKey: key, cancels: map[string]context.CancelFunc{}}
	httpServer := httptest.NewServer(g.routes())
	defer httpServer.Close()
	post := func(path, body string) map[string]any {
		req, _ := http.NewRequestWithContext(ctx, "POST", httpServer.URL+path, strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Idempotency-Key", "barrier-key")
		response, e := http.DefaultClient.Do(req)
		if e != nil {
			t.Fatal(e)
		}
		defer response.Body.Close()
		if response.StatusCode != 201 && response.StatusCode != 202 {
			t.Fatalf("POST failed: %d", response.StatusCode)
		}
		var result map[string]any
		if json.NewDecoder(response.Body).Decode(&result) != nil {
			t.Fatal("invalid HTTP result")
		}
		return result
	}
	conversation := post("/v1/conversations", `{}`)["id"].(string)
	turn := post("/v1/conversations/"+conversation+"/turns", `{"text":"配送规则"}`)["request_id"].(string)
	select {
	case <-barrier.entered:
	case <-ctx.Done():
		t.Fatal("generation barrier not reached")
	}
	req, _ := http.NewRequestWithContext(ctx, "GET", httpServer.URL+"/v1/turns/"+turn+"/events", nil)
	response, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != 200 {
		t.Fatalf("SSE open failed: %d", response.StatusCode)
	}
	if _, err = admin.Exec(ctx, `UPDATE app_user SET role='viewer',permission_revision='attempted-reuse' WHERE tenant_id=$1 AND id=$2`, session.tenantID, session.userID); err != nil {
		t.Fatal("revoke failed")
	}
	close(barrier.release)
	stream, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal("SSE did not close")
	}
	if !strings.Contains(string(stream), "permission_changed") || strings.Contains(string(stream), "revoked output") {
		t.Fatal("open SSE did not fail closed")
	}
	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()
	for {
		var state string
		if err = app.QueryRow(ctx, `SELECT status FROM conversation_execution WHERE tenant_id=$1 AND request_id=$2`, session.tenantID, turn).Scan(&state); err != nil {
			t.Fatal("read final state failed")
		}
		if state == "FAILED" {
			break
		}
		select {
		case <-ticker.C:
		case <-ctx.Done():
			t.Fatal("revoked execution did not fail")
		}
	}
	var leaked int
	err = app.QueryRow(ctx, `SELECT (SELECT count(*) FROM sse_event s JOIN conversation_execution e ON e.tenant_id=s.tenant_id AND e.id=s.execution_id WHERE e.request_id=$1 AND s.event_type='delta')+
        (SELECT count(*) FROM customer_reply cr JOIN conversation_execution e ON e.tenant_id=cr.tenant_id AND e.id=cr.execution_id WHERE e.request_id=$1)`, turn).Scan(&leaked)
	if err != nil || leaked != 0 {
		t.Fatal("revoked output was persisted")
	}
}
