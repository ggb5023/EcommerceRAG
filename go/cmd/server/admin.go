package main

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	ragv1 "github.com/ggb5023/EcommerceRAG/go/internal/pb/rag/v1"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/status"
)

const (
	adminRoleObserver = "platform_observer"
	adminRoleAccess   = "access_admin"
	adminRoleMerchant = "merchant_admin"
	adminRoleReviewer = "knowledge_reviewer"
	maxAdminUpload    = 9 * 1024 * 1024
)

type adminScope struct {
	tenantID   int64
	userID     int64
	role       string
	shops      []string
	tenantWide bool
}

type adminConfirmation struct {
	Confirm              bool   `json:"confirm"`
	TenantID             string `json:"tenant_id"`
	ShopID               string `json:"shop_id"`
	DocumentID           string `json:"document_id"`
	VersionHash          string `json:"version_hash"`
	PermissionRevision   string `json:"permission_revision"`
	ExpectedFencingEpoch int64  `json:"expected_fencing_epoch"`
	Reason               string `json:"reason"`
}

func (g *gateway) registerAdminRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /admin/v1/session", g.adminSession)
	mux.HandleFunc("GET /admin/v1/overview", g.adminOverview)
	mux.HandleFunc("GET /admin/v1/access/accounts", g.adminAccounts)
	mux.HandleFunc("GET /admin/v1/access/requests", g.adminListRequests)
	mux.HandleFunc("POST /admin/v1/access/requests", g.adminCreateRequest)
	mux.HandleFunc("POST /admin/v1/access/requests/{id}/decision", g.adminDecideRequest)
	mux.HandleFunc("GET /admin/v1/access/audit", g.adminAudit)
	mux.HandleFunc("POST /admin/v1/access/grants/{id}/revoke", g.adminRevokeGrant)
	mux.HandleFunc("GET /admin/v1/merchant/shops", g.adminShops)
	mux.HandleFunc("GET /admin/v1/merchant/sources", g.adminSources)
	mux.HandleFunc("POST /admin/v1/merchant/sources/import", g.adminImportPackage)
	mux.HandleFunc("GET /admin/v1/merchant/ingestion", g.adminJobs)
	mux.HandleFunc("GET /admin/v1/merchant/ingestion/{id}", g.adminJobDetail)
	mux.HandleFunc("POST /admin/v1/merchant/ingestion/{id}/cancel", g.adminCancelIngestJob)
	mux.HandleFunc("POST /admin/v1/merchant/ingestion/{id}/retry", g.adminRetryIngestJob)
	mux.HandleFunc("GET /admin/v1/merchant/reviews", g.adminReviews)
	mux.HandleFunc("GET /admin/v1/merchant/reviews/{id}", g.adminReviewDetail)
	mux.HandleFunc("POST /admin/v1/merchant/versions/{id}/review", g.adminReviewVersion)
	mux.HandleFunc("POST /admin/v1/merchant/versions/{id}/publish", g.adminPublishVersion)
	mux.HandleFunc("POST /admin/v1/merchant/versions/{id}/rollback", g.adminRollbackVersion)
	mux.HandleFunc("POST /admin/v1/merchant/versions/{id}/revoke", g.adminRevokeVersion)
	mux.HandleFunc("GET /admin/v1/merchant/versions", g.adminVersions)
}

func (g *gateway) requireAdmin(w http.ResponseWriter, r *http.Request, roles ...string) (adminScope, bool) {
	if g.db == nil {
		writeError(w, http.StatusForbidden, "admin_scope_denied", "administrative permission is required")
		return adminScope{}, false
	}
	if err := g.refreshAuthorization(r.Context()); err != nil {
		writeError(w, authorizationHTTPStatus(err), authorizationFailureCode(err), "authorization failed")
		return adminScope{}, false
	}
	shopID := strings.TrimSpace(r.URL.Query().Get("shop_id"))
	explicitShop := shopID != ""
	if shopID == "" {
		shopID = g.session.shop
	}
	if shopID != "" && !contains(g.session.allowedShops, shopID) {
		writeError(w, http.StatusForbidden, "admin_scope_denied", "shop is outside the current identity scope")
		return adminScope{}, false
	}
	for _, role := range roles {
		var scopeKind string
		var shops []string
		err := g.db.QueryRow(r.Context(), `SELECT scope_kind,shop_ids FROM admin_role_grant
			WHERE tenant_id=$1 AND user_id=$2 AND role=$3 AND status='active'
			AND EXISTS(SELECT 1 FROM app_user u JOIN tenant t ON t.id=u.tenant_id
				WHERE u.tenant_id=$1 AND u.id=$2 AND u.revoked_at IS NULL AND t.status='active')
			ORDER BY CASE scope_kind WHEN 'tenant' THEN 0 ELSE 1 END LIMIT 1`,
			g.session.tenantID, g.session.userID, role).Scan(&scopeKind, &shops)
		if err == nil && (scopeKind == "tenant" || contains(shops, shopID)) {
			if err = g.refreshAuthorization(r.Context()); err != nil {
				writeError(w, authorizationHTTPStatus(err), authorizationFailureCode(err), "authorization failed")
				return adminScope{}, false
			}
			effectiveShops := make([]string, 0, len(g.session.allowedShops))
			for _, allowedShop := range g.session.allowedShops {
				if scopeKind == "tenant" || contains(shops, allowedShop) {
					effectiveShops = append(effectiveShops, allowedShop)
				}
			}
			if explicitShop {
				effectiveShops = []string{shopID}
			}
			return adminScope{tenantID: g.session.tenantID, userID: g.session.userID,
				role: role, shops: effectiveShops, tenantWide: scopeKind == "tenant"}, true
		}
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			writeError(w, http.StatusServiceUnavailable, "authorization_unavailable", "administrative authorization is unavailable")
			return adminScope{}, false
		}
	}
	writeError(w, http.StatusForbidden, "admin_scope_denied", "administrative permission is required")
	return adminScope{}, false
}

func (g *gateway) permissionRequestScope(w http.ResponseWriter, r *http.Request) (adminScope, bool, bool) {
	if err := g.refreshAuthorization(r.Context()); err != nil {
		writeError(w, authorizationHTTPStatus(err), authorizationFailureCode(err), "authorization failed")
		return adminScope{}, false, false
	}
	shopID := strings.TrimSpace(r.URL.Query().Get("shop_id"))
	explicitShop := shopID != ""
	if shopID == "" {
		shopID = g.session.shop
	}
	if shopID != "" && !contains(g.session.allowedShops, shopID) {
		writeError(w, http.StatusForbidden, "admin_scope_denied", "shop is outside the current identity scope")
		return adminScope{}, false, false
	}
	var scopeKind string
	var shops []string
	err := g.db.QueryRow(r.Context(), `SELECT scope_kind,shop_ids FROM admin_role_grant
		WHERE tenant_id=$1 AND user_id=$2 AND role=ANY($3::text[]) AND status='active'
		AND EXISTS(SELECT 1 FROM app_user u JOIN tenant t ON t.id=u.tenant_id
			WHERE u.tenant_id=$1 AND u.id=$2 AND u.revoked_at IS NULL AND t.status='active')
		AND (scope_kind='tenant' OR $4=ANY(shop_ids))
		ORDER BY CASE role WHEN 'access_admin' THEN 0 ELSE 1 END,
			CASE scope_kind WHEN 'tenant' THEN 0 ELSE 1 END LIMIT 1`,
		g.session.tenantID, g.session.userID, []string{adminRoleAccess, adminRoleMerchant}, shopID).Scan(&scopeKind, &shops)
	if errors.Is(err, pgx.ErrNoRows) {
		return adminScope{tenantID: g.session.tenantID, userID: g.session.userID}, false, true
	}
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, "authorization_unavailable", "permission request scope is unavailable")
		return adminScope{}, false, false
	}
	if err = g.refreshAuthorization(r.Context()); err != nil {
		writeError(w, authorizationHTTPStatus(err), authorizationFailureCode(err), "authorization failed")
		return adminScope{}, false, false
	}
	effectiveShops := make([]string, 0, len(g.session.allowedShops))
	for _, allowedShop := range g.session.allowedShops {
		if scopeKind == "tenant" || contains(shops, allowedShop) {
			effectiveShops = append(effectiveShops, allowedShop)
		}
	}
	if explicitShop {
		effectiveShops = []string{shopID}
	}
	return adminScope{tenantID: g.session.tenantID, userID: g.session.userID,
		shops: effectiveShops, tenantWide: scopeKind == "tenant"}, true, true
}

func (g *gateway) adminSession(w http.ResponseWriter, r *http.Request) {
	if _, ok := g.requireAdmin(w, r, adminRoleObserver, adminRoleAccess, adminRoleMerchant, adminRoleReviewer); !ok {
		return
	}
	roles := make([]string, 0)
	rows, err := g.db.Query(r.Context(), `SELECT role FROM admin_role_grant WHERE tenant_id=$1 AND user_id=$2
		AND status='active' ORDER BY role`, g.session.tenantID, g.session.userID)
	if err != nil {
		writeError(w, 503, "authorization_unavailable", "could not load administrative session")
		return
	}
	for rows.Next() {
		var role string
		if rows.Scan(&role) != nil {
			rows.Close()
			writeError(w, 503, "authorization_unavailable", "could not load administrative session")
			return
		}
		roles = append(roles, role)
	}
	if err = rows.Err(); err != nil {
		rows.Close()
		writeError(w, 503, "authorization_unavailable", "could not load administrative session")
		return
	}
	rows.Close()
	catalog := make([]map[string]any, 0)
	catalogRows, err := g.db.Query(r.Context(), `SELECT role_key,storage_role,display_name,catalog_version,enabled,requestable
		FROM admin_role_catalog ORDER BY role_key`)
	if err != nil {
		writeError(w, 503, "authorization_unavailable", "could not load administrative role catalog")
		return
	}
	for catalogRows.Next() {
		var roleKey, displayName, catalogVersion string
		var storageRole *string
		var enabled, requestable bool
		if err = catalogRows.Scan(&roleKey, &storageRole, &displayName, &catalogVersion, &enabled, &requestable); err != nil {
			catalogRows.Close()
			writeError(w, 503, "authorization_unavailable", "could not load administrative role catalog")
			return
		}
		catalog = append(catalog, map[string]any{
			"role_key": roleKey, "storage_role": storageRole, "display_name": displayName,
			"catalog_version": catalogVersion, "enabled": enabled, "requestable": requestable,
		})
	}
	if err = catalogRows.Err(); err != nil {
		catalogRows.Close()
		writeError(w, 503, "authorization_unavailable", "could not load administrative role catalog")
		return
	}
	catalogRows.Close()
	writeJSON(w, 200, map[string]any{"tenant_id": g.session.tenant, "user_id": g.session.user,
		"runtime_role": g.session.role, "shop_id": g.session.shop, "allowed_shop_ids": g.session.allowedShops,
		"permission_revision": g.currentPermissionRevision(), "admin_roles": roles,
		"admin_role_catalog": catalog, "is_mock": true})
}

