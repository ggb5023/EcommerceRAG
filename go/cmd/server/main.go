package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	ragv1 "github.com/ggb5023/EcommerceRAG/go/internal/pb/rag/v1"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
)

const turnTimeout = 60 * time.Second

type mockSession struct {
	tenantID           int64
	userID             int64
	tenant             string
	user               string
	shop               string
	allowedShops       []string
	allShops           bool
	role               string
	permissionRevision string
	revokedAt          *time.Time
}

// IdentityAdapter is the boundary between the gateway and identity data.
// The M1 implementation is deliberately synthetic; a future OIDC adapter can
// replace it without changing retrieval, evidence, or generation code.
type IdentityAdapter interface {
	Resolve(context.Context) (mockSession, error)
}

type syntheticIdentityAdapter struct {
	db       *pgxpool.Pool
	userID   string
	shopID   string
	allShops bool
}

func (a syntheticIdentityAdapter) Resolve(ctx context.Context) (mockSession, error) {
	userID := a.userID
	if userID == "" {
		userID = "demo-agent-east"
	}
	var session mockSession
	err := a.db.QueryRow(ctx, `
		SELECT t.id, u.id,
		       CASE t.name
		           WHEN 'Synthetic ecommerce demo v1' THEN 'demo-tenant-a'
		           WHEN 'Synthetic ecommerce tenant b' THEN 'demo-tenant-b'
		           ELSE ''
		       END,
		       u.external_id, u.role,
		       COALESCE(u.shop_ids,ARRAY[]::text[]), u.permission_revision, u.revoked_at
		FROM tenant t JOIN app_user u ON u.tenant_id=t.id
		WHERE t.name IN ('Synthetic ecommerce demo v1','Synthetic ecommerce tenant b')
		  AND t.status='active' AND u.external_id=$1
		ORDER BY t.id DESC LIMIT 1`, userID).Scan(
		&session.tenantID, &session.userID, &session.tenant, &session.user,
		&session.role, &session.allowedShops, &session.permissionRevision, &session.revokedAt)
	if err != nil {
		return mockSession{}, err
	}
	session.allShops = a.allShops
	session.shop = a.shopID
	if session.shop == "" && len(session.allowedShops) > 0 {
		// M1 keeps one selected shop in the session. The complete authorized
		// shop list remains available for a future explicit shop selector.
		session.shop = session.allowedShops[0]
	}
	return session, nil
}

type m1IdentityAdapter struct {
	db *pgxpool.Pool
}

func (a m1IdentityAdapter) Resolve(ctx context.Context) (mockSession, error) {
	var session mockSession
	err := a.db.QueryRow(ctx, `
		SELECT t.id, u.id, 'm1-tenant', 'm1-user', 'shop-demo', 'operator'
		FROM tenant t JOIN app_user u ON u.tenant_id=t.id
		JOIN shop s ON s.tenant_id=t.id AND s.id='shop-demo'
		WHERE t.name='M1 isolated prototype' AND t.status='active'
		  AND u.external_id='m1-user' AND u.role='operator' AND s.status='active'
		ORDER BY t.id DESC LIMIT 1`).Scan(
		&session.tenantID, &session.userID, &session.tenant, &session.user,
		&session.shop, &session.role)
	if err != nil {
		return mockSession{}, err
	}
	session.allowedShops = []string{session.shop}
	return session, nil
}

type gateway struct {
	db         *pgxpool.Pool
	rag        ragv1.RagServiceClient
	session    mockSession
	signingKey []byte
	profile    string
	mu         sync.Mutex
	cancels    map[string]context.CancelFunc
}

type apiError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

func (g *gateway) authorizeHTTP(w http.ResponseWriter, r *http.Request) bool {
	if g.db == nil { // unit tests construct a handler-only gateway
		return true
	}
	if err := g.refreshAuthorization(r.Context()); err != nil {
		code := http.StatusForbidden
		if status.Code(err) == codes.Unavailable {
			code = http.StatusServiceUnavailable
		}
		writeError(w, code, authorizationFailureCode(err), "authorization failed")
		return false
	}
	return true
}

