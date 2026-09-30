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
	tenantID int64
	userID   int64
	tenant   string
	user     string
	shop     string
	role     string
}

type gateway struct {
	db      *pgxpool.Pool
	rag     ragv1.RagServiceClient
	session mockSession
	mu      sync.Mutex
	cancels map[string]context.CancelFunc
}

type apiError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
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
	var session mockSession
	err = db.QueryRow(ctx, `
		SELECT t.id, u.id, 'm1-tenant', 'm1-user', 'shop-demo', 'operator'
		FROM tenant t JOIN app_user u ON u.tenant_id=t.id
		JOIN shop s ON s.tenant_id=t.id AND s.id='shop-demo'
		WHERE t.name='M1 isolated prototype' AND t.status='active'
		  AND u.external_id='m1-user' AND u.role='operator' AND s.status='active'
		ORDER BY t.id DESC LIMIT 1`).Scan(
		&session.tenantID, &session.userID, &session.tenant,
		&session.user, &session.shop, &session.role,
	)
	if err != nil {
		log.Fatalf("load mock session (apply migrations first): %v", err)
	}
	conn, err := grpc.NewClient(ragAddr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		log.Fatalf("connect RAG service: %v", err)
	}
	g := &gateway{db: db, rag: ragv1.NewRagServiceClient(conn), session: session, cancels: map[string]context.CancelFunc{}}
	seedCtx, seedCancel := context.WithTimeout(context.Background(), 20*time.Second)
	fixture, imported, err := g.importM1Fixture(seedCtx)
	seedCancel()
	if err != nil {
		log.Fatalf("import M1 markdown fixture: %v", err)
	}
	log.Printf("M1 fixture %s version %s imported=%t", fixture.documentID, fixture.versionID, imported)
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
	mux.HandleFunc("GET /v1/session", g.getSession)
	mux.HandleFunc("GET /v1/conversations", g.listConversations)
	mux.HandleFunc("POST /v1/conversations", g.createConversation)
	mux.HandleFunc("GET /v1/conversations/{id}", g.getConversation)
	mux.HandleFunc("POST /v1/conversations/{id}/turns", g.createTurn)
	mux.HandleFunc("POST /v1/dev/fixtures/import", g.importFixtureRoute)
	mux.HandleFunc("GET /v1/turns/{id}", g.getTurn)
	mux.HandleFunc("GET /v1/turns/{id}/events", g.events)
	mux.HandleFunc("POST /v1/turns/{id}/cancel", g.cancelTurn)
	return mux
}

func (g *gateway) health(w http.ResponseWriter, r *http.Request) {
	if err := g.db.Ping(r.Context()); err != nil {
		writeError(w, http.StatusServiceUnavailable, "database_unavailable", "database unavailable")
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok", "component": "gateway", "stage": "m1"})
}

func (g *gateway) getSession(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"tenantId": g.session.tenant, "userId": g.session.user, "role": g.session.role,
		"shopId": g.session.shop, "shopName": "M1 演示店铺", "displayName": "M1 演示客服",
		"is_mock": true,
	})
}

func (g *gateway) listConversations(w http.ResponseWriter, r *http.Request) {
	rows, err := g.db.Query(r.Context(), `SELECT id, COALESCE(title,''), shop_id, created_at
		FROM conversation WHERE tenant_id=$1 AND user_id=$2 AND shop_id=$3
		ORDER BY created_at DESC LIMIT 100`, g.session.tenantID, g.session.userID, g.session.shop)
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
	writeJSON(w, http.StatusOK, map[string]any{"items": items})
}