func (g *gateway) adminOverview(w http.ResponseWriter, r *http.Request) {
	scope, ok := g.requireAdmin(w, r, adminRoleObserver, adminRoleAccess, adminRoleMerchant, adminRoleReviewer)
	if !ok {
		return
	}
	components := map[string]any{
		"go":         map[string]any{"status": "healthy", "version": envOr("APP_VERSION", "development")},
		"postgresql": map[string]any{"status": "healthy"},
		"redis":      map[string]any{"status": "not_configured"},
		"python":     map[string]any{"status": "unknown"},
	}
	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
	defer cancel()
	if err := g.db.Ping(ctx); err != nil {
		components["postgresql"] = map[string]any{"status": "unavailable"}
	}
	if g.grpcConn != nil {
		healthClient := grpc_health_v1.NewHealthClient(g.grpcConn)
		healthCtx, healthCancel := context.WithTimeout(ctx, 1200*time.Millisecond)
		response, err := healthClient.Check(healthCtx, &grpc_health_v1.HealthCheckRequest{})
		healthCancel()
		state := "unavailable"
		if err == nil && response.Status == grpc_health_v1.HealthCheckResponse_SERVING {
			state = "healthy"
		}
		components["python"] = map[string]any{"status": state, "role": "parser_and_rag_rpc"}
	}
	if address := strings.TrimSpace(os.Getenv("REDIS_ADDR")); address != "" {
		redisState := "unavailable"
		if pingRedis(ctx, address, os.Getenv("REDIS_USERNAME"), os.Getenv("REDIS_PASSWORD")) == nil {
			redisState = "healthy"
		}
		components["redis"] = map[string]any{"status": redisState}
	}
	var workerRows, activeWorkers, degradedWorkers int64
	var workerHeartbeat *time.Time
	var workerErrorCode *string
	err := g.db.QueryRow(ctx, `SELECT count(*),
		count(*) FILTER (WHERE heartbeat_at>=now()-interval '30 seconds' AND status='working'),
		max(heartbeat_at),
		count(*) FILTER (WHERE heartbeat_at>=now()-interval '30 seconds' AND status='degraded'),
		max(last_error_code) FILTER (WHERE heartbeat_at>=now()-interval '30 seconds' AND status='degraded')
		FROM ingest_worker_heartbeat`).Scan(&workerRows, &activeWorkers, &workerHeartbeat,
		&degradedWorkers, &workerErrorCode)
	if err != nil {
		writeError(w, 503, "status_unavailable", "could not load ingestion worker status")
		return
	}
	workerStatus := "not_configured"
	workerDetail := "no ingestion worker heartbeat has been recorded"
	if workerRows > 0 {
		workerStatus = "unavailable"
		workerDetail = "last ingestion worker heartbeat is stale"
	}
	if workerHeartbeat != nil && time.Since(*workerHeartbeat) < 30*time.Second {
		workerStatus = "healthy"
		workerDetail = fmt.Sprintf("%d active job(s)", activeWorkers)
		if degradedWorkers > 0 {
			workerStatus = "degraded"
			workerDetail = fmt.Sprintf("%d worker(s) report an error", degradedWorkers)
		}
	}
	components["worker"] = map[string]any{"status": workerStatus, "detail": workerDetail,
		"active_jobs": activeWorkers, "heartbeat_at": workerHeartbeat, "last_error_code": workerErrorCode}
	if g.profile == "synthetic_import_mock" {
		indexState := "healthy"
		if !g.adminIndexReady.Load() {
			indexState = "unavailable"
		}
		components["published_index"] = map[string]any{"status": indexState}
	}
	var migrationVersion string
	if err := g.db.QueryRow(ctx, `SELECT COALESCE(max(version),'none') FROM schema_migration`).Scan(&migrationVersion); err != nil {
		writeError(w, 503, "status_unavailable", "could not load migration status")
		return
	}
	var legacyMigrations int64
	if err := g.db.QueryRow(ctx, `SELECT count(*) FROM schema_migration WHERE checksum_sha256 IS NULL`).Scan(&legacyMigrations); err != nil {
		writeError(w, 503, "status_unavailable", "could not load migration integrity status")
		return
	}
	var jobs, pending, failed, total, errorsLast, executionsLast int64
	err = g.db.QueryRow(ctx, `SELECT count(*),count(*) FILTER (WHERE j.status IN ('pending','running','awaiting_review')),
		count(*) FILTER (WHERE j.status='failed') FROM ingest_job j JOIN source s ON s.tenant_id=j.tenant_id AND s.id=j.source_id
		WHERE j.tenant_id=$1 AND s.shop_id=ANY($2::text[])`, g.session.tenantID, scope.shops).
		Scan(&jobs, &pending, &failed)
	if err != nil {
		writeError(w, 503, "status_unavailable", "could not load ingestion status")
		return
	}
	err = g.db.QueryRow(ctx, `SELECT count(*),count(*) FILTER (WHERE e.status IN ('FAILED','failed')) FROM conversation_execution e
		JOIN turn t ON t.tenant_id=e.tenant_id AND t.id=e.turn_id
		JOIN conversation c ON c.tenant_id=t.tenant_id AND c.id=t.conversation_id
		WHERE e.tenant_id=$1 AND c.shop_id=ANY($2::text[]) AND e.created_at >= now()-interval '15 minutes'`,
		g.session.tenantID, scope.shops).Scan(&total, &errorsLast)
	if err != nil {
		writeError(w, 503, "status_unavailable", "could not load error rate")
		return
	}
	versionRows, err := g.db.Query(ctx, `SELECT v.status,count(*) FROM document_version v
		JOIN document d ON d.tenant_id=v.tenant_id AND d.id=v.document_id
		JOIN source s ON s.tenant_id=d.tenant_id AND s.id=d.source_id
		WHERE v.tenant_id=$1 AND s.shop_id=ANY($2::text[]) AND s.type='admin_manifest'
		GROUP BY v.status`, g.session.tenantID, scope.shops)
	if err != nil {
		writeError(w, 503, "status_unavailable", "could not load version status")
		return
	}
	versionCounts := make(map[string]int64)
	var versionTotal, activeVersions int64
	for versionRows.Next() {
		var state string
		var count int64
		if versionRows.Scan(&state, &count) != nil {
			versionRows.Close()
			writeError(w, 503, "status_unavailable", "could not load version status")
			return
		}
		versionCounts[state] = count
		versionTotal += count
		if state == "active" {
			activeVersions = count
		}
	}
	if err = versionRows.Err(); err != nil {
		versionRows.Close()
		writeError(w, 503, "status_unavailable", "could not load version status")
		return
	}
	versionRows.Close()
	executionsLast = total
	errorRate := float64(0)
	if executionsLast > 0 {
		errorRate = float64(errorsLast) / float64(executionsLast)
	}
	if err = g.refreshAuthorization(ctx); err != nil {
		writeError(w, authorizationHTTPStatus(err), authorizationFailureCode(err), "authorization failed")
		return
	}
	writeJSON(w, 200, map[string]any{
		"components": components,
		"migration":  map[string]any{"current_version": migrationVersion, "status": "read_only", "legacy_unverified": legacyMigrations},
		"ingestion":  map[string]any{"jobs": jobs, "awaiting_or_running": pending, "failed": failed},
		"versions":   map[string]any{"total": versionTotal, "active": activeVersions, "by_status": versionCounts},
		"version": map[string]string{"application": envOr("APP_VERSION", "development"),
			"revision": envOr("DEPLOYED_REVISION", "unknown")},
		"error_rate": map[string]any{"window_minutes": 15, "requests": executionsLast, "errors": errorsLast, "rate": errorRate},
		"scope":      map[string]any{"tenant_id": g.session.tenant, "shop_id": g.session.shop},
	})
}

func pingRedis(ctx context.Context, address, username, password string) error {
	dialer := net.Dialer{Timeout: 700 * time.Millisecond}
	conn, err := dialer.DialContext(ctx, "tcp", address)
	if err != nil {
		return err
	}
	defer conn.Close()
	reader := bufio.NewReader(conn)
	_ = conn.SetDeadline(time.Now().Add(700 * time.Millisecond))
	if password != "" {
		args := []string{"AUTH", password}
		if username != "" {
			args = []string{"AUTH", username, password}
		}
		if _, err = io.WriteString(conn, redisCommand(args...)); err != nil {
			return err
		}
		if _, err = reader.ReadString('\n'); err != nil {
			return err
		}
	}
	if _, err = io.WriteString(conn, redisCommand("PING")); err != nil {
		return err
	}
	line, err := reader.ReadString('\n')
	if err != nil || !strings.Contains(line, "PONG") {
		return errors.New("redis health check failed")
	}
	return nil
}

func redisCommand(args ...string) string {
	var builder strings.Builder
	fmt.Fprintf(&builder, "*%d\r\n", len(args))
	for _, arg := range args {
		fmt.Fprintf(&builder, "$%d\r\n%s\r\n", len(arg), arg)
	}
	return builder.String()
}

func (g *gateway) adminAccounts(w http.ResponseWriter, r *http.Request) {
	scope, ok := g.requireAdmin(w, r, adminRoleAccess)
	if !ok {
		return
	}
	rows, err := g.db.Query(r.Context(), `SELECT u.id,u.external_id,COALESCE(NULLIF(u.display_name,''),u.external_id),u.role,COALESCE(u.shop_ids,ARRAY[]::text[]),
		u.permission_revision,u.revoked_at,COALESCE(jsonb_agg(jsonb_build_object('id',gr.id::text,'role',gr.role,
		'scope_kind',gr.scope_kind,'shop_ids',gr.shop_ids,'status',gr.status)) FILTER (WHERE gr.id IS NOT NULL),'[]'::jsonb)
		FROM app_user u LEFT JOIN admin_role_grant gr ON gr.tenant_id=u.tenant_id AND gr.user_id=u.id
		WHERE u.tenant_id=$1 AND ($3::boolean OR COALESCE(u.shop_ids,ARRAY[]::text[]) && $2::text[])
		GROUP BY u.id ORDER BY u.id LIMIT 500`, g.session.tenantID, scope.shops, scope.tenantWide)
	if err != nil {
		writeError(w, 503, "database_unavailable", "could not load account summaries")
		return
	}
	defer rows.Close()
	items := make([]map[string]any, 0)
	for rows.Next() {
		var id int64
		var externalID, displayName, role, revision string
		var shops []string
		var grants []byte
		var revoked *time.Time
		if rows.Scan(&id, &externalID, &displayName, &role, &shops, &revision, &revoked, &grants) != nil {
			writeError(w, 503, "database_unavailable", "could not load account summaries")
			return
		}
		items = append(items, map[string]any{"user_id": strconv.FormatInt(id, 10), "external_id": externalID,
			"display_name": displayName, "runtime_role": role, "shop_ids": shops,
			"permission_revision": revision, "revoked": revoked != nil, "grants": json.RawMessage(grants)})
	}
	writeJSON(w, 200, map[string]any{"items": items})
}

func (g *gateway) adminListRequests(w http.ResponseWriter, r *http.Request) {
	scope, canManageRequests, ok := g.permissionRequestScope(w, r)
	if !ok {
		return
	}
	statusFilter := strings.TrimSpace(r.URL.Query().Get("status"))
	rows, err := g.db.Query(r.Context(), `SELECT q.id,q.requested_by,COALESCE(u.display_name,u.external_id),q.role,q.scope_kind,
		q.shop_ids,q.reason,q.status,q.reviewed_by,q.reviewed_at,q.decision_reason,q.created_at,q.payload_hash
		FROM admin_permission_request q JOIN app_user u ON u.tenant_id=q.tenant_id AND u.id=q.requested_by
		WHERE q.tenant_id=$1 AND ($2='' OR q.status=$2)
		AND (($5::boolean AND (($4::boolean AND q.scope_kind='tenant') OR (q.scope_kind='shops' AND q.shop_ids && $3::text[])))
			OR (NOT $5::boolean AND q.requested_by=$6))
		ORDER BY q.created_at DESC LIMIT 200`, g.session.tenantID, statusFilter, scope.shops,
		scope.tenantWide, canManageRequests, g.session.userID)
	if err != nil {
		writeError(w, 503, "database_unavailable", "could not load permission requests")
		return
	}
	defer rows.Close()
	items := make([]map[string]any, 0)
	for rows.Next() {
		var id, requester int64
		var reviewer *int64
		var reviewedAt *time.Time
		var displayName, role, scopeKind, reason, state, hash string
		var decision *string
		var shops []string
		var createdAt time.Time
		if rows.Scan(&id, &requester, &displayName, &role, &scopeKind, &shops, &reason, &state,
			&reviewer, &reviewedAt, &decision, &createdAt, &hash) != nil {
			writeError(w, 503, "database_unavailable", "could not load permission requests")
			return
		}
		items = append(items, map[string]any{"id": strconv.FormatInt(id, 10), "requested_by": strconv.FormatInt(requester, 10),
			"requester_name": displayName, "role": role, "scope_kind": scopeKind, "shop_ids": shops,
			"reason": reason, "status": state, "reviewed_by": reviewer, "reviewed_at": reviewedAt,
			"decision_reason": decision, "created_at": createdAt, "payload_hash": hash})
	}
	writeJSON(w, 200, map[string]any{"items": items})
}