func main() {
	if os.Getenv("APP_ENV") != "development" && os.Getenv("APP_ENV") != "test" {
		log.Fatal("APP_ENV must be development or test for the mock-session gateway")
	}
	dsn := os.Getenv("DATABASE_URL")
	ragAddr := os.Getenv("RAG_GRPC_ADDR")
	if (dsn == "" && (os.Getenv("PGHOST") == "" || os.Getenv("PGDATABASE") == "" || os.Getenv("PGUSER") == "")) || ragAddr == "" {
		log.Fatal("DATABASE_URL or PGHOST/PGDATABASE/PGUSER, and RAG_GRPC_ADDR are required")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	db, err := pgxpool.New(ctx, dsn)
	if err != nil {
		log.Fatalf("open database pool: %v", err)
	}
	if err = db.Ping(ctx); err != nil {
		log.Fatalf("ping database: %v", err)
	}
	var databaseName string
	var superuser bool
	if err = db.QueryRow(ctx, `SELECT current_database(),rolsuper FROM pg_roles WHERE rolname=current_user`).Scan(&databaseName, &superuser); err != nil || superuser || !strings.Contains(databaseName, "m1_test") {
		log.Fatal("mock gateway requires a non-superuser in an isolated m1_test database")
	}
	syntheticMode := os.Getenv("SYNTHETIC_MANIFEST") != ""
	profile := os.Getenv("RAG_PROFILE")
	if profile == "" {
		if syntheticMode {
			profile = "synthetic_import_mock"
		} else {
			profile = "m1_fixture_mock"
		}
	}
	if (profile != "synthetic_import_mock" && profile != "m1_fixture_mock") || (profile == "synthetic_import_mock") != syntheticMode {
		log.Fatal("RAG_PROFILE and manifest must select a consistent mock profile")
	}
	var identity IdentityAdapter
	if syntheticMode {
		identity = syntheticIdentityAdapter{
			db: db, userID: os.Getenv("SYNTHETIC_USER_ID"),
			shopID:   os.Getenv("SYNTHETIC_SHOP_ID"),
			allShops: os.Getenv("SYNTHETIC_ALL_SHOPS") == "1",
		}
	} else {
		identity = m1IdentityAdapter{db: db}
	}
	session, err := identity.Resolve(ctx)
	if err != nil {
		if syntheticMode {
			log.Fatalf("load synthetic mock session (apply synthetic seed first): %v", err)
		}
		log.Fatalf("load mock session (apply migrations first): %v", err)
	}
	if err = db.QueryRow(ctx, `SELECT permission_revision FROM app_user WHERE tenant_id=$1 AND id=$2`, session.tenantID, session.userID).Scan(&session.permissionRevision); err != nil {
		if !syntheticMode {
			log.Fatalf("load permission revision: %v", err)
		}
	}
	conn, err := grpc.NewClient(ragAddr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		log.Fatalf("connect RAG service: %v", err)
	}
	g := &gateway{profile: profile, db: db, rag: ragv1.NewRagServiceClient(conn), session: session, cancels: map[string]context.CancelFunc{}}
	seedCtx, seedCancel := context.WithTimeout(context.Background(), 20*time.Second)
	if !syntheticMode {
		fixture, imported, fixtureErr := g.importM1Fixture(seedCtx)
		if fixtureErr != nil {
			seedCancel()
			log.Fatalf("import M1 markdown fixture: %v", fixtureErr)
		}
		if err = g.registerM1Resources(seedCtx); err != nil {
			log.Fatal("register compiled M1 resources failed")
		}
		log.Printf("M1 fixture %s version %s imported=%t", fixture.documentID, fixture.versionID, imported)
	} else {
		log.Printf("synthetic manifest mode enabled: %s", os.Getenv("SYNTHETIC_MANIFEST"))
	}
	seedCancel()
	// Fixture registration can change the permission revision. Resolve afterwards.
	g.session, err = identity.Resolve(context.Background())
	if err != nil {
		log.Fatal("resolve identity after fixture registration failed")
	}
	if err = db.QueryRow(context.Background(), `SELECT permission_revision FROM app_user WHERE tenant_id=$1 AND id=$2`, g.session.tenantID, g.session.userID).Scan(&g.session.permissionRevision); err != nil {
		log.Fatal("read current permission revision failed")
	}
	g.signingKey, err = loadScopeSigningKey()
	if err != nil {
		log.Fatal("restricted scope signing key is unavailable")
	}
	mux := g.routes()
	addr := os.Getenv("HTTP_ADDR")
	if addr == "" {
		addr = "127.0.0.1:8080"
	}
	server := &http.Server{Addr: addr, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	log.Printf("M1 gateway listening on %s", addr)
	log.Fatal(server.ListenAndServe())
}

func (g *gateway) routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", g.health)
	mux.HandleFunc("POST /internal/authorization/validate", g.validateScopeHTTP)
	mux.HandleFunc("GET /v1/session", g.getSession)
	mux.HandleFunc("GET /v1/conversations", g.listConversations)
	mux.HandleFunc("POST /v1/conversations", g.createConversation)
	mux.HandleFunc("GET /v1/conversations/{id}", g.getConversation)
	mux.HandleFunc("POST /v1/conversations/{id}/turns", g.createTurn)
	mux.HandleFunc("POST /v1/dev/fixtures/import", g.importFixtureRoute)
	mux.HandleFunc("GET /v1/turns/{id}", g.getTurn)
	mux.HandleFunc("GET /v1/turns/{id}/events", g.events)
	mux.HandleFunc("POST /v1/turns/{id}/cancel", g.cancelTurn)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/healthz" && r.URL.Path != "/internal/authorization/validate" && !g.authorizeHTTP(w, r) {
			return
		}
		mux.ServeHTTP(w, r)
	})
}