func (g *gateway) createConversation(w http.ResponseWriter, r *http.Request) {
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
	rows, err := g.db.Query(r.Context(), `SELECT m.role, COALESCE(m.content,''), t.request_id
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
		if err := rows.Scan(&role, &content, &requestID); err != nil {
			writeError(w, 503, "database_unavailable", "could not load messages")
			return
		}
		messages = append(messages, map[string]any{"role": role, "content": content, "request_id": requestID, "mock": role == "assistant"})
	}
	var latestTurnID int64
	var latestRequest, latestStatus string
	var latestAnswer *string
	var latestMock bool
	err = g.db.QueryRow(r.Context(), `SELECT t.id,t.request_id,t.status,t.answer,e.is_mock
		FROM turn t JOIN conversation_execution e ON e.tenant_id=t.tenant_id AND e.turn_id=t.id
		WHERE t.tenant_id=$1 AND t.conversation_id=$2
		ORDER BY t.turn_no DESC,e.execution_no DESC LIMIT 1`, g.session.tenantID, id).
		Scan(&latestTurnID, &latestRequest, &latestStatus, &latestAnswer, &latestMock)
	evidence := make([]map[string]any, 0)
	citations := make([]map[string]any, 0)
	var reply *string
	var canCopy bool
	blockedReason := ""
	if latestRequest != "" {
		erows, eerr := g.db.Query(r.Context(), `SELECT es.evidence_id,COALESCE(es.document_id,''),COALESCE(es.version_id,''),
			COALESCE(es.source_ref,''),es.content,es.disclosure_class,es.customer_eligible,es.citation_index
			FROM evidence_snapshot es JOIN conversation_execution e ON e.tenant_id=es.tenant_id AND e.id=es.execution_id
			WHERE e.tenant_id=$1 AND e.request_id=$2 ORDER BY es.citation_index`, g.session.tenantID, latestRequest)
		if eerr == nil {
			for erows.Next() {
				var evidenceID, documentID, versionID, sourceRef, content, disclosure string
				var eligible bool
				var citationIndex *int
				if erows.Scan(&evidenceID, &documentID, &versionID, &sourceRef, &content, &disclosure, &eligible, &citationIndex) == nil {
					evidence = append(evidence, map[string]any{"id": evidenceID, "title": documentID, "snippet": content,
						"sourceType": disclosure, "documentId": documentID, "versionId": versionID, "sourceRef": sourceRef,
						"score": 1.0, "citationIndex": citationIndex, "customerEligible": eligible})
				}
			}
			erows.Close()
		}
		crows, cerr := g.db.Query(r.Context(), `SELECT c.evidence_id,c.citation_index
			FROM citation c JOIN conversation_execution e ON e.tenant_id=c.tenant_id AND e.id=c.execution_id
			WHERE c.tenant_id=$1 AND e.request_id=$2 ORDER BY c.citation_index`, g.session.tenantID, latestRequest)
		if cerr == nil {
			for crows.Next() {
				var evidenceID string
				var citationIndex int
				if crows.Scan(&evidenceID, &citationIndex) == nil {
					citations = append(citations, map[string]any{"evidence_id": evidenceID, "citation_index": citationIndex})
				}
			}
			crows.Close()
		}
		_ = g.db.QueryRow(r.Context(), `SELECT text_plain,can_copy,COALESCE(blocked_reason,'')
			FROM customer_reply cr JOIN conversation_execution e ON e.tenant_id=cr.tenant_id AND e.id=cr.execution_id
			WHERE e.tenant_id=$1 AND e.request_id=$2`, g.session.tenantID, latestRequest).
			Scan(&reply, &canCopy, &blockedReason)
		if latestMock {
			canCopy = false
			blockedReason = "mock"
		}
	}
	result := map[string]any{"id": r.PathValue("id"), "title": title, "messages": messages,
		"evidence": evidence, "citations": citations}
	if latestRequest != "" {
		result["last_turn"] = map[string]any{"turn_id": strconv.FormatInt(latestTurnID, 10), "request_id": latestRequest, "status": latestStatus,
			"answer": latestAnswer, "is_mock": latestMock, "evidence": evidence, "citations": citations,
			"customer_reply": map[string]any{"text_plain": reply, "can_copy": canCopy, "blocked_reason": blockedReason}}
		result["lastReply"] = latestAnswer
		result["lastReplyCopyable"] = canCopy
	}
	writeJSON(w, 200, result)
}

func (g *gateway) createTurn(w http.ResponseWriter, r *http.Request) {
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
	hash := sha256.Sum256([]byte(query))
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
	var oldHash, oldRequest string
	err = tx.QueryRow(r.Context(), `SELECT i.payload_hash,t.request_id FROM turn_idempotency i
		JOIN turn t ON t.tenant_id=i.tenant_id AND t.id=i.turn_id
		WHERE i.tenant_id=$1 AND i.conversation_id=$2 AND i.idempotency_key=$3`,
		g.session.tenantID, conversationID, key).Scan(&oldHash, &oldRequest)
	if err == nil {
		if oldHash != payloadHash {
			writeError(w, 409, "idempotency_conflict", "idempotency key was used with a different query")
			return
		}
		writeJSON(w, 200, map[string]any{"id": oldRequest, "request_id": oldRequest, "status": "EXISTING",
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
		err = tx.QueryRow(r.Context(), `INSERT INTO conversation_execution(tenant_id,turn_id,execution_no,request_id,status,is_mock,deadline_at)
			VALUES($1,$2,1,$3,'EXECUTING',true,$4) RETURNING id`,
			g.session.tenantID, turnID, requestID, deadline).Scan(&executionID)
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
	_, _ = g.db.Exec(r.Context(), `UPDATE conversation SET title=COALESCE(NULLIF(title,''),$3)
		WHERE tenant_id=$1 AND id=$2`, g.session.tenantID, conversationID, first24(query))
	ctx, cancel := context.WithDeadline(context.Background(), deadline)
	g.mu.Lock()
	g.cancels[requestID] = cancel
	g.mu.Unlock()
	go g.runTurn(ctx, requestID, conversationID, turnID, executionID, query)
	writeJSON(w, 202, map[string]any{"id": requestID, "request_id": requestID, "status": "EXECUTING",
		"is_mock": true, "events_url": "/v1/turns/" + requestID + "/events"})
}

func (g *gateway) historySummary(ctx context.Context, conversationID, currentTurnID int64) string {
	rows, err := g.db.Query(ctx, `SELECT m.role, COALESCE(m.content,'')
		FROM message m JOIN turn t ON t.tenant_id=m.tenant_id AND t.id=m.turn_id
		WHERE t.tenant_id=$1 AND t.conversation_id=$2 AND t.id<>$3
		ORDER BY t.turn_no DESC,m.created_at DESC LIMIT 6`, g.session.tenantID, conversationID, currentTurnID)
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
		finishCtx := context.Background()
		if state != "DONE" {
			_ = writeEvent("error", map[string]any{"type": "error", "message": code, "code": code})
		}
		_ = writeEvent("completed", map[string]any{"type": "completed", "status": state, "error_code": code, "is_mock": true, "can_copy": false})
		answerSum := sha256.Sum256([]byte(answer))
		_, err := g.db.Exec(finishCtx, `UPDATE conversation_execution SET status=$3,finish_reason=$3,error_code=NULLIF($4,''),
			answer=$5,answer_hash=$6,finished_at=now()
			WHERE tenant_id=$1 AND id=$2 AND status IN ('EXECUTING','CANCEL_REQUESTED')`, g.session.tenantID, executionID, state, code, answer, hex.EncodeToString(answerSum[:]))
		if err != nil {
			log.Printf("finish execution %s: %v", requestID, err)
		}
		_, err = g.db.Exec(finishCtx, `UPDATE turn SET status=$3,answer=$4 WHERE tenant_id=$1 AND id=$2`, g.session.tenantID, turnID, state, answer)
		if err != nil {
			log.Printf("finish turn %s: %v", requestID, err)
		}
		if state == "DONE" {
			_, err = g.db.Exec(finishCtx, `INSERT INTO message(tenant_id,turn_id,role,content) VALUES($1,$2,'assistant',$3)`, g.session.tenantID, turnID, answer)
			if err != nil {
				log.Printf("store assistant message %s: %v", requestID, err)
			}
		}
	}
	_ = writeEvent("status", map[string]any{"type": "status", "status": "EXECUTING", "is_mock": true})
	base := &ragv1.RequestContext{RequestId: requestID, TenantId: g.session.tenant, UserId: g.session.user,
		ShopId: g.session.shop, Role: g.session.role, AllowedShopIds: []string{g.session.shop}, PermissionRevision: "m1-seed-v1"}
	routed, err := g.rag.Understand(ctx, &ragv1.UnderstandRequest{Context: base, Query: query,
		HistorySummary: g.historySummary(ctx, conversationID, turnID)})
	if err != nil {
		state, code := grpcFailure(ctx, err)
		finish(state, code, "")
		return
	}
	if routed.Intent == "clarification" || strings.TrimSpace(routed.Clarification) != "" && routed.Confidence < 0.5 {
		clarification := routed.Clarification
		if clarification == "" {
			clarification = "请补充更多问题细节。"
		}
		_ = writeEvent("status", map[string]any{"type": "status", "status": "ASKING", "clarification": clarification, "is_mock": true})
		_, _ = g.db.Exec(context.Background(), `UPDATE conversation_execution SET status='ASKING',finish_reason='clarification',finished_at=now()
			WHERE tenant_id=$1 AND id=$2 AND status='EXECUTING'`, g.session.tenantID, executionID)
		_, _ = g.db.Exec(context.Background(), `UPDATE turn SET status='ASKING',intent=$3,information_source=$4
			WHERE tenant_id=$1 AND id=$2`, g.session.tenantID, turnID, routed.Intent, routed.InformationSource)
		_ = writeEvent("completed", map[string]any{"type": "completed", "status": "ASKING", "clarification": clarification, "is_mock": true, "can_copy": false})
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
		for _, item := range part.Evidence {
			if item.ShopId != g.session.shop || item.DisclosureClass != "external_allowed" || !item.CustomerEligible {
				continue
			}
			evidence = append(evidence, item)
		}
		if part.Complete {
			break
		}
	}
	for i, item := range evidence {
		if _, err = g.db.Exec(ctx, `INSERT INTO evidence_snapshot(tenant_id,execution_id,evidence_id,document_id,version_id,source_ref,content,disclosure_class,customer_eligible,citation_index)
			VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT(tenant_id,execution_id,evidence_id) DO NOTHING`,
			g.session.tenantID, executionID, item.Id, item.DocumentId, item.VersionId, item.SourceRef,
			item.Content, item.DisclosureClass, item.CustomerEligible, i+1); err != nil {
			finish("FAILED", "persistence_failed", "")
			return
		}
	}
	if err = writeEvent("evidence", map[string]any{"type": "evidence", "evidence": toEvidence(evidence), "is_mock": true}); err != nil {
		finish("FAILED", "persistence_failed", "")
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
		switch value := part.Event.(type) {
		case *ragv1.GenerateResponse_Delta:
			answer.WriteString(value.Delta)
			if err = writeEvent("delta", map[string]any{"type": "delta", "text": value.Delta}); err != nil {
				finish("FAILED", "persistence_failed", "")
				return
			}
		case *ragv1.GenerateResponse_Citation:
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
			_, _ = g.db.Exec(ctx, `INSERT INTO customer_reply(tenant_id,execution_id,text_plain,is_mock,can_copy,blocked_reason)
				VALUES($1,$2,$3,true,false,'mock') ON CONFLICT(tenant_id,execution_id) DO UPDATE SET text_plain=EXCLUDED.text_plain,is_mock=true,can_copy=false,blocked_reason='mock'`,
				g.session.tenantID, executionID, text)
			finish("DONE", "", text)
			return
		}
	}
}

func (g *gateway) getTurn(w http.ResponseWriter, r *http.Request) {
	var requestID, state string
	var answer *string
	var mock bool
	err := g.db.QueryRow(r.Context(), `SELECT t.request_id,t.status,t.answer,e.is_mock
		FROM turn t JOIN conversation c ON c.tenant_id=t.tenant_id AND c.id=t.conversation_id
		JOIN conversation_execution e ON e.tenant_id=t.tenant_id AND e.turn_id=t.id
		WHERE t.tenant_id=$1 AND c.user_id=$2 AND c.shop_id=$3 AND t.request_id=$4`,
		g.session.tenantID, g.session.userID, g.session.shop, r.PathValue("id")).Scan(&requestID, &state, &answer, &mock)
	if errors.Is(err, pgx.ErrNoRows) {
		writeError(w, 404, "not_found", "turn not found")
		return
	}
	if err != nil {
		writeError(w, 503, "database_unavailable", "could not load turn")
		return
	}
	var items []map[string]any
	rows, err := g.db.Query(r.Context(), `SELECT evidence_id,COALESCE(document_id,''),COALESCE(version_id,''),COALESCE(source_ref,''),content,disclosure_class,customer_eligible,citation_index
		FROM evidence_snapshot es JOIN conversation_execution e ON e.tenant_id=es.tenant_id AND e.id=es.execution_id
		JOIN turn t ON t.tenant_id=e.tenant_id AND t.id=e.turn_id
		JOIN conversation c ON c.tenant_id=t.tenant_id AND c.id=t.conversation_id
		WHERE e.tenant_id=$1 AND e.request_id=$2 AND c.user_id=$3 AND c.shop_id=$4 ORDER BY citation_index`,
		g.session.tenantID, requestID, g.session.userID, g.session.shop)
	if err != nil {
		writeError(w, 503, "database_unavailable", "could not load evidence")
		return
	}
	defer rows.Close()
	items = make([]map[string]any, 0)
	for rows.Next() {
		var id, documentID, versionID, sourceRef, content, disclosure string
		var eligible bool
		var index *int
		if err := rows.Scan(&id, &documentID, &versionID, &sourceRef, &content, &disclosure, &eligible, &index); err != nil {
			writeError(w, 503, "database_unavailable", "could not load evidence")
			return
		}
		items = append(items, map[string]any{"id": id, "title": documentID, "snippet": content, "sourceType": disclosure,
			"documentId": documentID, "versionId": versionID, "sourceRef": sourceRef, "score": 1.0, "citationIndex": index, "customerEligible": eligible})
	}
	var reply *string
	var canCopy bool
	var blocked string
	_ = g.db.QueryRow(r.Context(), `SELECT text_plain,can_copy,blocked_reason FROM customer_reply cr
		JOIN conversation_execution e ON e.tenant_id=cr.tenant_id AND e.id=cr.execution_id
		WHERE e.tenant_id=$1 AND e.request_id=$2`, g.session.tenantID, requestID).Scan(&reply, &canCopy, &blocked)
	if mock {
		canCopy = false
		blocked = "mock"
	}
	writeJSON(w, 200, map[string]any{"request_id": requestID, "status": state, "answer": answer, "is_mock": mock,
		"customer_reply": map[string]any{"text_plain": reply, "can_copy": canCopy, "blocked_reason": blocked}, "evidence": items})
}

func (g *gateway) events(w http.ResponseWriter, r *http.Request) {
	requestID := r.PathValue("id")
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
	requestID := r.PathValue("id")
	tag, err := g.db.Exec(r.Context(), `UPDATE conversation_execution e SET cancel_requested=true,status='CANCEL_REQUESTED'
		FROM turn t,conversation c WHERE e.tenant_id=$1 AND e.request_id=$2 AND e.status='EXECUTING'
		AND t.tenant_id=e.tenant_id AND t.id=e.turn_id AND c.tenant_id=t.tenant_id AND c.id=t.conversation_id
		AND c.user_id=$3 AND c.shop_id=$4`, g.session.tenantID, requestID, g.session.userID, g.session.shop)
	if err != nil {
		writeError(w, 503, "database_unavailable", "could not cancel turn")
		return
	}
	if tag.RowsAffected() == 0 {
		var state string
		err = g.db.QueryRow(r.Context(), `SELECT e.status FROM conversation_execution e
			JOIN turn t ON t.tenant_id=e.tenant_id AND t.id=e.turn_id JOIN conversation c ON c.tenant_id=t.tenant_id AND c.id=t.conversation_id
			WHERE e.tenant_id=$1 AND e.request_id=$2 AND c.user_id=$3 AND c.shop_id=$4`,
			g.session.tenantID, requestID, g.session.userID, g.session.shop).Scan(&state)
		if errors.Is(err, pgx.ErrNoRows) {
			writeError(w, 404, "not_found", "turn not found")
			return
		}
		if err != nil {
			writeError(w, 503, "database_unavailable", "could not read turn status")
			return
		}
		if state == "CANCEL_REQUESTED" {
			writeJSON(w, 200, map[string]any{"request_id": requestID, "status": state})
			return
		}
		writeJSON(w, 409, map[string]any{"request_id": requestID, "status": state})
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
			"sourceRef": item.SourceRef, "score": item.RawScore, "customerEligible": item.CustomerEligible})
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