func (g *gateway) adminCreateRequest(w http.ResponseWriter, r *http.Request) {
	if err := g.refreshAuthorization(r.Context()); err != nil {
		writeError(w, authorizationHTTPStatus(err), authorizationFailureCode(err), "authorization failed")
		return
	}
	var in struct {
		Role   string   `json:"role"`
		Scope  string   `json:"scope_kind"`
		Shops  []string `json:"shop_ids"`
		Reason string   `json:"reason"`
	}
	if err := decodeJSON(r, &in); err != nil || !validAdminRequest(in.Role, in.Scope, in.Shops, in.Reason, g.session.allowedShops) {
		writeError(w, 400, "invalid_request", "permission request is invalid")
		return
	}
	var requestable bool
	if err := g.db.QueryRow(r.Context(), `SELECT requestable FROM admin_role_catalog
		WHERE storage_role=$1 AND enabled`, in.Role).Scan(&requestable); errors.Is(err, pgx.ErrNoRows) {
		writeError(w, 400, "role_not_requestable", "the requested administrative role is not available")
		return
	} else if err != nil {
		writeError(w, 503, "role_catalog_unavailable", "administrative role catalog is unavailable")
		return
	} else if !requestable {
		writeError(w, 400, "role_not_requestable", "the requested administrative role cannot be self-requested")
		return
	}
	if in.Shops == nil {
		in.Shops = []string{}
	}
	canonical, _ := json.Marshal(map[string]any{"tenant_id": g.session.tenantID, "user_id": g.session.userID,
		"role": in.Role, "scope_kind": in.Scope, "shop_ids": in.Shops, "reason": strings.TrimSpace(in.Reason)})
	sum := sha256.Sum256(canonical)
	tx, err := g.db.BeginTx(r.Context(), pgx.TxOptions{})
	if err != nil {
		writeError(w, 503, "request_create_failed", "could not start permission request")
		return
	}
	defer tx.Rollback(r.Context())
	var id int64
	err = tx.QueryRow(r.Context(), `INSERT INTO admin_permission_request(tenant_id,requested_by,role,scope_kind,shop_ids,reason,payload_hash)
		VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING id`, g.session.tenantID, g.session.userID,
		in.Role, in.Scope, in.Shops, strings.TrimSpace(in.Reason), hex.EncodeToString(sum[:])).Scan(&id)
	if err != nil {
		writeError(w, 503, "request_create_failed", "could not create permission request")
		return
	}
	if err = auditAdmin(r.Context(), tx, g.session.tenantID, g.session.userID, "permission.request.create",
		"admin_permission_request", strconv.FormatInt(id, 10), "", "", 0, strings.TrimSpace(in.Reason), "success",
		map[string]any{"role": in.Role, "scope_kind": in.Scope, "shop_ids": in.Shops}); err != nil {
		writeError(w, 503, "request_create_failed", "could not record permission request")
		return
	}
	if err = tx.Commit(r.Context()); err != nil {
		writeError(w, 503, "request_create_failed", "could not commit permission request")
		return
	}
	writeJSON(w, 201, map[string]any{"id": strconv.FormatInt(id, 10), "status": "pending"})
}

func (g *gateway) adminDecideRequest(w http.ResponseWriter, r *http.Request) {
	scope, ok := g.requireAdmin(w, r, adminRoleAccess, adminRoleMerchant)
	if !ok {
		return
	}
	var in struct {
		Decision string `json:"decision"`
		Reason   string `json:"reason"`
		Confirm  bool   `json:"confirm"`
	}
	if err := decodeJSON(r, &in); err != nil || !in.Confirm || !contains([]string{"approved", "rejected", "needs_revision"}, in.Decision) || len(strings.TrimSpace(in.Reason)) < 10 {
		writeError(w, 400, "invalid_decision", "decision and reason are required")
		return
	}
	requestID, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil {
		writeError(w, 404, "not_found", "permission request not found")
		return
	}
	tx, err := g.db.BeginTx(r.Context(), pgx.TxOptions{})
	if err != nil {
		writeError(w, 503, "database_unavailable", "decision could not be started")
		return
	}
	defer tx.Rollback(r.Context())
	var targetUser int64
	var role, scopeKind string
	var shops []string
	err = tx.QueryRow(r.Context(), `SELECT requested_by,role,scope_kind,shop_ids FROM admin_permission_request
		WHERE tenant_id=$1 AND id=$2 AND status='pending' AND requested_by<>$3`,
		g.session.tenantID, requestID, g.session.userID).Scan(&targetUser, &role, &scopeKind, &shops)
	if errors.Is(err, pgx.ErrNoRows) {
		writeError(w, 409, "request_conflict", "request is unavailable or already decided")
		return
	}
	if err != nil {
		writeError(w, 503, "database_unavailable", "permission request could not be loaded")
		return
	}
	if (scopeKind == "tenant" && !scope.tenantWide) || (scopeKind == "shops" && !allContained(scope.shops, shops)) {
		writeError(w, 403, "admin_scope_denied", "request scope is outside the current administrative grant")
		return
	}
	if in.Decision == "approved" {
		if _, err = tx.Exec(r.Context(), `SELECT ecr_issue_admin_grant($1,$2,$3,$4,$5,$6,$7,$8)`,
			g.session.tenantID, targetUser, role, scopeKind, shops, g.session.userID, requestID, strings.TrimSpace(in.Reason)); err != nil {
			writeError(w, databaseDecisionStatus(err), "request_conflict", "permission grant could not be issued")
			return
		}
	} else {
		_, err = tx.Exec(r.Context(), `SELECT ecr_decide_admin_request($1,$2,$3,$4,$5)`,
			g.session.tenantID, requestID, g.session.userID, in.Decision, strings.TrimSpace(in.Reason))
	}
	if err != nil {
		writeError(w, 503, "decision_failed", "permission decision could not be recorded")
		return
	}
	if err = tx.Commit(r.Context()); err != nil {
		writeError(w, 503, "decision_failed", "permission decision could not be committed")
		return
	}
	writeJSON(w, 200, map[string]any{"id": strconv.FormatInt(requestID, 10), "status": in.Decision,
		"audit_result": "success"})
}

func (g *gateway) adminAudit(w http.ResponseWriter, r *http.Request) {
	scope, ok := g.requireAdmin(w, r, adminRoleAccess, adminRoleMerchant, adminRoleReviewer)
	if !ok {
		return
	}
	rows, err := g.db.Query(r.Context(), `SELECT id,actor_user_id,action,resource_type,resource_id,resource_version,
		content_hash,fencing_epoch,reason,result,metadata_json,created_at FROM admin_audit
		WHERE tenant_id=$1 AND (metadata_json->>'shop_id'=ANY($2::text[])
			OR COALESCE(metadata_json->'shop_ids','[]'::jsonb) ?| $2::text[] OR $3::boolean)
		ORDER BY created_at DESC,id DESC LIMIT 200`, g.session.tenantID, scope.shops, scope.tenantWide)
	if err != nil {
		writeError(w, 503, "database_unavailable", "could not load audit records")
		return
	}
	defer rows.Close()
	items := make([]map[string]any, 0)
	for rows.Next() {
		var id, actor int64
		var action, kind, resourceID, result string
		var version, hash, reason *string
		var fence *int64
		var metadata []byte
		var created time.Time
		if rows.Scan(&id, &actor, &action, &kind, &resourceID, &version, &hash, &fence, &reason, &result, &metadata, &created) != nil {
			writeError(w, 503, "database_unavailable", "could not load audit records")
			return
		}
		items = append(items, map[string]any{"id": strconv.FormatInt(id, 10), "actor_user_id": strconv.FormatInt(actor, 10),
			"action": action, "resource_type": kind, "resource_id": resourceID, "resource_version": version,
			"content_hash": hash, "fencing_epoch": fence, "reason": reason, "result": result,
			"metadata": json.RawMessage(metadata), "created_at": created})
	}
	writeJSON(w, 200, map[string]any{"items": items})
}

func (g *gateway) adminRevokeGrant(w http.ResponseWriter, r *http.Request) {
	if _, ok := g.requireAdmin(w, r, adminRoleAccess); !ok {
		return
	}
	var in struct {
		Reason  string `json:"reason"`
		Confirm bool   `json:"confirm"`
	}
	if err := decodeJSON(r, &in); err != nil || !in.Confirm || len(strings.TrimSpace(in.Reason)) < 10 {
		writeError(w, 400, "invalid_request", "revocation reason is required")
		return
	}
	id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil {
		writeError(w, 404, "not_found", "grant not found")
		return
	}
	_, err = g.db.Exec(r.Context(), `SELECT ecr_revoke_admin_grant($1,$2,$3,$4)`, g.session.tenantID, id, g.session.userID, strings.TrimSpace(in.Reason))
	if err != nil {
		writeError(w, databaseDecisionStatus(err), "grant_revoke_failed", "grant could not be revoked")
		return
	}
	writeJSON(w, 200, map[string]any{"id": strconv.FormatInt(id, 10), "status": "revoked", "audit_result": "success"})
}

func (g *gateway) adminShops(w http.ResponseWriter, r *http.Request) {
	scope, ok := g.requireAdmin(w, r, adminRoleMerchant, adminRoleReviewer, adminRoleAccess, adminRoleObserver)
	if !ok {
		return
	}
	rows, err := g.db.Query(r.Context(), `SELECT id,name,status FROM shop WHERE tenant_id=$1 AND id=ANY($2::text[]) ORDER BY id`,
		g.session.tenantID, scope.shops)
	if err != nil {
		writeError(w, 503, "database_unavailable", "could not load shops")
		return
	}
	defer rows.Close()
	items := make([]map[string]string, 0)
	for rows.Next() {
		var id, name, state string
		if rows.Scan(&id, &name, &state) != nil {
			writeError(w, 503, "database_unavailable", "could not load shops")
			return
		}
		items = append(items, map[string]string{"shop_id": id, "name": name, "status": state})
	}
	writeJSON(w, 200, map[string]any{"items": items})
}

func (g *gateway) adminSources(w http.ResponseWriter, r *http.Request) {
	scope, ok := g.requireAdmin(w, r, adminRoleMerchant, adminRoleReviewer, adminRoleObserver)
	if !ok {
		return
	}
	rows, err := g.db.Query(r.Context(), `SELECT s.id,s.shop_id,s.type,CASE WHEN s.type='admin_manifest' THEN s.uri ELSE NULL END,s.created_at,
		(SELECT count(*) FROM document d WHERE d.tenant_id=s.tenant_id AND d.source_id=s.id) AS documents,
		(SELECT max(sm.created_at) FROM source_manifest sm WHERE sm.tenant_id=s.tenant_id AND sm.source_id=s.id) AS latest_import
		FROM source s WHERE s.tenant_id=$1 AND s.shop_id=ANY($2::text[])
		AND s.type<>'synthetic-acl' ORDER BY s.created_at DESC LIMIT 300`, g.session.tenantID, scope.shops)
	if err != nil {
		writeError(w, 503, "database_unavailable", "could not load sources")
		return
	}
	defer rows.Close()
	items := make([]map[string]any, 0)
	for rows.Next() {
		var id, docs int64
		var shop, kind string
		var uri *string
		var created time.Time
		var latest *time.Time
		if rows.Scan(&id, &shop, &kind, &uri, &created, &docs, &latest) != nil {
			writeError(w, 503, "database_unavailable", "could not load sources")
			return
		}
		items = append(items, map[string]any{"source_id": strconv.FormatInt(id, 10), "shop_id": shop,
			"type": kind, "uri": uri, "documents": docs, "created_at": created, "latest_import": latest})
	}
	writeJSON(w, 200, map[string]any{"items": items})
}