func (g *gateway) health(w http.ResponseWriter, r *http.Request) {
	if err := g.db.Ping(r.Context()); err != nil {
		writeError(w, http.StatusServiceUnavailable, "database_unavailable", "database unavailable")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok", "component": "gateway", "stage": "m1"})
}

func (g *gateway) getSession(w http.ResponseWriter, _ *http.Request) {
	if g.db != nil {
		if err := g.refreshAuthorization(context.Background()); err != nil {
			statusCode := http.StatusForbidden
			if status.Code(err) == codes.Unavailable {
				statusCode = http.StatusServiceUnavailable
			}
			writeError(w, statusCode, authorizationFailureCode(err), "authorization failed")
			return
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"tenantId": g.session.tenant, "userId": g.session.user, "role": g.session.role,
		"shopId": g.session.shop, "shopName": g.session.shop, "displayName": g.session.user, "profile": g.profile,
		"allowedShopIds": g.session.allowedShops, "allShops": g.session.allShops,
		"permission_revision": g.session.permissionRevision,
		"is_mock":             true,
	})
}

func (g *gateway) listConversations(w http.ResponseWriter, r *http.Request) {
	rows, err := g.db.Query(r.Context(), `SELECT id, COALESCE(title,''), shop_id, created_at
		FROM conversation c WHERE tenant_id=$1 AND user_id=$2 AND shop_id=$3
        AND NOT EXISTS(SELECT 1 FROM turn t JOIN conversation_execution e ON e.tenant_id=t.tenant_id AND e.turn_id=t.id
            WHERE t.tenant_id=c.tenant_id AND t.conversation_id=c.id AND e.permission_revision IS DISTINCT FROM $4)
		ORDER BY created_at DESC LIMIT 100`, g.session.tenantID, g.session.userID, g.session.shop, g.session.permissionRevision)
	if err != nil {
		writeError(w, 503, "database_unavailable", "could not load conversations")
		return
	}
	defer rows.Close()
	items := make([]map[string]any, 0)
	for rows.Next() {
		var id int64
		var title, shop string
		var created time.Time
		if err := rows.Scan(&id, &title, &shop, &created); err != nil {
			writeError(w, 503, "database_unavailable", "could not load conversations")
			return
		}
		items = append(items, map[string]any{"id": strconv.FormatInt(id, 10), "title": title, "shop_id": shop,
			"created_at": created, "messages": []any{}})
	}
	if rows.Err() != nil {
		writeError(w, 503, "database_unavailable", "could not load conversations")
		return
	}
	if err := g.refreshAuthorization(r.Context()); err != nil {
		writeError(w, authorizationHTTPStatus(err), authorizationFailureCode(err), "authorization failed")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": items})
}

func (g *gateway) createConversation(w http.ResponseWriter, r *http.Request) {
	if !g.requireWriteRole(w) {
		return
	}
	var in struct {
		Title  string `json:"title"`
		ShopID string `json:"shop_id"`
	}
	if err := decodeJSON(r, &in); err != nil {
		writeError(w, 400, "invalid_json", "invalid request body")
		return
	}
	if in.ShopID != "" && in.ShopID != g.session.shop {
		writeError(w, 403, "forbidden", "shop is outside the current session")
		return
	}
	if strings.TrimSpace(in.Title) == "" {
		in.Title = "新会话"
	}
	var id int64
	err := g.db.QueryRow(r.Context(), `INSERT INTO conversation(tenant_id,user_id,shop_id,title)
		VALUES($1,$2,$3,NULLIF($4,'')) RETURNING id`, g.session.tenantID, g.session.userID,
		g.session.shop, in.Title).Scan(&id)
	if err != nil {
		writeError(w, 503, "database_unavailable", "could not create conversation")
		return
	}
	writeJSON(w, 201, map[string]any{"id": strconv.FormatInt(id, 10), "title": in.Title, "shop_id": g.session.shop, "messages": []any{}})
}

func (g *gateway) getConversation(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil {
		writeError(w, 404, "not_found", "conversation not found")
		return
	}
	var title string
	err = g.db.QueryRow(r.Context(), `SELECT COALESCE(title,'') FROM conversation
		WHERE tenant_id=$1 AND user_id=$2 AND shop_id=$3 AND id=$4`,
		g.session.tenantID, g.session.userID, g.session.shop, id).Scan(&title)
	if errors.Is(err, pgx.ErrNoRows) {
		writeError(w, 404, "not_found", "conversation not found")
		return
	}
	if err != nil {
		writeError(w, 503, "database_unavailable", "could not load conversation")
		return
	}
	if !g.authorizeConversationHistory(w, r, id) {
		return
	}
	rows, err := g.db.Query(r.Context(), `SELECT m.role, COALESCE(m.content,''), t.request_id,t.id,COALESCE((SELECT MAX(e.execution_no) FROM conversation_execution e WHERE e.tenant_id=t.tenant_id AND e.turn_id=t.id),1)
		FROM turn t JOIN message m ON m.tenant_id=t.tenant_id AND m.turn_id=t.id
		WHERE t.tenant_id=$1 AND t.conversation_id=$2 ORDER BY t.turn_no,m.created_at`, g.session.tenantID, id)
	if err != nil {
		writeError(w, 503, "database_unavailable", "could not load messages")
		return
	}
	defer rows.Close()
	messages := make([]map[string]any, 0)
	for rows.Next() {
		var role, content, requestID string
		var turnID int64
		var executionNo int
		if err := rows.Scan(&role, &content, &requestID, &turnID, &executionNo); err != nil {
			writeError(w, 503, "database_unavailable", "could not load messages")
			return
		}
		messages = append(messages, map[string]any{"role": role, "content": content, "request_id": requestID, "turn_id": strconv.FormatInt(turnID, 10), "execution_no": executionNo, "mock": role == "assistant"})
	}
	if err = rows.Err(); err != nil {
		writeError(w, 503, "database_unavailable", "could not load messages")
		return
	}
	rows.Close()
	var latestRequest string
	err = g.db.QueryRow(r.Context(), `SELECT e.request_id FROM turn t JOIN conversation_execution e ON e.tenant_id=t.tenant_id AND e.turn_id=t.id
		WHERE t.tenant_id=$1 AND t.conversation_id=$2 ORDER BY t.turn_no DESC,e.execution_no DESC LIMIT 1`, g.session.tenantID, id).Scan(&latestRequest)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		writeError(w, 503, "database_unavailable", "could not load execution")
		return
	}
	result := map[string]any{"id": r.PathValue("id"), "title": title, "messages": messages}
	if latestRequest != "" {
		last, loadErr := g.executionResult(r.Context(), latestRequest)
		if loadErr != nil {
			writeError(w, 503, "database_unavailable", "could not load execution")
			return
		}
		result["last_turn"] = last
		result["evidence"] = last["evidence"]
		result["citations"] = last["citations"]
		result["lastReply"] = last["answer"]
		result["lastReplyCopyable"] = false
	}
	if err := g.refreshAuthorization(r.Context()); err != nil {
		writeError(w, authorizationHTTPStatus(err), authorizationFailureCode(err), "authorization failed")
		return
	}
	writeJSON(w, 200, result)
}

func (g *gateway) createTurn(w http.ResponseWriter, r *http.Request) {
	if !g.requireWriteRole(w) {
		return
	}
	conversationID, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil {
		writeError(w, 404, "not_found", "conversation not found")
		return
	}
	var in struct {
		Text         string `json:"text"`
		Query        string `json:"query"`
		ParentTurnID string `json:"parent_turn_id"`
	}
	if err := decodeJSON(r, &in); err != nil {
		writeError(w, 400, "invalid_json", "invalid request body")
		return
	}
	query := strings.TrimSpace(in.Text)
	if query == "" {
		query = strings.TrimSpace(in.Query)
	}
	if query == "" {
		writeError(w, 400, "invalid_query", "query is required")
		return
	}
	key := strings.TrimSpace(r.Header.Get("Idempotency-Key"))
	if key == "" {
		writeError(w, 400, "idempotency_key_required", "Idempotency-Key is required")
		return
	}
	canonicalPayload, _ := json.Marshal(map[string]string{"query": query, "parent_turn_id": strings.TrimSpace(in.ParentTurnID)})
	hash := sha256.Sum256(canonicalPayload)
	payloadHash := hex.EncodeToString(hash[:])
	tx, err := g.db.BeginTx(r.Context(), pgx.TxOptions{})
	if err != nil {
		writeError(w, 503, "database_unavailable", "could not start turn")
		return
	}
	defer tx.Rollback(r.Context())
	var shopID string
	err = tx.QueryRow(r.Context(), `SELECT shop_id FROM conversation
		WHERE tenant_id=$1 AND user_id=$2 AND id=$3 FOR UPDATE`,
		g.session.tenantID, g.session.userID, conversationID).Scan(&shopID)
	if errors.Is(err, pgx.ErrNoRows) {
		writeError(w, 404, "not_found", "conversation not found")
		return
	}
	if err != nil {
		writeError(w, 503, "database_unavailable", "could not lock conversation")
		return
	}
	if shopID != g.session.shop {
		writeError(w, 403, "forbidden", "shop is outside the current session")
		return
	}
	if !g.authorizeConversationHistory(w, r, conversationID) {
		return
	}
	var parentTurnID *int64
	if strings.TrimSpace(in.ParentTurnID) != "" {
		var parentID int64
		parentID, err = strconv.ParseInt(strings.TrimSpace(in.ParentTurnID), 10, 64)
		if err != nil {
			err = tx.QueryRow(r.Context(), `SELECT id FROM turn WHERE tenant_id=$1 AND conversation_id=$2 AND request_id=$3`,
				g.session.tenantID, conversationID, strings.TrimSpace(in.ParentTurnID)).Scan(&parentID)
			if errors.Is(err, pgx.ErrNoRows) {
				writeError(w, 400, "invalid_parent_turn", "parent turn is invalid")
				return
			}
		}
		var parentStatus string
		err = tx.QueryRow(r.Context(), `SELECT status FROM turn WHERE tenant_id=$1 AND conversation_id=$2 AND id=$3`,
			g.session.tenantID, conversationID, parentID).Scan(&parentStatus)
		if errors.Is(err, pgx.ErrNoRows) {
			writeError(w, 404, "parent_turn_not_found", "parent turn not found")
			return
		}
		if err != nil {
			writeError(w, 503, "database_unavailable", "could not load parent turn")
			return
		}
		if parentStatus != "ASKING" {
			writeError(w, 409, "parent_turn_not_asking", "parent turn is not awaiting clarification")
			return
		}
		parentTurnID = &parentID
	}
	var oldHash, oldRequest, oldStatus string
	var oldTurnID int64
	var oldExecutionNo int
	err = tx.QueryRow(r.Context(), `SELECT i.payload_hash,e.request_id,e.status,t.id,e.execution_no FROM turn_idempotency i
		JOIN turn t ON t.tenant_id=i.tenant_id AND t.id=i.turn_id
		JOIN conversation_execution e ON e.tenant_id=t.tenant_id AND e.turn_id=t.id
		WHERE i.tenant_id=$1 AND i.conversation_id=$2 AND i.idempotency_key=$3`,
		g.session.tenantID, conversationID, key).Scan(&oldHash, &oldRequest, &oldStatus, &oldTurnID, &oldExecutionNo)
	if err == nil {
		if oldHash != payloadHash {
			writeError(w, 409, "idempotency_conflict", "idempotency key was used with a different query")
			return
		}
		writeJSON(w, 200, map[string]any{"id": oldRequest, "request_id": oldRequest, "status": oldStatus, "turn_id": strconv.FormatInt(oldTurnID, 10), "execution_no": oldExecutionNo, "conversation_id": strconv.FormatInt(conversationID, 10),
			"events_url": "/v1/turns/" + oldRequest + "/events", "is_mock": true})
		return
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		writeError(w, 503, "database_unavailable", "could not check idempotency")
		return
	}
	var active bool
	err = tx.QueryRow(r.Context(), `SELECT EXISTS(SELECT 1 FROM turn WHERE tenant_id=$1
		AND conversation_id=$2 AND status IN ('PENDING','EXECUTING','CANCEL_REQUESTED'))`,
		g.session.tenantID, conversationID).Scan(&active)
	if err != nil {
		writeError(w, 503, "database_unavailable", "could not check active turn")
		return
	}
	if active {
		writeError(w, 409, "turn_in_progress", "conversation already has an active turn")
		return
	}
	requestID := newRequestID()
	var turnID, turnNo, executionID int64
	err = tx.QueryRow(r.Context(), `SELECT COALESCE(MAX(turn_no),0)+1 FROM turn
		WHERE tenant_id=$1 AND conversation_id=$2`, g.session.tenantID, conversationID).Scan(&turnNo)
	if err == nil {
		err = tx.QueryRow(r.Context(), `INSERT INTO turn(tenant_id,conversation_id,turn_no,request_id,user_query,status,parent_turn_id)
			VALUES($1,$2,$3,$4,$5,'EXECUTING',$6) RETURNING id`, g.session.tenantID,
			conversationID, turnNo, requestID, query, parentTurnID).Scan(&turnID)
	}
	deadline := time.Now().Add(turnTimeout)
	if err == nil {
		err = tx.QueryRow(r.Context(), `INSERT INTO conversation_execution(tenant_id,turn_id,execution_no,request_id,status,is_mock,deadline_at,permission_revision)
			VALUES($1,$2,1,$3,'EXECUTING',true,$4,$5) RETURNING id`,
			g.session.tenantID, turnID, requestID, deadline, g.session.permissionRevision).Scan(&executionID)
	}
	if err == nil {
		_, err = tx.Exec(r.Context(), `INSERT INTO turn_idempotency(tenant_id,conversation_id,idempotency_key,payload_hash,turn_id)
			VALUES($1,$2,$3,$4,$5)`, g.session.tenantID, conversationID, key, payloadHash, turnID)
	}
	if err == nil {
		_, err = tx.Exec(r.Context(), `INSERT INTO message(tenant_id,turn_id,role,content) VALUES($1,$2,'user',$3)`, g.session.tenantID, turnID, query)
	}
	if err != nil {
		writeError(w, 503, "database_unavailable", "could not persist turn")
		return
	}
	if err = tx.Commit(r.Context()); err != nil {
		writeError(w, 503, "database_unavailable", "could not commit turn")
		return
	}
	_, _ = g.db.Exec(r.Context(), `UPDATE conversation SET title=CASE WHEN title IS NULL OR title IN ('','新会话') THEN $3 ELSE title END
		WHERE tenant_id=$1 AND id=$2`, g.session.tenantID, conversationID, first24(query))
	ctx, cancel := context.WithDeadline(context.Background(), deadline)
	g.mu.Lock()
	g.cancels[requestID] = cancel
	g.mu.Unlock()
	go g.runTurn(ctx, requestID, conversationID, turnID, executionID, query)
	writeJSON(w, 202, map[string]any{"id": requestID, "request_id": requestID, "status": "EXECUTING",
		"turn_id": strconv.FormatInt(turnID, 10), "execution_no": 1, "conversation_id": strconv.FormatInt(conversationID, 10),
		"is_mock": true, "events_url": "/v1/turns/" + requestID + "/events"})
}

func (g *gateway) historySummary(ctx context.Context, conversationID, currentTurnID int64) string {
	rows, err := g.db.Query(ctx, `SELECT m.role, COALESCE(m.content,'')
		FROM message m JOIN turn t ON t.tenant_id=m.tenant_id AND t.id=m.turn_id
		WHERE t.tenant_id=$1 AND t.conversation_id=$2 AND t.id<>$3 AND EXISTS (SELECT 1 FROM conversation_execution e WHERE e.tenant_id=t.tenant_id AND e.turn_id=t.id AND e.permission_revision=$4)
		ORDER BY t.turn_no DESC,m.created_at DESC LIMIT 6`, g.session.tenantID, conversationID, currentTurnID, g.session.permissionRevision)
	if err != nil {
		return ""
	}
	defer rows.Close()
	parts := make([]string, 0, 6)
	for rows.Next() {
		var role, content string
		if rows.Scan(&role, &content) == nil && strings.TrimSpace(content) != "" {
			parts = append(parts, role+": "+content)
		}
	}
	for left, right := 0, len(parts)-1; left < right; left, right = left+1, right-1 {
		parts[left], parts[right] = parts[right], parts[left]
	}
	return strings.Join(parts, "\n")
}

func (g *gateway) runTurn(ctx context.Context, requestID string, conversationID, turnID, executionID int64, query string) {
	defer func() {
		g.mu.Lock()
		delete(g.cancels, requestID)
		g.mu.Unlock()
	}()
	eventSequence := int64(0)
	writeEvent := func(kind string, payload any) error {
		eventSequence++
		if object, ok := payload.(map[string]any); ok {
			object["seq"] = eventSequence
			object["request_id"] = requestID
			object["turn_id"] = strconv.FormatInt(turnID, 10)
			object["conversation_id"] = strconv.FormatInt(conversationID, 10)
			object["execution_no"] = 1
		}
		body, err := json.Marshal(payload)
		if err != nil {
			return err
		}
		_, err = g.db.Exec(context.Background(), `INSERT INTO sse_event(tenant_id,execution_id,sequence,event_type,data_json)
			VALUES($1,$2,(SELECT COALESCE(MAX(sequence),0)+1 FROM sse_event WHERE tenant_id=$1 AND execution_id=$2),$3,$4::jsonb)`,
			g.session.tenantID, executionID, kind, string(body))
		return err
	}
	finish := func(state, code, answer string) {
		finishCtx, finishCancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer finishCancel()
		tx, err := g.db.Begin(finishCtx)
		if err != nil {
			log.Printf("finish transaction unavailable")
			return
		}
		defer tx.Rollback(finishCtx)
		var current string
		err = tx.QueryRow(finishCtx, `SELECT status FROM conversation_execution WHERE tenant_id=$1 AND id=$2 FOR UPDATE`, g.session.tenantID, executionID).Scan(&current)
		if err != nil || terminal(current) {
			return
		}
		if current == "CANCEL_REQUESTED" || errors.Is(ctx.Err(), context.Canceled) {
			state = "CANCELLED"
			code = "cancelled"
			answer = ""
		}
		if state == "DONE" && ctx.Err() != nil {
			state, code = grpcFailure(ctx, ctx.Err())
			answer = ""
		}
		answerSum := sha256.Sum256([]byte(answer))
		_, err = tx.Exec(finishCtx, `UPDATE conversation_execution SET status=$3,finish_reason=$3,error_code=NULLIF($4,''),answer=$5,answer_hash=$6,finished_at=now()
			WHERE tenant_id=$1 AND id=$2`, g.session.tenantID, executionID, state, code, answer, hex.EncodeToString(answerSum[:]))
		if err == nil {
			_, err = tx.Exec(finishCtx, `UPDATE turn SET status=$3,answer=$4 WHERE tenant_id=$1 AND id=$2`, g.session.tenantID, turnID, state, answer)
		}
		if state == "DONE" && err == nil {
			_, err = tx.Exec(finishCtx, `INSERT INTO message(tenant_id,turn_id,role,content) VALUES($1,$2,'assistant',$3)`, g.session.tenantID, turnID, answer)
			if err == nil {
				_, err = tx.Exec(finishCtx, `INSERT INTO customer_reply(tenant_id,execution_id,text_plain,is_mock,can_copy,blocked_reason)
				VALUES($1,$2,$3,true,false,'mock') ON CONFLICT(tenant_id,execution_id) DO UPDATE SET text_plain=EXCLUDED.text_plain,is_mock=true,can_copy=false,blocked_reason='mock'`, g.session.tenantID, executionID, answer)
			}
		}
		appendEvent := func(kind string, object map[string]any) {
			if err != nil {
				return
			}
			eventSequence++
			object["seq"] = eventSequence
			object["request_id"] = requestID
			object["turn_id"] = strconv.FormatInt(turnID, 10)
			object["conversation_id"] = strconv.FormatInt(conversationID, 10)
			object["execution_no"] = 1
			body, marshalErr := json.Marshal(object)
			if marshalErr != nil {
				err = marshalErr
				return
			}
			_, err = tx.Exec(finishCtx, `INSERT INTO sse_event(tenant_id,execution_id,sequence,event_type,data_json) VALUES($1,$2,$3,$4,$5::jsonb)`, g.session.tenantID, executionID, eventSequence, kind, string(body))
		}
		if state != "DONE" && state != "ASKING" {
			appendEvent("error", map[string]any{"type": "error", "code": code, "message": code})
		}
		appendEvent("completed", map[string]any{"type": "completed", "status": state, "clarification": answer, "error_code": code, "is_mock": true, "can_copy": false})
		if err == nil {
			err = tx.Commit(finishCtx)
		}
		if err != nil {
			log.Printf("finish execution transaction failed")
		}
	}
	ensureAuthorized := func() bool {
		if ctx.Err() != nil {
			state, code := grpcFailure(ctx, ctx.Err())
			finish(state, code, "")
			return false
		}
		if err := g.refreshAuthorization(ctx); err != nil {
			finish("FAILED", authorizationFailureCode(err), "")
			return false
		}
		return true
	}
	if !ensureAuthorized() {
		return
	}
	_ = writeEvent("status", map[string]any{"type": "status", "status": "EXECUTING", "is_mock": true})
	base, err := g.issueScope(ctx, requestID)
	if err != nil {
		finish("FAILED", authorizationFailureCode(err), "")
		return
	}

	routed, err := g.rag.Understand(ctx, &ragv1.UnderstandRequest{Context: base, Query: query,
		HistorySummary: g.historySummary(ctx, conversationID, turnID)})
	if err != nil {
		state, code := grpcFailure(ctx, err)
		finish(state, code, "")
		return
	}
	if routed.Intent == "clarification" || strings.TrimSpace(routed.Clarification) != "" && routed.Confidence < 0.5 {
		if !ensureAuthorized() {
			return
		}
		clarification := routed.Clarification
		if clarification == "" {
			clarification = "请补充更多问题细节。"
		}
		finish("ASKING", "", clarification)
		return
	}
	if !ensureAuthorized() {
		return
	}
	search, err := g.rag.Search(ctx, &ragv1.SearchRequest{Context: base, Query: routed.RewrittenQuery, ResponsePurpose: "customer_reply"})
	if err != nil {
		state, code := grpcFailure(ctx, err)
		finish(state, code, "")
		return
	}
	evidence := make([]*ragv1.Evidence, 0)
	for {
		part, recvErr := search.Recv()
		if errors.Is(recvErr, context.Canceled) || errors.Is(recvErr, context.DeadlineExceeded) {
			state, code := grpcFailure(ctx, recvErr)
			finish(state, code, "")
			return
		}
		if recvErr != nil {
			if status.Code(recvErr) == codes.Canceled || status.Code(recvErr) == codes.DeadlineExceeded {
				state, code := grpcFailure(ctx, recvErr)
				finish(state, code, "")
				return
			}
			finish("FAILED", "rag_search_failed", "")
			return
		}
		if !ensureAuthorized() {
			return
		}
		for _, item := range part.Evidence {
			allowed, authErr := g.evidenceAllowed(ctx, item)
			if authErr != nil {
				finish("FAILED", authorizationFailureCode(authErr), "")
				return
			}
			if !allowed || item.DisclosureClass != "external_allowed" || !item.CustomerEligible {
				continue
			}
			evidence = append(evidence, item)
		}
		if part.Complete {
			break
		}
	}
	for i, item := range evidence {
		if !ensureAuthorized() {
			return
		}
		if _, err = g.db.Exec(ctx, `INSERT INTO evidence_snapshot(tenant_id,execution_id,evidence_id,document_id,version_id,source_ref,content,disclosure_class,customer_eligible,citation_index,shop_id)
			VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) ON CONFLICT(tenant_id,execution_id,evidence_id) DO NOTHING`,
			g.session.tenantID, executionID, item.Id, item.DocumentId, item.VersionId, item.SourceRef,
			item.Content, item.DisclosureClass, item.CustomerEligible, i+1, item.ShopId); err != nil {
			finish("FAILED", "persistence_failed", "")
			return
		}
	}
	if err = writeEvent("evidence", map[string]any{"type": "evidence", "evidence": toEvidence(evidence), "is_mock": true}); err != nil {
		finish("FAILED", "persistence_failed", "")
		return
	}
	if !ensureAuthorized() {
		return
	}
	gen, err := g.rag.Generate(ctx, &ragv1.GenerateRequest{Context: base, Query: routed.RewrittenQuery, Evidence: evidence, ResponsePurpose: "customer_reply"})
	if err != nil {
		state, code := grpcFailure(ctx, err)
		finish(state, code, "")
		return
	}
	var answer strings.Builder
	isMock := true
	for {
		if !ensureAuthorized() {
			return
		}
		part, recvErr := gen.Recv()
		if recvErr != nil {
			if recvErr == context.Canceled || recvErr == context.DeadlineExceeded || status.Code(recvErr) == codes.Canceled || status.Code(recvErr) == codes.DeadlineExceeded {
				state, code := grpcFailure(ctx, recvErr)
				finish(state, code, "")
				return
			}
			if recvErr == context.Canceled {
				finish("CANCELLED", "cancelled", "")
			} else {
				finish("FAILED", "rag_generate_failed", "")
			}
			return
		}
		if !ensureAuthorized() {
			return
		}
		switch value := part.Event.(type) {
		case *ragv1.GenerateResponse_Delta:
			answer.WriteString(value.Delta)
			if err = writeEvent("delta", map[string]any{"type": "delta", "text": value.Delta}); err != nil {
				finish("FAILED", "persistence_failed", "")
				return
			}
		case *ragv1.GenerateResponse_Citation:
			allowed, authErr := g.evidenceAllowed(ctx, value.Citation)
			if authErr != nil {
				finish("FAILED", authorizationFailureCode(authErr), "")
				return
			}
			canonical := false
			for _, original := range evidence {
				if evidenceEqual(original, value.Citation) {
					canonical = true
					break
				}
			}
			if !allowed || !canonical {
				finish("FAILED", "invalid_citation", "")
				return
			}
			if value.Citation.DisclosureClass == "external_allowed" && value.Citation.CustomerEligible {
				_, _ = g.db.Exec(ctx, `INSERT INTO citation(tenant_id,execution_id,evidence_id,citation_index)
					VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING`, g.session.tenantID, executionID, value.Citation.Id, int(value.Citation.Rank))
			}
		case *ragv1.GenerateResponse_IsMock:
			isMock = value.IsMock
		case *ragv1.GenerateResponse_ErrorCode:
			finish("FAILED", value.ErrorCode, "")
			return
		case *ragv1.GenerateResponse_Done:
			if !value.Done {
				continue
			}
			if !isMock {
				finish("FAILED", "non_mock_provider_rejected", "")
				return
			}
			text := answer.String()
			if !ensureAuthorized() {
				return
			}

			finish("DONE", "", text)
			return
		}
	}
}

func authorizationFailureCode(err error) string {
	switch status.Code(err) {
	case codes.PermissionDenied:
		if strings.Contains(err.Error(), "identity_revoked") {
			return "identity_revoked"
		}
		if strings.Contains(err.Error(), "permission_changed") {
			return "permission_changed"
		}
		if strings.Contains(err.Error(), "shop_not_authorized") {
			return "shop_not_authorized"
		}
		if strings.Contains(err.Error(), "no_shop_authorized") {
			return "no_shop_authorized"
		}
		return "permission_denied"
	case codes.Unavailable:
		return "authorization_unavailable"
	default:
		return "authorization_failed"
	}
}

// authorizationCacheKey is the stable shape required for any future result
// cache. Callers must provide a hash for query/resource inputs; the current M1
// gateway does not enable an application result cache. Permission revisions
// are part of the key so a revoked or changed scope cannot reuse old data.
func authorizationCacheKey(tenant, user, permissionRevision, shop, resource, inputHash string) string {
	return strings.Join([]string{"ecr:v1", "auth", tenant, user, permissionRevision, shop, resource, inputHash}, ":")
}

func (g *gateway) getTurn(w http.ResponseWriter, r *http.Request) {
	if !g.authorizeExecution(w, r, r.PathValue("id")) {
		return
	}
	result, err := g.executionResult(r.Context(), r.PathValue("id"))
	if errors.Is(err, pgx.ErrNoRows) {
		writeError(w, 404, "not_found", "turn not found")
		return
	}
	if err != nil {
		writeError(w, 503, "database_unavailable", "could not load execution")
		return
	}
	if err = g.refreshAuthorization(r.Context()); err != nil {
		writeError(w, authorizationHTTPStatus(err), authorizationFailureCode(err), "authorization failed")
		return
	}
	writeJSON(w, 200, result)
}

func (g *gateway) events(w http.ResponseWriter, r *http.Request) {
	requestID := r.PathValue("id")
	if !g.authorizeExecution(w, r, requestID) {
		return
	}
	var executionID int64
	var deadline time.Time
	err := g.db.QueryRow(r.Context(), `SELECT e.id,e.deadline_at FROM conversation_execution e
		JOIN turn t ON t.tenant_id=e.tenant_id AND t.id=e.turn_id
		JOIN conversation c ON c.tenant_id=t.tenant_id AND c.id=t.conversation_id
		WHERE e.tenant_id=$1 AND e.request_id=$2 AND c.user_id=$3 AND c.shop_id=$4`,
		g.session.tenantID, requestID, g.session.userID, g.session.shop).Scan(&executionID, &deadline)
	if errors.Is(err, pgx.ErrNoRows) {
		writeError(w, 404, "not_found", "turn not found")
		return
	}
	if err != nil {
		writeError(w, 503, "database_unavailable", "could not load events")
		return
	}
	lastID, _ := strconv.ParseInt(r.Header.Get("Last-Event-ID"), 10, 64)
	if header := r.URL.Query().Get("last_event_id"); header != "" && lastID == 0 {
		lastID, _ = strconv.ParseInt(header, 10, 64)
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache, no-transform")
	w.Header().Set("X-Accel-Buffering", "no")
	flusher, _ := w.(http.Flusher)
	ticker := time.NewTicker(200 * time.Millisecond)
	defer ticker.Stop()
	for {
		if err := g.refreshAuthorization(r.Context()); err != nil {
			if flusher != nil {
				payload, _ := json.Marshal(map[string]any{"type": "error", "code": authorizationFailureCode(err), "message": "authorization failed", "request_id": requestID, "http_status": authorizationHTTPStatus(err)})
				fmt.Fprintf(w, "event: error\ndata: %s\n\n", payload)
				flusher.Flush()
			}
			return
		}
		rows, queryErr := g.db.Query(r.Context(), `SELECT sequence,event_type,data_json::text FROM sse_event
			WHERE tenant_id=$1 AND execution_id=$2 AND sequence>$3 ORDER BY sequence`, g.session.tenantID, executionID, lastID)
		if queryErr != nil {
			return
		}
		count := 0
		for rows.Next() {
			var seq int64
			var kind, data string
			if rows.Scan(&seq, &kind, &data) != nil {
				rows.Close()
				return
			}
			if authErr := g.refreshAuthorization(r.Context()); authErr != nil {
				rows.Close()
				payload, _ := json.Marshal(map[string]any{"type": "error", "code": authorizationFailureCode(authErr), "message": "authorization failed", "request_id": requestID, "http_status": authorizationHTTPStatus(authErr)})
				fmt.Fprintf(w, "event: error\ndata: %s\n\n", payload)
				if flusher != nil {
					flusher.Flush()
				}
				return
			}
			fmt.Fprintf(w, "id: %d\nevent: %s\ndata: %s\n\n", seq, kind, data)
			lastID, count = seq, count+1
		}
		rows.Close()
		if flusher != nil && count > 0 {
			flusher.Flush()
		}
		var state string
		if err := g.db.QueryRow(r.Context(), `SELECT status FROM conversation_execution WHERE tenant_id=$1 AND id=$2`, g.session.tenantID, executionID).Scan(&state); err != nil {
			return
		}
		if terminal(state) && count == 0 {
			return
		}
		if !deadline.IsZero() && time.Now().After(deadline.Add(5*time.Second)) {
			return
		}
		select {
		case <-r.Context().Done():
			return
		case <-ticker.C:
			if count == 0 {
				fmt.Fprint(w, ": keep-alive\n\n")
				if flusher != nil {
					flusher.Flush()
				}
			}
		}
	}
}

func (g *gateway) cancelTurn(w http.ResponseWriter, r *http.Request) {
	if !g.requireWriteRole(w) {
		return
	}
	requestID := r.PathValue("id")
	if !g.authorizeExecution(w, r, requestID) {
		return
	}
	tx, err := g.db.Begin(r.Context())
	if err != nil {
		writeError(w, 503, "database_unavailable", "could not cancel turn")
		return
	}
	defer tx.Rollback(r.Context())
	var state string
	var executionID, turnID int64
	err = tx.QueryRow(r.Context(), `SELECT e.status,e.id,e.turn_id FROM conversation_execution e
		JOIN turn t ON t.tenant_id=e.tenant_id AND t.id=e.turn_id JOIN conversation c ON c.tenant_id=t.tenant_id AND c.id=t.conversation_id
		WHERE e.tenant_id=$1 AND e.request_id=$2 AND c.user_id=$3 AND c.shop_id=$4 FOR UPDATE OF e`, g.session.tenantID, requestID, g.session.userID, g.session.shop).Scan(&state, &executionID, &turnID)
	if err != nil {
		writeError(w, 503, "database_unavailable", "could not cancel turn")
		return
	}
	if terminal(state) {
		writeJSON(w, 200, map[string]any{"request_id": requestID, "status": state})
		return
	}
	_, err = tx.Exec(r.Context(), `UPDATE conversation_execution SET cancel_requested=true,status='CANCEL_REQUESTED' WHERE tenant_id=$1 AND id=$2`, g.session.tenantID, executionID)
	if err == nil {
		_, err = tx.Exec(r.Context(), `UPDATE turn SET status='CANCEL_REQUESTED' WHERE tenant_id=$1 AND id=$2`, g.session.tenantID, turnID)
	}
	if err == nil {
		err = tx.Commit(r.Context())
	}
	if err != nil {
		writeError(w, 503, "database_unavailable", "could not persist cancellation")
		return
	}
	g.mu.Lock()
	stop := g.cancels[requestID]
	g.mu.Unlock()
	if stop != nil {
		stop()
	}
	writeJSON(w, 200, map[string]any{"request_id": requestID, "status": "CANCEL_REQUESTED"})
}

func grpcFailure(ctx context.Context, err error) (string, string) {
	if errors.Is(ctx.Err(), context.Canceled) || status.Code(err) == codes.Canceled {
		return "CANCELLED", "cancelled"
	}
	if errors.Is(ctx.Err(), context.DeadlineExceeded) || status.Code(err) == codes.DeadlineExceeded {
		return "FAILED", "deadline_exceeded"
	}
	switch status.Code(err) {
	case codes.InvalidArgument:
		return "FAILED", "invalid_argument"
	case codes.PermissionDenied, codes.Unauthenticated:
		return "FAILED", "permission_denied"
	case codes.ResourceExhausted:
		return "FAILED", "rate_limited"
	case codes.Unavailable:
		return "FAILED", "rag_unavailable"
	default:
		return "FAILED", "rag_failed"
	}
}

func toEvidence(items []*ragv1.Evidence) []map[string]any {
	out := make([]map[string]any, 0, len(items))
	for _, item := range items {
		out = append(out, map[string]any{"id": item.Id, "title": item.DocumentId, "snippet": item.Content,
			"sourceType": item.SourceType, "documentId": item.DocumentId, "versionId": item.VersionId,
			"sourceRef": item.SourceRef, "score": item.RawScore, "customerEligible": item.CustomerEligible, "citationIndex": item.Rank, "shopId": item.ShopId})
	}
	return out
}

func newRequestID() string { return fmt.Sprintf("m1-%d", time.Now().UnixNano()) }
func terminal(state string) bool {
	switch state {
	case "DONE", "FAILED", "CANCELLED", "ASKING":
		return true
	default:
		return false
	}
}
func first24(value string) string {
	value = strings.Join(strings.Fields(value), " ")
	runes := []rune(value)
	if len(runes) > 24 {
		runes = runes[:24]
	}
	return string(runes)
}
func decodeJSON(r *http.Request, dst any) error {
	defer r.Body.Close()
	data, err := io.ReadAll(io.LimitReader(r.Body, (1<<20)+1))
	if err != nil {
		return err
	}
	if len(data) > 1<<20 {
		return errors.New("request body too large")
	}
	return json.Unmarshal(data, dst)
}
func writeJSON(w http.ResponseWriter, statusCode int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(statusCode)
	_ = json.NewEncoder(w).Encode(value)
}
func writeError(w http.ResponseWriter, statusCode int, code, message string) {
	writeJSON(w, statusCode, apiError{Code: code, Message: message})
}
