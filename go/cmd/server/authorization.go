package main

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"net"
	"net/http"
	"os"
	"slices"
	"strconv"
	"syscall"
	"time"

	ragv1 "github.com/ggb5023/EcommerceRAG/go/internal/pb/rag/v1"
	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func loadScopeSigningKey() ([]byte, error) {
	path := os.Getenv("SCOPE_SIGNING_KEY_FILE")
	if path == "" {
		path = "/etc/ecommerce-rag/synthetic-auth.key"
	}
	stat, err := os.Lstat(path)
	if err != nil {
		return nil, err
	}
	owner, ok := stat.Sys().(*syscall.Stat_t)
	if !ok || !stat.Mode().IsRegular() || stat.Mode().Perm() != 0600 || owner.Uid != uint32(os.Geteuid()) {
		return nil, errors.New("scope key must be an owned regular 0600 file")
	}
	key, err := os.ReadFile(path)
	if err != nil || len(key) < 32 {
		return nil, errors.New("scope key is missing or too short")
	}
	return key, nil
}

// Session is immutable; revalidation never changes shared request state.
func (g *gateway) refreshAuthorization(ctx context.Context) error {
	if g.db == nil {
		return nil
	}
	var role, revision, externalID, tenantStatus string
	var revoked *time.Time
	var shops []string
	err := g.db.QueryRow(ctx, `SELECT u.role,COALESCE(u.shop_ids,ARRAY[]::text[]),u.permission_revision,u.revoked_at,u.external_id,t.status
      FROM app_user u JOIN tenant t ON t.id=u.tenant_id WHERE u.tenant_id=$1 AND u.id=$2`, g.session.tenantID, g.session.userID).
		Scan(&role, &shops, &revision, &revoked, &externalID, &tenantStatus)
	if errors.Is(err, pgx.ErrNoRows) {
		return status.Error(codes.PermissionDenied, "identity_not_found")
	}
	if err != nil {
		return status.Error(codes.Unavailable, "authorization_unavailable")
	}
	if revoked != nil || tenantStatus != "active" {
		return status.Error(codes.PermissionDenied, "identity_revoked")
	}
	if role != g.session.role || revision != g.session.permissionRevision || externalID != g.session.user || !slices.Equal(shops, g.session.allowedShops) {
		return status.Error(codes.PermissionDenied, "permission_changed")
	}
	if !slices.Contains([]string{"owner", "admin", "operator", "viewer"}, role) {
		return status.Error(codes.PermissionDenied, "role_forbidden")
	}
	if len(shops) == 0 {
		return status.Error(codes.PermissionDenied, "no_shop_authorized")
	}
	if !slices.Contains(shops, g.session.shop) || (g.session.allShops && role != "owner" && role != "admin") {
		return status.Error(codes.PermissionDenied, "shop_not_authorized")
	}
	var active bool
	if err = g.db.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM shop WHERE tenant_id=$1 AND id=$2 AND status='active')`, g.session.tenantID, g.session.shop).Scan(&active); err != nil {
		return status.Error(codes.Unavailable, "authorization_unavailable")
	}
	if !active {
		return status.Error(codes.PermissionDenied, "shop_not_authorized")
	}
	return nil
}

func (g *gateway) requireWriteRole(w http.ResponseWriter) bool {
	if !slices.Contains([]string{"owner", "admin", "operator"}, g.session.role) {
		writeError(w, http.StatusForbidden, "role_forbidden", "current role cannot modify conversations")
		return false
	}
	return true
}

type resourceScope struct{ shop, disclosure string }

func authorizationHTTPStatus(err error) int {
	if status.Code(err) == codes.PermissionDenied {
		return http.StatusForbidden
	}
	return http.StatusServiceUnavailable
}

// Registered resources inherit shop scope unless an ACL gate narrows it.
// Every present tenant/source/document gate must grant this principal.
func (g *gateway) authorizedDocuments(ctx context.Context) (map[string]resourceScope, error) {
	rows, err := g.db.Query(ctx, `SELECT d.logical_key,s.shop_id,COALESCE(d.meta_json->>'disclosure_class','unclassified')
      FROM document d JOIN source s ON s.tenant_id=d.tenant_id AND s.id=d.source_id
      JOIN tenant t ON t.id=d.tenant_id
      JOIN shop sh ON sh.tenant_id=d.tenant_id AND sh.id=s.shop_id
      WHERE d.tenant_id=$1 AND d.status='active' AND t.status='active' AND sh.status='active'
        AND s.shop_id=$2 AND s.shop_id=ANY($3::text[])
        AND NOT EXISTS (
          SELECT 1 FROM (VALUES ('tenant',t.id),('source',s.id),('document',d.id)) gate(kind,id)
          WHERE EXISTS (SELECT 1 FROM acl a WHERE a.tenant_id=d.tenant_id AND a.resource_type=gate.kind AND a.resource_id=gate.id)
            AND NOT EXISTS (SELECT 1 FROM acl a WHERE a.tenant_id=d.tenant_id AND a.resource_type=gate.kind AND a.resource_id=gate.id
              AND a.permission IN ('read','write','admin') AND
              ((a.subject_type='user' AND a.subject_id IN ($4,$5)) OR (a.subject_type='role' AND a.subject_id=$6))))`,
		g.session.tenantID, g.session.shop, g.session.allowedShops, g.session.user, strconv.FormatInt(g.session.userID, 10), g.session.role)
	if err != nil {
		return nil, status.Error(codes.Unavailable, "authorization_unavailable")
	}
	defer rows.Close()
	result := map[string]resourceScope{}
	for rows.Next() {
		var id string
		var resource resourceScope
		if rows.Scan(&id, &resource.shop, &resource.disclosure) != nil {
			return nil, status.Error(codes.Unavailable, "authorization_unavailable")
		}
		result[id] = resource
	}
	if rows.Err() != nil {
		return nil, status.Error(codes.Unavailable, "authorization_unavailable")
	}
	return result, nil
}

func (g *gateway) evidenceAllowed(ctx context.Context, item *ragv1.Evidence) (bool, error) {
	if item == nil || item.DocumentId == "" || (item.TenantId != "" && item.TenantId != g.session.tenant) {
		return false, nil
	}
	if g.db == nil {
		return slices.Contains(g.session.allowedShops, item.ShopId), nil
	}
	resources, err := g.authorizedDocuments(ctx)
	if err != nil {
		return false, err
	}
	resource, found := resources[item.DocumentId]
	return found && resource.shop == item.ShopId && resource.disclosure == "external_allowed" &&
		(item.DisclosureClass == "" || item.DisclosureClass == resource.disclosure), nil
}

func scopeMAC(key []byte, scope *ragv1.RequestContext) []byte {
	unsigned := proto.Clone(scope).(*ragv1.RequestContext)
	unsigned.ScopeSignature = ""
	data, _ := proto.MarshalOptions{Deterministic: true}.Marshal(unsigned)
	mac := hmac.New(sha256.New, key)
	mac.Write(data)
	return mac.Sum(nil)
}

func (g *gateway) issueScope(ctx context.Context, requestID string) (*ragv1.RequestContext, error) {
	if err := g.refreshAuthorization(ctx); err != nil {
		return nil, err
	}
	resources, err := g.authorizedDocuments(ctx)
	if err != nil {
		return nil, err
	}
	ids := []string{}
	for id, resource := range resources {
		if resource.disclosure == "external_allowed" {
			ids = append(ids, id)
		}
	}
	slices.Sort(ids)
	scope := &ragv1.RequestContext{RequestId: requestID, TenantId: g.session.tenant, UserId: g.session.user,
		ShopId: g.session.shop, Role: g.session.role, AllowedShopIds: append([]string(nil), g.session.allowedShops...),
		AllShops: g.session.allShops, PermissionRevision: g.session.permissionRevision,
		AllowedDocumentIds: ids, EnforceDocumentScope: true, ScopeExpiresAt: time.Now().Add(turnTimeout + 5*time.Second).Unix(),
		TenantDbId: g.session.tenantID, UserDbId: g.session.userID}
	scope.ScopeSignature = hex.EncodeToString(scopeMAC(g.signingKey, scope))
	if err = g.refreshAuthorization(ctx); err != nil {
		return nil, err
	}
	return scope, nil
}

func (g *gateway) validateScope(ctx context.Context, scope *ragv1.RequestContext) error {
	signature, err := hex.DecodeString(scope.ScopeSignature)
	now := time.Now().Unix()
	if err != nil || len(g.signingKey) < 32 || !hmac.Equal(signature, scopeMAC(g.signingKey, scope)) ||
		scope.ScopeExpiresAt <= now || scope.ScopeExpiresAt > now+70 || !scope.EnforceDocumentScope || scope.RequestId == "" {
		return status.Error(codes.PermissionDenied, "invalid_scope")
	}
	var tenant string
	err = g.db.QueryRow(ctx, `SELECT CASE name WHEN 'M1 isolated prototype' THEN 'm1-tenant'
        WHEN 'Synthetic ecommerce demo v1' THEN 'demo-tenant-a'
        WHEN 'Synthetic ecommerce tenant b' THEN 'demo-tenant-b' ELSE '' END FROM tenant WHERE id=$1`, scope.TenantDbId).Scan(&tenant)
	if errors.Is(err, pgx.ErrNoRows) {
		return status.Error(codes.PermissionDenied, "invalid_scope")
	}
	if err != nil {
		return status.Error(codes.Unavailable, "authorization_unavailable")
	}
	if tenant == "" || tenant != scope.TenantId {
		return status.Error(codes.PermissionDenied, "invalid_scope")
	}
	verifier := &gateway{db: g.db, session: mockSession{tenantID: scope.TenantDbId, userID: scope.UserDbId,
		tenant: scope.TenantId, user: scope.UserId, shop: scope.ShopId, role: scope.Role,
		allowedShops: scope.AllowedShopIds, allShops: scope.AllShops, permissionRevision: scope.PermissionRevision}}
	if err = verifier.refreshAuthorization(ctx); err != nil {
		return err
	}
	resources, err := verifier.authorizedDocuments(ctx)
	if err != nil {
		return err
	}
	for _, id := range scope.AllowedDocumentIds {
		resource, found := resources[id]
		if !found || resource.disclosure != "external_allowed" {
			return status.Error(codes.PermissionDenied, "invalid_scope")
		}
	}
	return verifier.refreshAuthorization(ctx)
}

func (g *gateway) validateScopeHTTP(w http.ResponseWriter, r *http.Request) {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil || !net.ParseIP(host).IsLoopback() {
		writeError(w, 403, "forbidden", "internal endpoint")
		return
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 128<<10))
	scope := &ragv1.RequestContext{}
	if err != nil || proto.Unmarshal(body, scope) != nil {
		writeError(w, 400, "invalid_scope", "invalid scope")
		return
	}
	if err = g.validateScope(r.Context(), scope); err != nil {
		code := 403
		if status.Code(err) == codes.Unavailable {
			code = 503
		}
		writeError(w, code, authorizationFailureCode(err), "authorization failed")
		return
	}
	writeJSON(w, 200, map[string]bool{"allowed": true})
}

func evidenceEqual(a, b *ragv1.Evidence) bool { return a != nil && b != nil && proto.Equal(a, b) }

func (g *gateway) authorizeConversationHistory(w http.ResponseWriter, r *http.Request, id int64) bool {
	var stale bool
	err := g.db.QueryRow(r.Context(), `SELECT EXISTS(SELECT 1 FROM turn t JOIN conversation_execution e ON e.tenant_id=t.tenant_id AND e.turn_id=t.id
        WHERE t.tenant_id=$1 AND t.conversation_id=$2 AND e.permission_revision IS DISTINCT FROM $3)`, g.session.tenantID, id, g.session.permissionRevision).Scan(&stale)
	if err != nil {
		writeError(w, 503, "authorization_unavailable", "authorization failed")
		return false
	}
	if stale {
		writeError(w, 403, "execution_permission_changed", "historical execution scope is no longer valid")
		return false
	}
	return true
}

func (g *gateway) authorizeExecution(w http.ResponseWriter, r *http.Request, requestID string) bool {
	var revision *string
	err := g.db.QueryRow(r.Context(), `SELECT e.permission_revision FROM conversation_execution e JOIN turn t ON t.tenant_id=e.tenant_id AND t.id=e.turn_id
        JOIN conversation c ON c.tenant_id=t.tenant_id AND c.id=t.conversation_id
        WHERE e.tenant_id=$1 AND e.request_id=$2 AND c.user_id=$3 AND c.shop_id=$4`, g.session.tenantID, requestID, g.session.userID, g.session.shop).Scan(&revision)
	if errors.Is(err, pgx.ErrNoRows) {
		writeError(w, 404, "not_found", "turn not found")
		return false
	}
	if err != nil {
		writeError(w, 503, "authorization_unavailable", "authorization failed")
		return false
	}
	if revision == nil || *revision != g.session.permissionRevision {
		writeError(w, 403, "execution_permission_changed", "historical execution scope is no longer valid")
		return false
	}
	return true
}