func (g *gateway) adminImportPackage(w http.ResponseWriter, r *http.Request) {
	scope, ok := g.requireAdmin(w, r, adminRoleMerchant)
	if !ok {
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxAdminUpload)
	if err := r.ParseMultipartForm(maxAdminUpload); err != nil {
		writeError(w, 400, "upload_too_large", "manifest package is invalid or too large")
		return
	}
	shopID := strings.TrimSpace(r.FormValue("shop_id"))
	if !contains(scope.shops, shopID) {
		writeError(w, 403, "admin_scope_denied", "shop is outside the current identity scope")
		return
	}
	manifest := r.FormValue("manifest")
	if strings.TrimSpace(manifest) == "" || len(manifest) > 256*1024 {
		writeError(w, 400, "invalid_manifest", "manifest is required and must be under 256 KiB")
		return
	}
	paths := r.MultipartForm.Value["paths"]
	fileHeaders := r.MultipartForm.File["files"]
	if len(fileHeaders) == 0 || len(fileHeaders) != len(paths) || len(fileHeaders) > 100 {
		writeError(w, 400, "invalid_package", "manifest files do not match package paths")
		return
	}
	files := make([]*ragv1.PackageFile, 0, len(fileHeaders))
	for index, header := range fileHeaders {
		if header.Size > 8*1024*1024 {
			writeError(w, 413, "file_too_large", "a manifest file exceeds 8 MiB")
			return
		}
		file, err := header.Open()
		if err != nil {
			writeError(w, 400, "invalid_package", "an uploaded file could not be read")
			return
		}
		content, readErr := io.ReadAll(io.LimitReader(file, 8*1024*1024+1))
		_ = file.Close()
		if readErr != nil || len(content) > 8*1024*1024 {
			writeError(w, 413, "file_too_large", "a manifest file exceeds 8 MiB")
			return
		}
		files = append(files, &ragv1.PackageFile{Path: paths[index], Content: content})
	}
	queued, err := g.queuePackage(r.Context(), manifest, files, g.session.tenantID, g.session.userID, shopID)
	if err != nil {
		if status.Code(err) == codes.InvalidArgument {
			writeError(w, 400, "invalid_package", "manifest package failed validation")
			return
		}
		if status.Code(err) == codes.PermissionDenied {
			writeError(w, 403, "admin_scope_denied", "submitter authorization changed")
			return
		}
		var conflict *adminConflictError
		if errors.As(err, &conflict) {
			writeError(w, 409, "version_conflict", conflict.Error())
			return
		}
		writeError(w, 503, "ingestion_queue_failed", "package could not be queued")
		return
	}
	if err = g.refreshAuthorization(r.Context()); err != nil {
		writeError(w, authorizationHTTPStatus(err), authorizationFailureCode(err), "authorization failed")
		return
	}
	writeJSON(w, http.StatusAccepted, map[string]any{"job_id": strconv.FormatInt(queued.JobID, 10),
		"manifest_sha256": queued.ManifestSHA256, "package_sha256": queued.PackageSHA256,
		"item_count": queued.ItemCount, "status": queued.Status, "is_mock": true})
}

type adminConflictError struct{ message string }

func (e *adminConflictError) Error() string { return e.message }

func (g *gateway) persistParsedPackage(ctx context.Context, parsed *ragv1.ParsePackageResponse, manifest string,
	files []*ragv1.PackageFile, tenantID, userID int64, shopID string) error {
	claim := &ingestClaim{
		TenantID: tenantID, ShopID: shopID, ManifestSHA: "",
	}
	if parsed != nil {
		claim.ManifestSHA = parsed.ManifestSha256
	}
	for index, file := range files {
		if file == nil {
			return &ingestParseQualityError{code: "parse_file_invalid", message: "parser file binding is invalid"}
		}
		claimFile := ingestClaimFile{ItemID: int64(index + 1), Path: file.Path, Content: file.Content}
		for _, document := range parsed.GetDocuments() {
			if document != nil && document.Path == file.Path {
				claimFile.DocumentID = document.DocumentId
				claimFile.SourceHash = document.SourceHash
				break
			}
		}
		claim.Files = append(claim.Files, claimFile)
	}
	if err := validateParsedPackage(claim, parsed); err != nil {
		return err
	}
	tx, err := g.db.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	key := "admin:" + parsed.SourceId + ":" + parsed.ManifestSha256
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtext($1)::bigint)`, key); err != nil {
		return err
	}
	var existingJob int64
	err = tx.QueryRow(ctx, `SELECT id FROM ingest_job WHERE tenant_id=$1 AND idempotency_key=$2`, tenantID, key).Scan(&existingJob)
	if err == nil {
		return tx.Commit(ctx)
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return err
	}
	var sourceID int64
	err = tx.QueryRow(ctx, `INSERT INTO source(tenant_id,shop_id,type,uri)
		VALUES($1,$2,'admin_manifest',$3) ON CONFLICT(tenant_id,shop_id,type,uri) WHERE uri IS NOT NULL
		DO UPDATE SET uri=EXCLUDED.uri RETURNING id`, tenantID, shopID, "admin://"+parsed.SourceId).Scan(&sourceID)
	if err != nil {
		return err
	}
	var manifestID int64
	err = tx.QueryRow(ctx, `INSERT INTO source_manifest(tenant_id,source_id,manifest_sha256,dataset_sha256,pipeline_version,manifest_yaml,uploaded_by)
		VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING id`, tenantID, sourceID, parsed.ManifestSha256,
		parsed.DatasetSha256, parsed.PipelineVersion, manifest, userID).Scan(&manifestID)
	if err != nil {
		return err
	}
	fileByPath := make(map[string][]byte, len(files))
	for _, file := range files {
		fileByPath[file.Path] = file.Content
	}
	fileIDs := map[string]int64{}
	for _, document := range parsed.Documents {
		content, found := fileByPath[document.Path]
		if !found {
			return &adminConflictError{message: "source file map changed after validation"}
		}
		media := "text/markdown"
		if document.Format == "csv" {
			media = "text/csv"
		}
		if document.Format == "docx" {
			media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
		}
		var fileID int64
		err = tx.QueryRow(ctx, `INSERT INTO source_file(tenant_id,source_manifest_id,document_id,relative_path,media_type,content_sha256,size_bytes,content)
			VALUES($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id`, tenantID, manifestID, document.DocumentId,
			document.Path, media, document.SourceHash, len(content), content).Scan(&fileID)
		if err != nil {
			return err
		}
		fileIDs[document.DocumentId] = fileID
	}
	var jobID int64
	err = tx.QueryRow(ctx, `INSERT INTO ingest_job(tenant_id,source_id,idempotency_key,stage,status,manifest_sha256,dataset_sha256,submitted_by)
		VALUES($1,$2,$3,'review','awaiting_review',$4,$5,$6) RETURNING id`, tenantID, sourceID, key,
		parsed.ManifestSha256, parsed.DatasetSha256, userID).Scan(&jobID)
	if err != nil {
		return err
	}
	for _, document := range parsed.Documents {
		var documentRow int64
		err = tx.QueryRow(ctx, `INSERT INTO document(tenant_id,source_id,logical_key,title,doc_type,meta_json,status)
			VALUES($1,$2,$3,$4,$5,jsonb_build_object('shop_id',$6::text,'disclosure_class',$7::text,'external_allowed',false),'pending')
			ON CONFLICT(tenant_id,logical_key) DO UPDATE SET title=EXCLUDED.title,
				status=CASE WHEN document.status='revoked' THEN 'pending' ELSE document.status END,
				active_version_id=CASE WHEN document.status='revoked' THEN NULL ELSE document.active_version_id END
			WHERE document.source_id=EXCLUDED.source_id
			RETURNING id`, tenantID, sourceID, document.DocumentId, document.Title, document.Format, shopID, document.DisclosureClass).Scan(&documentRow)
		if errors.Is(err, pgx.ErrNoRows) {
			return &adminConflictError{message: "document identifier is already owned by another source or active publication"}
		}
		if err != nil {
			return err
		}
		var versionID int64
		err = tx.QueryRow(ctx, `INSERT INTO document_version(tenant_id,document_id,source_hash,parser_version,chunk_rule_version,
			embedding_model,pipeline_version,object_key,parsed_object_key,status,chunk_count,disclosure_class,external_allowed,
			source_manifest_id,quality_json)
			VALUES($1,$2,$3,'deterministic-parser-v1','deterministic-chunk-v1','not_run',$4,$5,$6,'ready',$7,$8,false,$9,$10::jsonb)
			RETURNING id`, tenantID, documentRow, document.SourceHash, parsed.PipelineVersion,
			"db://source_file/"+strconv.FormatInt(fileIDs[document.DocumentId], 10),
			"db://source_file/"+strconv.FormatInt(fileIDs[document.DocumentId], 10), len(document.Chunks), document.DisclosureClass,
			manifestID, fmt.Sprintf(`{"chunk_count":%d,"empty_chunk_count":0,"parse_status":"passed","embedding_status":"not_run","index_mode":"deterministic_keyword","quality_gate":"passed"}`, len(document.Chunks))).Scan(&versionID)
		if err != nil {
			return err
		}
		var itemID int64
		err = tx.QueryRow(ctx, `INSERT INTO ingest_job_item(tenant_id,job_id,data_id,payload_hash,status,stage,document_version_id)
			VALUES($1,$2,$3,$4,'pending','review',$5) RETURNING id`, tenantID, jobID, document.DocumentId, document.SourceHash, versionID).Scan(&itemID)
		if err != nil {
			return err
		}
		for _, chunk := range document.Chunks {
			metadata := chunk.MetadataJson
			if !json.Valid([]byte(metadata)) {
				return errors.New("parsed chunk metadata is invalid")
			}
			var effectiveFrom, effectiveTo *string
			if document.EffectiveFrom != "" {
				value := document.EffectiveFrom
				effectiveFrom = &value
			}
			if document.EffectiveTo != "" {
				value := document.EffectiveTo
				effectiveTo = &value
			}
			_, err = tx.Exec(ctx, `INSERT INTO chunk(version_id,doc_id,tenant_id,shop_id,chunk_index,section_seq,section_chunk_index,
				char_start,char_end,heading_path,content,embed_text,token_count,content_type,split_reason,embedding_model,embedding_dim,
				tokenizer_id,effective_from,effective_to,meta_json,source_object_key,parsed_object_key,chunk_hash)
				VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$11,$12,$13,$14,'not_run',1024,'unicode-char-v1',
				$15::date,$16::date,$17::jsonb,$18,$18,$19)`, versionID, documentRow, tenantID, shopID,
				chunk.ChunkIndex, chunk.SectionSeq, chunk.SectionChunkIndex, chunk.CharStart, chunk.CharEnd,
				chunk.HeadingPath, chunk.Content, chunk.TokenCount, chunk.ContentType, chunk.SplitReason,
				nullableDate(effectiveFrom), nullableDate(effectiveTo), metadata,
				"db://source_file/"+strconv.FormatInt(fileIDs[document.DocumentId], 10), chunk.ChunkHash)
			if err != nil {
				return err
			}
		}
		_, err = tx.Exec(ctx, `UPDATE ingest_job SET document_version_id=COALESCE(document_version_id,$3),updated_at=now() WHERE tenant_id=$1 AND id=$2`, tenantID, jobID, versionID)
		if err != nil {
			return err
		}
	}
	if err = auditAdmin(ctx, tx, tenantID, userID, "ingestion.import", "source_manifest", strconv.FormatInt(manifestID, 10),
		"", parsed.ManifestSha256, 0, "validated synthetic package import", "success",
		map[string]any{"source_id": parsed.SourceId, "document_count": len(parsed.Documents), "dataset_sha256": parsed.DatasetSha256, "shop_id": shopID}); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func nullableDate(value *string) any {
	if value == nil || *value == "" {
		return nil
	}
	return *value
}

func (g *gateway) adminJobs(w http.ResponseWriter, r *http.Request) {
	scope, ok := g.requireAdmin(w, r, adminRoleMerchant, adminRoleReviewer, adminRoleObserver)
	if !ok {
		return
	}
	rows, err := g.db.Query(r.Context(), `SELECT j.id,j.stage,j.status,s.shop_id,j.manifest_sha256,j.dataset_sha256,
		j.fencing_epoch,j.cancel_requested,j.created_at,j.updated_at,count(i.id),
		count(i.id) FILTER (WHERE i.status='pending'),count(i.id) FILTER (WHERE i.status='running'),
		count(i.id) FILTER (WHERE i.status='awaiting_review'),count(i.id) FILTER (WHERE i.status='done'),
		count(i.id) FILTER (WHERE i.status='failed') FROM ingest_job j JOIN source s ON s.tenant_id=j.tenant_id AND s.id=j.source_id
		LEFT JOIN ingest_job_item i ON i.tenant_id=j.tenant_id AND i.job_id=j.id
		WHERE j.tenant_id=$1 AND s.shop_id=ANY($2::text[]) AND s.type='admin_manifest'
		GROUP BY j.id,s.shop_id ORDER BY j.created_at DESC LIMIT 200`, g.session.tenantID, scope.shops)
	if err != nil {
		writeError(w, 503, "database_unavailable", "could not load ingestion jobs")
		return
	}
	defer rows.Close()
	items := make([]map[string]any, 0)
	for rows.Next() {
		var id, fence, all, pending, running, review, done, failed int64
		var stage, state, shop, manifestHash string
		var datasetHash *string
		var cancelRequested bool
		var created, updated time.Time
		if rows.Scan(&id, &stage, &state, &shop, &manifestHash, &datasetHash, &fence, &cancelRequested,
			&created, &updated, &all, &pending, &running, &review, &done, &failed) != nil {
			writeError(w, 503, "database_unavailable", "could not load ingestion jobs")
			return
		}
		items = append(items, map[string]any{"job_id": strconv.FormatInt(id, 10), "stage": stage, "status": state,
			"shop_id": shop, "manifest_sha256": manifestHash, "dataset_sha256": datasetHash,
			"fencing_epoch": fence, "cancel_requested": cancelRequested, "item_count": all,
			"pending": pending, "running": running, "awaiting_review": review, "done": done, "failed": failed,
			"created_at": created, "updated_at": updated})
	}
	writeJSON(w, 200, map[string]any{"items": items})
}

func (g *gateway) adminJobDetail(w http.ResponseWriter, r *http.Request) {
	scope, ok := g.requireAdmin(w, r, adminRoleMerchant, adminRoleReviewer, adminRoleObserver)
	if !ok {
		return
	}
	jobID, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil {
		writeError(w, 404, "not_found", "ingestion job not found")
		return
	}
	var jobStatus, jobStage, shopID, manifestHash string
	var datasetHash, errorCode *string
	var fencingEpoch int64
	var cancelRequested bool
	var leaseExpiresAt *time.Time
	err = g.db.QueryRow(r.Context(), `SELECT j.status,j.stage,s.shop_id,j.manifest_sha256,j.dataset_sha256,
		j.fencing_epoch,j.cancel_requested,j.lease_expires_at,j.error_code FROM ingest_job j
		JOIN source s ON s.tenant_id=j.tenant_id AND s.id=j.source_id
		WHERE j.tenant_id=$1 AND j.id=$2 AND s.shop_id=ANY($3::text[]) AND s.type='admin_manifest'`,
		g.session.tenantID, jobID, scope.shops).Scan(&jobStatus, &jobStage, &shopID, &manifestHash,
		&datasetHash, &fencingEpoch, &cancelRequested, &leaseExpiresAt, &errorCode)
	if errors.Is(err, pgx.ErrNoRows) {
		writeError(w, 404, "not_found", "ingestion job not found")
		return
	}
	if err != nil {
		writeError(w, 503, "database_unavailable", "could not load ingestion job")
		return
	}
	rows, err := g.db.Query(r.Context(), `SELECT i.id,i.data_id,i.payload_hash,i.status,i.stage,i.retry_count,
		i.error_code,i.cancel_requested,i.fencing_epoch,i.document_version_id::text,v.status,v.review_status,v.source_hash
		FROM ingest_job j JOIN source s ON s.tenant_id=j.tenant_id AND s.id=j.source_id
		JOIN ingest_job_item i ON i.tenant_id=j.tenant_id AND i.job_id=j.id
		LEFT JOIN document_version v ON v.tenant_id=i.tenant_id AND v.id=i.document_version_id
		WHERE j.tenant_id=$1 AND j.id=$2 AND s.shop_id=ANY($3::text[]) AND s.type='admin_manifest'
		ORDER BY i.id LIMIT 500`, g.session.tenantID, jobID, scope.shops)
	if err != nil {
		writeError(w, 503, "database_unavailable", "could not load ingestion items")
		return
	}
	defer rows.Close()
	items := make([]map[string]any, 0)
	for rows.Next() {
		var id, retries, fence int64
		var dataID, payloadHash, state, stage string
		var errorCode *string
		var cancelled bool
		var versionID, versionState, reviewState, versionHash *string
		if rows.Scan(&id, &dataID, &payloadHash, &state, &stage, &retries, &errorCode, &cancelled,
			&fence, &versionID, &versionState, &reviewState, &versionHash) != nil {
			writeError(w, 503, "database_unavailable", "could not load ingestion items")
			return
		}
		items = append(items, map[string]any{"item_id": strconv.FormatInt(id, 10), "document_id": dataID,
			"payload_hash": payloadHash, "status": state, "stage": stage, "retry_count": retries,
			"error_code": errorCode, "cancel_requested": cancelled, "fencing_epoch": fence,
			"version_id": versionID, "version_status": versionState, "review_status": reviewState,
			"version_hash": versionHash})
	}
	if err = rows.Err(); err != nil {
		writeError(w, 503, "database_unavailable", "could not load ingestion items")
		return
	}
	if len(items) == 0 {
		var exists bool
		if err = g.db.QueryRow(r.Context(), `SELECT EXISTS(SELECT 1 FROM ingest_job j JOIN source s ON s.tenant_id=j.tenant_id AND s.id=j.source_id
			WHERE j.tenant_id=$1 AND j.id=$2 AND s.shop_id=ANY($3::text[]) AND s.type='admin_manifest')`,
			g.session.tenantID, jobID, scope.shops).Scan(&exists); err != nil {
			writeError(w, 503, "database_unavailable", "could not verify ingestion job")
			return
		}
		if !exists {
			writeError(w, 404, "not_found", "ingestion job not found")
			return
		}
	}
	writeJSON(w, 200, map[string]any{"job_id": strconv.FormatInt(jobID, 10), "status": jobStatus,
		"stage": jobStage, "shop_id": shopID, "manifest_sha256": manifestHash, "dataset_sha256": datasetHash,
		"fencing_epoch": fencingEpoch, "cancel_requested": cancelRequested, "lease_expires_at": leaseExpiresAt,
		"error_code": errorCode, "items": items})
}

func (g *gateway) adminReviews(w http.ResponseWriter, r *http.Request) {
	scope, ok := g.requireAdmin(w, r, adminRoleReviewer)
	if !ok {
		return
	}
	rows, err := g.db.Query(r.Context(), `SELECT v.id,d.logical_key,d.title,s.shop_id,v.source_hash,v.status,v.disclosure_class,
		v.external_allowed,v.review_status,v.reviewed_hash,v.fencing_epoch,v.chunk_count,v.pipeline_version,v.quality_json,v.created_at,
		j.id,j.stage FROM document_version v JOIN document d ON d.tenant_id=v.tenant_id AND d.id=v.document_id
		JOIN source s ON s.tenant_id=d.tenant_id AND s.id=d.source_id
		LEFT JOIN ingest_job_item i ON i.tenant_id=v.tenant_id AND i.document_version_id=v.id
		LEFT JOIN ingest_job j ON j.tenant_id=i.tenant_id AND j.id=i.job_id
		WHERE v.tenant_id=$1 AND s.shop_id=ANY($2::text[]) AND s.type='admin_manifest'
		ORDER BY v.created_at DESC LIMIT 300`, g.session.tenantID, scope.shops)
	if err != nil {
		writeError(w, 503, "database_unavailable", "could not load review queue")
		return
	}
	defer rows.Close()
	items := make([]map[string]any, 0)
	for rows.Next() {
		var id int64
		var jobID *int64
		var key, title, shop, hash, state, disclosure, reviewState, pipeline, stage string
		var external bool
		var reviewedHash *string
		var fence int64
		var chunks *int32
		var created time.Time
		var quality []byte
		if rows.Scan(&id, &key, &title, &shop, &hash, &state, &disclosure, &external, &reviewState, &reviewedHash, &fence, &chunks, &pipeline, &quality, &created, &jobID, &stage) != nil {
			writeError(w, 503, "database_unavailable", "could not load review queue")
			return
		}
		items = append(items, map[string]any{"version_id": strconv.FormatInt(id, 10), "document_id": key, "title": title, "shop_id": shop,
			"source_hash": hash, "status": state, "disclosure_class": disclosure, "external_allowed": external, "review_status": reviewState,
			"reviewed_hash": reviewedHash, "fencing_epoch": fence, "chunk_count": chunks, "pipeline_version": pipeline,
			"quality": json.RawMessage(quality), "created_at": created,
			"job_id": jobID, "stage": stage})
	}
	writeJSON(w, 200, map[string]any{"items": items})
}

func (g *gateway) adminReviewDetail(w http.ResponseWriter, r *http.Request) {
	scope, ok := g.requireAdmin(w, r, adminRoleReviewer)
	if !ok {
		return
	}
	id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil {
		writeError(w, 404, "not_found", "version not found")
		return
	}
	var data map[string]any
	err = g.db.QueryRow(r.Context(), `SELECT jsonb_build_object('version_id',v.id::text,'document_id',d.logical_key,'title',d.title,
		'shop_id',s.shop_id,'source_hash',v.source_hash,'status',v.status,'disclosure_class',v.disclosure_class,
		'external_allowed',v.external_allowed,'review_status',v.review_status,'reviewed_hash',v.reviewed_hash,
		'fencing_epoch',v.fencing_epoch,'chunk_count',v.chunk_count,'quality',v.quality_json,
		'content',COALESCE((SELECT jsonb_agg(jsonb_build_object('chunk_index',c.chunk_index,'content',c.content) ORDER BY c.chunk_index)
			FROM chunk c WHERE c.tenant_id=v.tenant_id AND c.version_id=v.id),'[]'::jsonb),'path',sf.relative_path,'media_type',sf.media_type)
		FROM document_version v JOIN document d ON d.tenant_id=v.tenant_id AND d.id=v.document_id
		JOIN source s ON s.tenant_id=d.tenant_id AND s.id=d.source_id
		JOIN source_file sf ON sf.tenant_id=v.tenant_id AND sf.source_manifest_id=v.source_manifest_id AND sf.document_id=d.logical_key
		WHERE v.tenant_id=$1 AND v.id=$2 AND s.shop_id=ANY($3::text[]) AND s.type='admin_manifest'`,
		g.session.tenantID, id, scope.shops).Scan(&data)
	if errors.Is(err, pgx.ErrNoRows) {
		writeError(w, 404, "not_found", "version not found")
		return
	}
	if err != nil {
		writeError(w, 503, "database_unavailable", "review material is unavailable")
		return
	}
	if err = g.refreshAuthorization(r.Context()); err != nil {
		writeError(w, authorizationHTTPStatus(err), authorizationFailureCode(err), "authorization failed")
		return
	}
	writeJSON(w, 200, data)
}

func (g *gateway) adminReviewVersion(w http.ResponseWriter, r *http.Request) {
	scope, ok := g.requireAdmin(w, r, adminRoleReviewer)
	if !ok {
		return
	}
	var in struct {
		Confirm          bool   `json:"confirm"`
		Decision         string `json:"decision"`
		Disclosure       string `json:"disclosure_class"`
		External         bool   `json:"external_allowed"`
		ExpectedHash     string `json:"expected_hash"`
		ExpectedFence    int64  `json:"expected_fencing_epoch"`
		ExpectedRevision string `json:"permission_revision"`
		Reason           string `json:"reason"`
	}
	if err := decodeJSON(r, &in); err != nil || !in.Confirm || !contains([]string{"approved", "rejected"}, in.Decision) || !contains([]string{"external_allowed", "internal_only", "unclassified"}, in.Disclosure) || len(strings.TrimSpace(in.Reason)) < 10 {
		writeError(w, 400, "invalid_review", "review decision, classification and reason are required")
		return
	}
	if in.External != (in.Disclosure == "external_allowed") {
		writeError(w, 400, "invalid_disclosure", "external_allowed must match disclosure_class")
		return
	}
	if in.Decision == "approved" && in.Disclosure == "unclassified" {
		writeError(w, 400, "invalid_disclosure", "unclassified versions cannot be approved")
		return
	}
	if in.ExpectedRevision != g.currentPermissionRevision() {
		writeError(w, 409, "confirmation_stale", "permission revision changed")
		return
	}
	id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil {
		writeError(w, 404, "not_found", "version not found")
		return
	}
	tx, err := g.db.BeginTx(r.Context(), pgx.TxOptions{})
	if err != nil {
		writeError(w, 503, "database_unavailable", "review could not be started")
		return
	}
	defer tx.Rollback(r.Context())
	var documentKey, shop, hash, state, reviewStatus string
	var fence int64
	err = tx.QueryRow(r.Context(), `SELECT d.logical_key,s.shop_id,v.source_hash,v.status,v.review_status,v.fencing_epoch FROM document_version v
		JOIN document d ON d.tenant_id=v.tenant_id AND d.id=v.document_id JOIN source s ON s.tenant_id=d.tenant_id AND s.id=d.source_id
		WHERE v.tenant_id=$1 AND v.id=$2 AND s.shop_id=ANY($3::text[]) FOR UPDATE OF v`, g.session.tenantID, id, scope.shops).Scan(&documentKey, &shop, &hash, &state, &reviewStatus, &fence)
	if errors.Is(err, pgx.ErrNoRows) {
		writeError(w, 404, "not_found", "version not found")
		return
	}
	if err != nil {
		writeError(w, 503, "database_unavailable", "version could not be loaded")
		return
	}
	if !g.adminGrantInTx(r.Context(), tx, adminRoleReviewer, shop) {
		writeError(w, 403, "admin_scope_denied", "review permission changed")
		return
	}
	if hash != in.ExpectedHash || fence != in.ExpectedFence || !reviewableVersion(state, reviewStatus) {
		writeError(w, 409, "version_conflict", "version hash, fencing epoch or review state changed")
		return
	}
	reviewState := in.Decision
	_, err = tx.Exec(r.Context(), `UPDATE document_version SET review_status=$3,reviewed_hash=source_hash,reviewed_by=$4,
		reviewed_at=now(),review_reason=$5,disclosure_class=$6,external_allowed=$7,
		quality_json=quality_json || jsonb_build_object('review',$3::text,'reviewed_hash',source_hash)
		WHERE tenant_id=$1 AND id=$2`, g.session.tenantID, id, reviewState, g.session.userID, strings.TrimSpace(in.Reason), in.Disclosure, in.External)
	if err == nil {
		_, err = tx.Exec(r.Context(), `UPDATE ingest_job_item SET stage='reviewed',error_code=NULL WHERE tenant_id=$1 AND document_version_id=$2`, g.session.tenantID, id)
	}
	if err == nil {
		err = auditAdmin(r.Context(), tx, g.session.tenantID, g.session.userID, "version.review."+in.Decision, "document", documentKey, strconv.FormatInt(id, 10), hash, fence, strings.TrimSpace(in.Reason), "success", map[string]any{"shop_id": shop, "disclosure_class": in.Disclosure, "external_allowed": in.External})
	}
	if err != nil {
		writeError(w, 503, "review_failed", "review could not be recorded")
		return
	}
	if err = tx.Commit(r.Context()); err != nil {
		writeError(w, 503, "review_failed", "review could not be committed")
		return
	}
	writeJSON(w, 200, map[string]any{"version_id": strconv.FormatInt(id, 10), "review_status": reviewState, "reviewed_hash": hash, "audit_result": "success"})
}

func (g *gateway) adminPublishVersion(w http.ResponseWriter, r *http.Request) {
	scope, ok := g.requireAdmin(w, r, adminRoleReviewer)
	if !ok {
		return
	}
	var in adminConfirmation
	if err := decodeJSON(r, &in); err != nil || !in.Confirm || len(strings.TrimSpace(in.Reason)) < 10 {
		writeError(w, 400, "confirmation_required", "complete the scoped confirmation and reason")
		return
	}
	id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil {
		writeError(w, 404, "not_found", "version not found")
		return
	}
	if err = g.lockAdminIndexTransition(r.Context()); err != nil {
		writeError(w, 503, "index_sync_unavailable", "published knowledge index is unavailable")
		return
	}
	defer g.adminIndexMu.Unlock()
	shop, key, hash, fence, state, review, err := g.lockVersion(r.Context(), id, scope.shops)
	if err != nil {
		g.writeVersionError(w, err)
		return
	}
	if !g.confirmVersion(w, r, in, shop, key, hash, fence) {
		return
	}
	if state != "ready" || review != "approved" {
		writeError(w, 409, "version_conflict", "version is not reviewed and ready for publication")
		return
	}
	tx, err := g.db.BeginTx(r.Context(), pgx.TxOptions{})
	if err != nil {
		writeError(w, 503, "database_unavailable", "publication could not be started")
		return
	}
	defer tx.Rollback(r.Context())
	if !g.adminGrantInTx(r.Context(), tx, adminRoleReviewer, shop) {
		writeError(w, 403, "admin_scope_denied", "publication permission changed")
		return
	}
	result, err := publishVersionTx(r.Context(), tx, g.session.tenantID, g.session.userID, id, shop, key, hash, fence, strings.TrimSpace(in.Reason))
	if err != nil {
		writeError(w, databaseDecisionStatus(err), "publication_conflict", "publication failed closed")
		return
	}
	if err = tx.Commit(r.Context()); err != nil {
		writeError(w, 503, "publication_failed", "publication could not be committed")
		return
	}
	if err = g.completeAdminIndexTransition(r.Context(), []int64{id}); err != nil {
		writeError(w, 503, "index_sync_unavailable", "publication is stored but the search index is not ready")
		return
	}
	writeJSON(w, 200, result)
}

func (g *gateway) adminRollbackVersion(w http.ResponseWriter, r *http.Request) {
	scope, ok := g.requireAdmin(w, r, adminRoleReviewer)
	if !ok {
		return
	}
	var in adminConfirmation
	if err := decodeJSON(r, &in); err != nil || !in.Confirm || len(strings.TrimSpace(in.Reason)) < 10 {
		writeError(w, 400, "confirmation_required", "complete the scoped confirmation and reason")
		return
	}
	id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil {
		writeError(w, 404, "not_found", "version not found")
		return
	}
	if err = g.lockAdminIndexTransition(r.Context()); err != nil {
		writeError(w, 503, "index_sync_unavailable", "published knowledge index is unavailable")
		return
	}
	defer g.adminIndexMu.Unlock()
	shop, key, hash, fence, state, review, err := g.lockVersion(r.Context(), id, scope.shops)
	if err != nil {
		g.writeVersionError(w, err)
		return
	}
	if !g.confirmVersion(w, r, in, shop, key, hash, fence) {
		return
	}
	if state != "superseded" || review != "approved" {
		writeError(w, 409, "version_conflict", "only an approved superseded version can be restored")
		return
	}
	tx, err := g.db.BeginTx(r.Context(), pgx.TxOptions{})
	if err != nil {
		writeError(w, 503, "database_unavailable", "rollback could not be started")
		return
	}
	defer tx.Rollback(r.Context())
	if !g.adminGrantInTx(r.Context(), tx, adminRoleReviewer, shop) {
		writeError(w, 403, "admin_scope_denied", "rollback permission changed")
		return
	}
	result, err := rollbackVersionTx(r.Context(), tx, g.session.tenantID, g.session.userID, id, shop, key, hash, fence, strings.TrimSpace(in.Reason))
	if err != nil {
		writeError(w, databaseDecisionStatus(err), "rollback_conflict", "rollback failed closed")
		return
	}
	if err = tx.Commit(r.Context()); err != nil {
		writeError(w, 503, "rollback_failed", "rollback could not be committed")
		return
	}
	if err = g.completeAdminIndexTransition(r.Context(), []int64{id}); err != nil {
		writeError(w, 503, "index_sync_unavailable", "rollback is stored but the search index is not ready")
		return
	}
	writeJSON(w, 200, result)
}

func (g *gateway) adminRevokeVersion(w http.ResponseWriter, r *http.Request) {
	scope, ok := g.requireAdmin(w, r, adminRoleReviewer)
	if !ok {
		return
	}
	var in adminConfirmation
	if err := decodeJSON(r, &in); err != nil || !in.Confirm || len(strings.TrimSpace(in.Reason)) < 10 {
		writeError(w, 400, "confirmation_required", "complete the scoped confirmation and reason")
		return
	}
	id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil {
		writeError(w, 404, "not_found", "version not found")
		return
	}
	if err = g.lockAdminIndexTransition(r.Context()); err != nil {
		writeError(w, 503, "index_sync_unavailable", "published knowledge index is unavailable")
		return
	}
	defer g.adminIndexMu.Unlock()
	shop, key, hash, fence, state, _, err := g.lockVersion(r.Context(), id, scope.shops)
	if err != nil {
		g.writeVersionError(w, err)
		return
	}
	if !g.confirmVersion(w, r, in, shop, key, hash, fence) {
		return
	}
	if state == "revoked" {
		writeError(w, 409, "version_conflict", "revoked version cannot be changed")
		return
	}
	tx, err := g.db.BeginTx(r.Context(), pgx.TxOptions{})
	if err != nil {
		writeError(w, 503, "database_unavailable", "revocation could not be started")
		return
	}
	defer tx.Rollback(r.Context())
	if !g.adminGrantInTx(r.Context(), tx, adminRoleReviewer, shop) {
		writeError(w, 403, "admin_scope_denied", "revoke permission changed")
		return
	}
	_, err = tx.Exec(r.Context(), `UPDATE document_version SET status='revoked',fencing_epoch=fencing_epoch+1 WHERE tenant_id=$1 AND id=$2 AND fencing_epoch=$3`, g.session.tenantID, id, fence)
	if err == nil {
		_, err = tx.Exec(r.Context(), `UPDATE document SET status='revoked',active_version_id=NULL WHERE tenant_id=$1 AND logical_key=$2 AND active_version_id=$3`, g.session.tenantID, key, id)
	}
	if err == nil {
		err = auditAdmin(r.Context(), tx, g.session.tenantID, g.session.userID, "version.revoke", "document", key, strconv.FormatInt(id, 10), hash, fence+1, strings.TrimSpace(in.Reason), "success", map[string]any{"shop_id": shop})
	}
	if err != nil {
		writeError(w, 409, "version_conflict", "version revoke failed closed")
		return
	}
	if err = tx.Commit(r.Context()); err != nil {
		writeError(w, 503, "revoke_failed", "version revoke could not be committed")
		return
	}
	if err = g.completeAdminIndexTransition(r.Context(), []int64{id}); err != nil {
		writeError(w, 503, "index_sync_unavailable", "revocation is stored but the search index is not ready")
		return
	}
	writeJSON(w, 200, map[string]any{"version_id": strconv.FormatInt(id, 10), "status": "revoked", "fencing_epoch": fence + 1, "audit_result": "success"})
}

func (g *gateway) adminVersions(w http.ResponseWriter, r *http.Request) {
	scope, ok := g.requireAdmin(w, r, adminRoleMerchant, adminRoleReviewer, adminRoleObserver)
	if !ok {
		return
	}
	rows, err := g.db.Query(r.Context(), `SELECT v.id,d.logical_key,d.title,s.shop_id,v.status,v.review_status,v.source_hash,v.reviewed_hash,
		v.disclosure_class,v.external_allowed,v.fencing_epoch,v.chunk_count,v.created_at,v.activated_at
		FROM document_version v JOIN document d ON d.tenant_id=v.tenant_id AND d.id=v.document_id
		JOIN source s ON s.tenant_id=d.tenant_id AND s.id=d.source_id
		WHERE v.tenant_id=$1 AND s.shop_id=ANY($2::text[]) AND s.type='admin_manifest'
		ORDER BY v.created_at DESC LIMIT 500`, g.session.tenantID, scope.shops)
	if err != nil {
		writeError(w, 503, "database_unavailable", "could not load version history")
		return
	}
	defer rows.Close()
	items := make([]map[string]any, 0)
	for rows.Next() {
		var id int64
		var key, title, shop, state, review, hash, disclosure string
		var reviewed *string
		var external bool
		var fence int64
		var chunks *int32
		var created time.Time
		var active *time.Time
		if rows.Scan(&id, &key, &title, &shop, &state, &review, &hash, &reviewed, &disclosure, &external, &fence, &chunks, &created, &active) != nil {
			writeError(w, 503, "database_unavailable", "could not load version history")
			return
		}
		items = append(items, map[string]any{"version_id": strconv.FormatInt(id, 10), "document_id": key, "title": title, "shop_id": shop, "status": state, "review_status": review, "source_hash": hash, "reviewed_hash": reviewed, "disclosure_class": disclosure, "external_allowed": external, "fencing_epoch": fence, "chunk_count": chunks, "created_at": created, "activated_at": active})
	}
	writeJSON(w, 200, map[string]any{"items": items})
}

func (g *gateway) lockVersion(ctx context.Context, id int64, shops []string) (shop, key, hash string, fence int64, state, review string, err error) {
	err = g.db.QueryRow(ctx, `SELECT s.shop_id,d.logical_key,v.source_hash,v.fencing_epoch,v.status,v.review_status FROM document_version v
		JOIN document d ON d.tenant_id=v.tenant_id AND d.id=v.document_id JOIN source s ON s.tenant_id=d.tenant_id AND s.id=d.source_id
		WHERE v.tenant_id=$1 AND v.id=$2 AND s.shop_id=ANY($3::text[]) AND s.type='admin_manifest'`, g.session.tenantID, id, shops).Scan(&shop, &key, &hash, &fence, &state, &review)
	if errors.Is(err, pgx.ErrNoRows) {
		err = pgx.ErrNoRows
	}
	return
}

func (g *gateway) confirmVersion(w http.ResponseWriter, r *http.Request, in adminConfirmation, shop, key, hash string, fence int64) bool {
	if in.TenantID != g.session.tenant || in.ShopID != shop || in.DocumentID != key || in.VersionHash != hash || in.PermissionRevision != g.currentPermissionRevision() || in.ExpectedFencingEpoch != fence {
		writeError(w, 409, "confirmation_stale", "confirmation does not match the current authorized resource")
		return false
	}
	if err := g.refreshAuthorization(r.Context()); err != nil {
		writeError(w, authorizationHTTPStatus(err), authorizationFailureCode(err), "authorization failed")
		return false
	}
	return true
}

func (g *gateway) adminGrantInTx(ctx context.Context, tx pgx.Tx, role, shop string) bool {
	var allowed bool
	err := tx.QueryRow(ctx, `SELECT ecr_check_admin_grant($1,$2,$3,$4)`,
		g.session.tenantID, g.session.userID, role, shop).Scan(&allowed)
	return err == nil && allowed
}

func (g *gateway) reloadPublishedIndex(ctx context.Context, versionIDs []int64) error {
	if g.rag == nil {
		return errors.New("RAG service is unavailable")
	}
	for _, versionID := range versionIDs {
		var documentKey, shop, tenant, disclosure string
		var activeVersion *int64
		err := g.db.QueryRow(ctx, `SELECT t.name,s.shop_id,d.logical_key,d.active_version_id,
			COALESCE(active.disclosure_class,'') FROM document_version target
			JOIN document d ON d.tenant_id=target.tenant_id AND d.id=target.document_id
			JOIN source s ON s.tenant_id=d.tenant_id AND s.id=d.source_id
		JOIN tenant t ON t.id=target.tenant_id
		LEFT JOIN document_version active ON active.tenant_id=d.tenant_id AND active.id=d.active_version_id
		WHERE target.tenant_id=$1 AND target.id=$2 AND s.type='admin_manifest'`, g.session.tenantID, versionID).
			Scan(&tenant, &shop, &documentKey, &activeVersion, &disclosure)
		if err != nil {
			return err
		}
		request := &ragv1.ReloadSyntheticIndexRequest{Context: &ragv1.RequestContext{}}
		if activeVersion == nil || disclosure != "external_allowed" {
			request.RemoveDocumentIds = []string{documentKey}
		} else {
			rows, queryErr := g.db.Query(ctx, `SELECT c.chunk_index,c.section_seq,c.section_chunk_index,c.content,c.chunk_hash,c.heading_path,c.meta_json,
				c.effective_from,c.effective_to,v.id::text,d.logical_key FROM chunk c JOIN document_version v ON v.tenant_id=c.tenant_id AND v.id=c.version_id
				JOIN document d ON d.tenant_id=v.tenant_id AND d.id=v.document_id WHERE c.tenant_id=$1 AND v.id=$2 ORDER BY c.chunk_index`, g.session.tenantID, *activeVersion)
			if queryErr != nil {
				return queryErr
			}
			request.ReplaceDocumentIds = []string{documentKey}
			for rows.Next() {
				var index, section, sectionIndex int32
				var content, hash, heading, versionIDText, id string
				var metadata []byte
				var from, to *time.Time
				if rows.Scan(&index, &section, &sectionIndex, &content, &hash, &heading, &metadata, &from, &to, &versionIDText, &id) != nil {
					continue
				}
				chunkID := "chunk-" + hash
				if len(hash) > 20 {
					chunkID = "chunk-" + hash[:20]
				}
				meta := strings.TrimSpace(string(metadata))
				if !json.Valid([]byte(meta)) {
					meta = "{}"
				}
				chunk := &ragv1.SyntheticIndexChunk{TenantId: sessionTenantName(tenant), ShopId: shop, DocumentId: id, VersionId: versionIDText, ChunkId: chunkID,
					Title: id, HeadingPath: heading, Content: content, SourceRef: "db://document_version/" + versionIDText, DisclosureClass: disclosure, MetadataJson: meta,
					SectionSeq: section, ChunkIndex: index}
				if from != nil {
					chunk.EffectiveFrom = from.Format("2006-01-02")
				}
				if to != nil {
					chunk.EffectiveTo = to.Format("2006-01-02")
				}
				request.Chunks = append(request.Chunks, chunk)
			}
			rows.Close()
			if len(request.Chunks) == 0 {
				return errors.New("published version has no stored chunks")
			}
		}
		scope, err := g.issueScopeForShop(ctx, newRequestID(), shop)
		if err != nil {
			return err
		}
		request.Context = scope
		if _, err = g.rag.ReloadSyntheticIndex(ctx, request); err != nil {
			return err
		}
	}
	return nil
}

func (g *gateway) loadActiveAdminIndexes(ctx context.Context) error {
	if g.rag == nil {
		return errors.New("RAG service is unavailable")
	}
	rows, err := g.db.Query(ctx, `SELECT d.logical_key,s.shop_id,d.active_version_id,COALESCE(v.status,''),
		COALESCE(v.disclosure_class,'') FROM document d
		JOIN source s ON s.tenant_id=d.tenant_id AND s.id=d.source_id
		LEFT JOIN document_version v ON v.tenant_id=d.tenant_id AND v.id=d.active_version_id
		WHERE d.tenant_id=$1 AND s.type='admin_manifest' AND s.shop_id=ANY($2::text[])
		ORDER BY s.shop_id,d.logical_key`, g.session.tenantID, g.session.allowedShops)
	if err != nil {
		return err
	}
	type indexBatch struct {
		replace []string
		remove  []string
		chunks  []*ragv1.SyntheticIndexChunk
	}
	docs := make([]struct {
		id, shop, state, disclosure string
		version                     *int64
	}, 0)
	for rows.Next() {
		var doc struct {
			id, shop, state, disclosure string
			version                     *int64
		}
		if err = rows.Scan(&doc.id, &doc.shop, &doc.version, &doc.state, &doc.disclosure); err != nil {
			rows.Close()
			return err
		}
		docs = append(docs, doc)
	}
	if err = rows.Err(); err != nil {
		rows.Close()
		return err
	}
	rows.Close()
	batches := make(map[string]*indexBatch)
	for _, doc := range docs {
		batch := batches[doc.shop]
		if batch == nil {
			batch = &indexBatch{}
			batches[doc.shop] = batch
		}
		if doc.version == nil || doc.state != "active" || doc.disclosure != "external_allowed" {
			batch.remove = append(batch.remove, doc.id)
			continue
		}
		batch.replace = append(batch.replace, doc.id)
		chunkRows, queryErr := g.db.Query(ctx, `SELECT c.chunk_index,c.section_seq,c.section_chunk_index,c.content,c.chunk_hash,
			c.heading_path,c.meta_json,c.effective_from,c.effective_to,v.id::text,d.logical_key
			FROM chunk c JOIN document_version v ON v.tenant_id=c.tenant_id AND v.id=c.version_id
			JOIN document d ON d.tenant_id=v.tenant_id AND d.id=v.document_id
			WHERE c.tenant_id=$1 AND v.id=$2 ORDER BY c.chunk_index`, g.session.tenantID, *doc.version)
		if queryErr != nil {
			return queryErr
		}
		chunkCount := 0
		for chunkRows.Next() {
			var index, section, sectionIndex int32
			var content, hash, heading, versionID, documentID string
			var metadata []byte
			var from, to *time.Time
			if queryErr = chunkRows.Scan(&index, &section, &sectionIndex, &content, &hash, &heading,
				&metadata, &from, &to, &versionID, &documentID); queryErr != nil {
				chunkRows.Close()
				return queryErr
			}
			chunkID := "chunk-" + hash
			if len(hash) > 20 {
				chunkID = "chunk-" + hash[:20]
			}
			meta := strings.TrimSpace(string(metadata))
			if !json.Valid([]byte(meta)) {
				meta = "{}"
			}
			chunk := &ragv1.SyntheticIndexChunk{TenantId: g.session.tenant, ShopId: doc.shop,
				DocumentId: documentID, VersionId: versionID, ChunkId: chunkID, Title: documentID,
				HeadingPath: heading, Content: content, SourceRef: "db://document_version/" + versionID,
				DisclosureClass: doc.disclosure, MetadataJson: meta, SectionSeq: section, ChunkIndex: index}
			if from != nil {
				chunk.EffectiveFrom = from.Format("2006-01-02")
			}
			if to != nil {
				chunk.EffectiveTo = to.Format("2006-01-02")
			}
			batch.chunks = append(batch.chunks, chunk)
			chunkCount++
		}
		if queryErr = chunkRows.Err(); queryErr != nil {
			chunkRows.Close()
			return queryErr
		}
		chunkRows.Close()
		if chunkCount == 0 {
			return errors.New("published version has no stored chunks")
		}
	}
	for _, shop := range g.session.allowedShops {
		batch := batches[shop]
		if batch == nil || (len(batch.replace) == 0 && len(batch.remove) == 0) {
			continue
		}
		scope, scopeErr := g.issueScopeForShop(ctx, newRequestID(), shop)
		if scopeErr != nil {
			return scopeErr
		}
		request := &ragv1.ReloadSyntheticIndexRequest{Context: scope,
			Chunks: batch.chunks, ReplaceDocumentIds: batch.replace, RemoveDocumentIds: batch.remove}
		if _, err = g.rag.ReloadSyntheticIndex(ctx, request); err != nil {
			return err
		}
	}
	fingerprint, err := g.activeAdminIndexFingerprint(ctx)
	if err != nil {
		return err
	}
	g.adminIndexFingerprint = fingerprint
	g.adminIndexReady.Store(true)
	return nil
}

func (g *gateway) activeAdminIndexFingerprint(ctx context.Context) (string, error) {
	rows, err := g.db.Query(ctx, `SELECT d.logical_key,COALESCE(d.active_version_id,0),
		COALESCE(v.status,''),COALESCE(v.disclosure_class,'')
		FROM document d
		JOIN source s ON s.tenant_id=d.tenant_id AND s.id=d.source_id
		LEFT JOIN document_version v ON v.tenant_id=d.tenant_id AND v.id=d.active_version_id
		WHERE d.tenant_id=$1 AND s.type='admin_manifest' AND s.shop_id=ANY($2::text[])
		ORDER BY s.shop_id,d.logical_key`, g.session.tenantID, g.session.allowedShops)
	if err != nil {
		return "", err
	}
	defer rows.Close()
	hash := sha256.New()
	for rows.Next() {
		var key, statusText, disclosure string
		var versionID int64
		if err := rows.Scan(&key, &versionID, &statusText, &disclosure); err != nil {
			return "", err
		}
		_, _ = fmt.Fprintf(hash, "%s\x00%d\x00%s\x00%s\n", key, versionID, statusText, disclosure)
	}
	if err := rows.Err(); err != nil {
		return "", err
	}
	return hex.EncodeToString(hash.Sum(nil)), nil
}

func (g *gateway) ensureAdminIndexReady(ctx context.Context) error {
	if g.profile != "synthetic_import_mock" {
		return nil
	}
	fingerprint, err := g.activeAdminIndexFingerprint(ctx)
	if err != nil {
		return err
	}
	g.adminIndexMu.RLock()
	ready := g.adminIndexReady.Load()
	currentFingerprint := g.adminIndexFingerprint
	g.adminIndexMu.RUnlock()
	if ready && fingerprint == currentFingerprint {
		return nil
	}
	g.adminIndexMu.Lock()
	defer g.adminIndexMu.Unlock()
	if g.adminIndexReady.Load() && fingerprint == g.adminIndexFingerprint {
		return nil
	}
	return g.loadActiveAdminIndexes(ctx)
}

func (g *gateway) lockAdminIndexTransition(ctx context.Context) error {
	g.adminIndexMu.Lock()
	if g.profile == "synthetic_import_mock" && !g.adminIndexReady.Load() {
		if err := g.loadActiveAdminIndexes(ctx); err != nil {
			g.adminIndexMu.Unlock()
			return err
		}
	}
	return nil
}

func (g *gateway) completeAdminIndexTransition(ctx context.Context, versionIDs []int64) error {
	if g.profile == "synthetic_import_mock" {
		g.adminIndexReady.Store(false)
	}
	if err := g.reloadPublishedIndex(ctx, versionIDs); err != nil {
		return err
	}
	fingerprint, err := g.activeAdminIndexFingerprint(ctx)
	if err != nil {
		return err
	}
	g.adminIndexFingerprint = fingerprint
	if g.profile == "synthetic_import_mock" {
		g.adminIndexReady.Store(true)
	}
	return nil
}

func reviewableVersion(state, reviewStatus string) bool {
	return state == "ready" && reviewStatus == "pending"
}

func (g *gateway) writeVersionError(w http.ResponseWriter, err error) {
	if errors.Is(err, pgx.ErrNoRows) {
		writeError(w, 404, "not_found", "version not found")
	} else {
		writeError(w, 503, "database_unavailable", "version status is unavailable")
	}
}

func publishVersionTx(ctx context.Context, tx pgx.Tx, tenant, user, version int64, shop, key, hash string, fence int64, reason string) (map[string]any, error) {
	var documentID int64
	var sourceHash, reviewedHash, statusText, reviewStatus, disclosure string
	var current *int64
	var external bool
	var currentFence int64
	var qualityJSON []byte
	var expectedChunks int32
	var storedChunks int64
	var sourceFileMatches bool
	err := tx.QueryRow(ctx, `SELECT d.id,v.source_hash,v.reviewed_hash,v.status,v.review_status,v.disclosure_class,v.external_allowed,
		v.fencing_epoch,d.active_version_id,v.quality_json,COALESCE(v.chunk_count,0),
		(SELECT count(*) FROM chunk c WHERE c.tenant_id=v.tenant_id AND c.version_id=v.id),
		EXISTS(SELECT 1 FROM source_file sf WHERE sf.tenant_id=v.tenant_id AND sf.source_manifest_id=v.source_manifest_id
			AND sf.document_id=d.logical_key AND sf.content_sha256=v.source_hash)
		FROM document_version v JOIN document d ON d.tenant_id=v.tenant_id AND d.id=v.document_id JOIN source s ON s.tenant_id=d.tenant_id AND s.id=d.source_id
		WHERE v.tenant_id=$1 AND v.id=$2 AND s.shop_id=$3 AND d.logical_key=$4 FOR UPDATE OF d,v`, tenant, version, shop, key).
		Scan(&documentID, &sourceHash, &reviewedHash, &statusText, &reviewStatus, &disclosure, &external, &currentFence,
			&current, &qualityJSON, &expectedChunks, &storedChunks, &sourceFileMatches)
	if err != nil {
		return nil, err
	}
	if sourceHash != hash || currentFence != fence || statusText != "ready" || reviewStatus != "approved" || reviewedHash != hash || external != (disclosure == "external_allowed") {
		return nil, pgx.ErrNoRows
	}
	var quality struct {
		ParseStatus     string `json:"parse_status"`
		IndexMode       string `json:"index_mode"`
		QualityGate     string `json:"quality_gate"`
		ChunkCount      int    `json:"chunk_count"`
		EmptyChunkCount int    `json:"empty_chunk_count"`
	}
	if json.Unmarshal(qualityJSON, &quality) != nil || quality.ParseStatus != "passed" ||
		quality.IndexMode != "deterministic_keyword" || quality.QualityGate != "passed" ||
		quality.EmptyChunkCount != 0 || quality.ChunkCount != int(expectedChunks) ||
		expectedChunks <= 0 || storedChunks != int64(expectedChunks) || !sourceFileMatches {
		return nil, errors.New("document version quality gate failed")
	}
	if _, err = tx.Exec(ctx, `UPDATE document_version SET status='superseded' WHERE tenant_id=$1 AND id=$2 AND status='active'`, tenant, current); err != nil {
		return nil, err
	}
	if _, err = tx.Exec(ctx, `UPDATE document_version SET status='active',activated_at=now(),fencing_epoch=fencing_epoch+1
		WHERE tenant_id=$1 AND id=$2 AND status='ready' AND fencing_epoch=$3`, tenant, version, fence); err != nil {
		return nil, err
	}
	if _, err = tx.Exec(ctx, `UPDATE document SET active_version_id=$3,status='active',meta_json=meta_json || jsonb_build_object('shop_id',$4::text,'disclosure_class',$5::text,'external_allowed',$6::boolean)
		WHERE tenant_id=$1 AND id=$2`, tenant, documentID, version, shop, disclosure, external); err != nil {
		return nil, err
	}
	var itemID, jobID int64
	var nextEpoch int64
	err = tx.QueryRow(ctx, `SELECT i.id,j.id,i.fencing_epoch+1 FROM ingest_job_item i JOIN ingest_job j ON j.tenant_id=i.tenant_id AND j.id=i.job_id
		WHERE i.tenant_id=$1 AND i.document_version_id=$2 AND i.cancel_requested=false FOR UPDATE OF i,j`, tenant, version).Scan(&itemID, &jobID, &nextEpoch)
	if err != nil {
		return nil, err
	}
	_, err = tx.Exec(ctx, `UPDATE ingest_job_item SET status='running',owner_token=$3,fencing_epoch=$4,lease_expires_at=now()+interval '5 minutes',stage='publishing'
		WHERE tenant_id=$1 AND id=$2`, tenant, itemID, fmt.Sprintf("publish-%d-%d", version, nextEpoch), nextEpoch)
	if err != nil {
		return nil, err
	}
	_, err = tx.Exec(ctx, `INSERT INTO ingest_publication(tenant_id,item_id,fencing_epoch,document_version_id) VALUES($1,$2,$3,$4)`, tenant, itemID, nextEpoch, version)
	if err != nil {
		return nil, err
	}
	_, err = tx.Exec(ctx, `UPDATE ingest_job SET status='done',stage='published',updated_at=now() WHERE tenant_id=$1 AND id=$2`, tenant, jobID)
	if err != nil {
		return nil, err
	}
	if err = auditAdmin(ctx, tx, tenant, user, "version.publish", "document", key, strconv.FormatInt(version, 10), hash, nextEpoch, reason, "success", map[string]any{"shop_id": shop, "disclosure_class": disclosure, "external_allowed": external}); err != nil {
		return nil, err
	}
	return map[string]any{"version_id": strconv.FormatInt(version, 10), "status": "active", "fencing_epoch": fence + 1, "audit_result": "success"}, nil
}

func rollbackVersionTx(ctx context.Context, tx pgx.Tx, tenant, user, version int64, shop, key, hash string, fence int64, reason string) (map[string]any, error) {
	var documentID int64
	var sourceHash, reviewedHash, state, review, disclosure string
	var current *int64
	var currentFence int64
	var external bool
	err := tx.QueryRow(ctx, `SELECT d.id,v.source_hash,v.reviewed_hash,v.status,v.review_status,v.disclosure_class,v.external_allowed,v.fencing_epoch,d.active_version_id
		FROM document_version v JOIN document d ON d.tenant_id=v.tenant_id AND d.id=v.document_id JOIN source s ON s.tenant_id=d.tenant_id AND s.id=d.source_id
		WHERE v.tenant_id=$1 AND v.id=$2 AND s.shop_id=$3 AND d.logical_key=$4 FOR UPDATE OF d,v`, tenant, version, shop, key).Scan(&documentID, &sourceHash, &reviewedHash, &state, &review, &disclosure, &external, &currentFence, &current)
	if err != nil {
		return nil, err
	}
	if sourceHash != hash || currentFence != fence || state != "superseded" || review != "approved" || reviewedHash != hash || external != (disclosure == "external_allowed") {
		return nil, pgx.ErrNoRows
	}
	if current != nil {
		if _, err = tx.Exec(ctx, `UPDATE document_version SET status='superseded' WHERE tenant_id=$1 AND id=$2 AND status='active'`, tenant, *current); err != nil {
			return nil, err
		}
	}
	if _, err = tx.Exec(ctx, `UPDATE document_version SET status='active',activated_at=now(),fencing_epoch=fencing_epoch+1 WHERE tenant_id=$1 AND id=$2 AND status='superseded' AND fencing_epoch=$3`, tenant, version, fence); err != nil {
		return nil, err
	}
	if _, err = tx.Exec(ctx, `UPDATE document SET active_version_id=$3,status='active',
		meta_json=meta_json || jsonb_build_object('disclosure_class',$4::text,'external_allowed',$5::boolean)
		WHERE tenant_id=$1 AND id=$2`, tenant, documentID, version, disclosure, external); err != nil {
		return nil, err
	}
	if err = auditAdmin(ctx, tx, tenant, user, "version.rollback", "document", key, strconv.FormatInt(version, 10), hash, fence+1, reason, "success", map[string]any{"shop_id": shop, "previous_version_id": current}); err != nil {
		return nil, err
	}
	return map[string]any{"version_id": strconv.FormatInt(version, 10), "status": "active", "fencing_epoch": fence + 1, "audit_result": "success"}, nil
}

func auditAdmin(ctx context.Context, tx pgx.Tx, tenant, user int64, action, kind, resource, version, hash string, fence int64, reason, result string, metadata map[string]any) error {
	encoded, err := json.Marshal(metadata)
	if err != nil {
		return err
	}
	var versionValue, hashValue, reasonValue any
	if version != "" {
		versionValue = version
	}
	if hash != "" {
		hashValue = hash
	}
	if reason != "" {
		reasonValue = reason
	}
	var fenceValue any
	if fence > 0 {
		fenceValue = fence
	}
	_, err = tx.Exec(ctx, `INSERT INTO admin_audit(tenant_id,actor_user_id,action,resource_type,resource_id,resource_version,content_hash,fencing_epoch,reason,result,metadata_json)
		VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb)`, tenant, user, action, kind, resource, versionValue, hashValue, fenceValue, reasonValue, result, encoded)
	return err
}

func validAdminRequest(role, scope string, shops []string, reason string, allowed []string) bool {
	if !contains([]string{"platform_observer", "merchant_admin", "knowledge_reviewer"}, role) || len(strings.TrimSpace(reason)) < 10 || len(reason) > 2000 {
		return false
	}
	if scope == "tenant" {
		return len(shops) == 0
	}
	if scope != "shops" || len(shops) == 0 {
		return false
	}
	seen := map[string]bool{}
	for _, shop := range shops {
		if !contains(allowed, shop) || seen[shop] {
			return false
		}
		seen[shop] = true
	}
	return true
}

func databaseDecisionStatus(err error) int {
	if errors.Is(err, pgx.ErrNoRows) {
		return 409
	}
	if status.Code(err) == codes.PermissionDenied {
		return 403
	}
	var pgError *pgconn.PgError
	if errors.As(err, &pgError) {
		switch pgError.Code {
		case "42501":
			return 403
		case "40001", "23505", "23503", "23514", "55000":
			return 409
		}
	}
	return 503
}
func contains(values []string, want string) bool {
	for _, value := range values {
		if value == want {
			return true
		}
	}
	return false
}

func allContained(allowed, requested []string) bool {
	for _, value := range requested {
		if !contains(allowed, value) {
			return false
		}
	}
	return true
}
func envOr(key, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}
func sessionTenantName(dbName string) string {
	switch dbName {
	case "Synthetic ecommerce demo v1":
		return "demo-tenant-a"
	case "Synthetic ecommerce tenant b":
		return "demo-tenant-b"
	case "M1 isolated prototype":
		return "m1-tenant"
	default:
		return ""
	}
}
