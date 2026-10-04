package main

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"path"
	"sort"
	"strconv"
	"strings"
	"time"

	ragv1 "github.com/ggb5023/EcommerceRAG/go/internal/pb/rag/v1"
	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

const (
	ingestLeaseDuration = 45 * time.Second
	ingestWorkerPoll    = 900 * time.Millisecond
	ingestMaxAttempts   = 5
)

type queuedPackage struct {
	JobID          int64
	ManifestSHA256 string
	PackageSHA256  string
	ItemCount      int
	Status         string
}

type ingestClaimFile struct {
	ItemID     int64
	ItemEpoch  int64
	DocumentID string
	Path       string
	SourceHash string
	MediaType  string
	Content    []byte
	OwnerToken string
}

type ingestClaim struct {
	TenantID    int64
	JobID       int64
	SourceID    int64
	SubmittedBy int64
	JobEpoch    int64
	RetryCount  int
	MaxAttempts int
	OwnerToken  string
	Manifest    string
	ManifestSHA string
	PackageSHA  string
	ShopID      string
	WorkerID    string
	Files       []ingestClaimFile
}

type adminJobConfirmation struct {
	Confirm              bool   `json:"confirm"`
	TenantID             string `json:"tenant_id"`
	ShopID               string `json:"shop_id"`
	JobID                string `json:"job_id"`
	PermissionRevision   string `json:"permission_revision"`
	ExpectedFencingEpoch int64  `json:"expected_fencing_epoch"`
	Reason               string `json:"reason"`
}

var errIngestLeaseLost = errors.New("ingest lease lost")

func (g *gateway) queuePackage(ctx context.Context, manifest string, files []*ragv1.PackageFile,
	tenantID, userID int64, shopID string) (queuedPackage, error) {
	validated, err := g.ingest.ValidatePackage(ctx, &ragv1.ValidatePackageRequest{
		ManifestYaml: manifest, Files: files, TenantId: g.session.tenant, ShopId: shopID,
	})
	if err != nil {
		return queuedPackage{}, err
	}
	if len(validated.Documents) == 0 || len(validated.Documents) != len(files) {
		return queuedPackage{}, errors.New("validated package file count changed")
	}
	manifestSHA := validated.ManifestSha256
	jobKey := "admin:" + shopID + ":" + validated.SourceId + ":" + manifestSHA
	fileByPath := make(map[string]*ragv1.PackageFile, len(files))
	fileHash := make(map[string]string, len(files))
	orderedPaths := make([]string, 0, len(files))
	packageHasher := sha256.New()
	_, _ = io.WriteString(packageHasher, strconv.FormatInt(tenantID, 10)+"\x00"+shopID+"\x00"+manifestSHA+"\x00")
	var total int
	for _, file := range files {
		if file == nil || file.Path == "" || strings.Contains(file.Path, "\\") || path.IsAbs(file.Path) || path.Clean(file.Path) != file.Path {
			return queuedPackage{}, errors.New("package contains an unsafe path")
		}
		if _, exists := fileByPath[file.Path]; exists {
			return queuedPackage{}, errors.New("package contains a duplicate path")
		}
		total += len(file.Content)
		if total > 8*1024*1024 {
			return queuedPackage{}, errors.New("package exceeds the allowed size")
		}
		digest := sha256.Sum256(file.Content)
		fileByPath[file.Path] = file
		fileHash[file.Path] = hex.EncodeToString(digest[:])
		orderedPaths = append(orderedPaths, file.Path)
	}
	sort.Strings(orderedPaths)
	for _, relative := range orderedPaths {
		_, _ = io.WriteString(packageHasher, relative+"\x00"+fileHash[relative]+"\x00")
	}
	packageSHA := hex.EncodeToString(packageHasher.Sum(nil))

	tx, err := g.db.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return queuedPackage{}, err
	}
	defer tx.Rollback(ctx)
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtext($1)::bigint)`, strconv.FormatInt(tenantID, 10)+":"+jobKey); err != nil {
		return queuedPackage{}, err
	}
	var exists queuedPackage
	err = tx.QueryRow(ctx, `SELECT id,manifest_sha256,COALESCE(dataset_sha256,''),status
		FROM ingest_job WHERE tenant_id=$1 AND idempotency_key=$2`, tenantID, jobKey).
		Scan(&exists.JobID, &exists.ManifestSHA256, &exists.PackageSHA256, &exists.Status)
	if err == nil {
		var recorded string
		payloadErr := tx.QueryRow(ctx, `SELECT payload_hash FROM ingest_payload WHERE tenant_id=$1 AND job_id=$2`, tenantID, exists.JobID).Scan(&recorded)
		if payloadErr == nil && recorded != packageSHA {
			return queuedPackage{}, &adminConflictError{message: "idempotency key was reused with different package bytes"}
		}
		if payloadErr != nil && !errors.Is(payloadErr, pgx.ErrNoRows) {
			return queuedPackage{}, payloadErr
		}
		if err = tx.QueryRow(ctx, `SELECT count(*) FROM ingest_job_item WHERE tenant_id=$1 AND job_id=$2`, tenantID, exists.JobID).Scan(&exists.ItemCount); err != nil {
			return queuedPackage{}, err
		}
		if err = tx.Commit(ctx); err != nil {
			return queuedPackage{}, err
		}
		exists.ManifestSHA256 = manifestSHA
		exists.PackageSHA256 = packageSHA
		return exists, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return queuedPackage{}, err
	}
	var authorized bool
	err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM admin_role_grant grant_row
		JOIN app_user u ON u.tenant_id=grant_row.tenant_id AND u.id=grant_row.user_id
		JOIN tenant t ON t.id=u.tenant_id
		JOIN shop s ON s.tenant_id=t.id AND s.id=$3 AND s.status='active'
		WHERE grant_row.tenant_id=$1 AND grant_row.user_id=$2 AND grant_row.role='merchant_admin'
		AND grant_row.status='active' AND u.revoked_at IS NULL AND t.status='active'
			AND (grant_row.scope_kind='tenant' OR grant_row.shop_ids @> ARRAY[$3]::text[]))`,
		tenantID, userID, shopID).Scan(&authorized)
	if err != nil {
		return queuedPackage{}, err
	}
	if !authorized {
		return queuedPackage{}, status.Error(codes.PermissionDenied, "submitter no longer has active shop management scope")
	}
	var sourceRowID int64
	err = tx.QueryRow(ctx, `INSERT INTO source(tenant_id,shop_id,type,uri)
		VALUES($1,$2,'admin_manifest',$3) ON CONFLICT(tenant_id,shop_id,type,uri) WHERE uri IS NOT NULL
		DO UPDATE SET uri=EXCLUDED.uri RETURNING id`, tenantID, shopID, "admin://"+validated.SourceId).Scan(&sourceRowID)
	if err != nil {
		return queuedPackage{}, err
	}
	var jobID int64
	err = tx.QueryRow(ctx, `INSERT INTO ingest_job(tenant_id,source_id,idempotency_key,stage,status,manifest_sha256,
		submitted_by,next_attempt_at,max_attempts)
		VALUES($1,$2,$3,'queued','pending',$4,$5,now(),$6) RETURNING id`,
		tenantID, sourceRowID, jobKey, manifestSHA, userID, ingestMaxAttempts).Scan(&jobID)
	if err != nil {
		return queuedPackage{}, err
	}
	if _, err = tx.Exec(ctx, `INSERT INTO ingest_payload(tenant_id,job_id,idempotency_key,payload_hash)
		VALUES($1,$2,$3,$4)`, tenantID, jobID, jobKey, packageSHA); err != nil {
		return queuedPackage{}, err
	}
	if _, err = tx.Exec(ctx, `INSERT INTO ingest_package_upload(tenant_id,job_id,manifest_yaml,package_sha256,uploaded_by)
		VALUES($1,$2,$3,$4,$5)`, tenantID, jobID, manifest, packageSHA, userID); err != nil {
		return queuedPackage{}, err
	}
	itemByDocument := make(map[string]int64, len(validated.Documents))
	for _, document := range validated.Documents {
		file, ok := fileByPath[document.Path]
		if !ok || fileHash[document.Path] != document.SourceHash {
			return queuedPackage{}, &adminConflictError{message: "validated package metadata changed before queueing"}
		}
		media := mediaTypeForPackagePath(document.Path)
		var documentRow int64
		err = tx.QueryRow(ctx, `INSERT INTO document(tenant_id,source_id,logical_key,title,doc_type,meta_json,status)
			VALUES($1,$2,$3,$4,$5,jsonb_build_object('shop_id',$6::text,'disclosure_class',$7::text,'external_allowed',false),'pending')
			ON CONFLICT(tenant_id,logical_key) DO UPDATE SET title=EXCLUDED.title,
				status=CASE WHEN document.status='revoked' THEN 'pending' ELSE document.status END,
				active_version_id=CASE WHEN document.status='revoked' THEN NULL ELSE document.active_version_id END
			WHERE document.source_id=EXCLUDED.source_id RETURNING id`, tenantID, sourceRowID, document.DocumentId,
			document.Title, document.Format, shopID, document.DisclosureClass).Scan(&documentRow)
		if errors.Is(err, pgx.ErrNoRows) {
			return queuedPackage{}, &adminConflictError{message: "document identifier is already owned by another source"}
		}
		if err != nil {
			return queuedPackage{}, err
		}
		objectKey := "db://ingest_package_file/" + strconv.FormatInt(jobID, 10) + "/" + document.Path
		var versionID int64
		err = tx.QueryRow(ctx, `INSERT INTO document_version(tenant_id,document_id,source_hash,parser_version,chunk_rule_version,
			embedding_model,pipeline_version,object_key,status,disclosure_class,external_allowed,quality_json)
			VALUES($1,$2,$3,'deterministic-parser-v1','deterministic-chunk-v1','not_run',$4,$5,'building',$6,false,
			' {"parse_status":"pending","embedding_status":"not_run","index_mode":"deterministic_keyword","quality_gate":"pending","chunk_count":0,"empty_chunk_count":0}'::jsonb)
			RETURNING id`, tenantID, documentRow, document.SourceHash, validated.PipelineVersion, objectKey,
			document.DisclosureClass).Scan(&versionID)
		if err != nil {
			return queuedPackage{}, err
		}
		var itemID int64
		err = tx.QueryRow(ctx, `INSERT INTO ingest_job_item(tenant_id,job_id,data_id,payload_hash,status,stage,document_version_id)
			VALUES($1,$2,$3,$4,'pending','queued',$5) RETURNING id`, tenantID, jobID,
			document.DocumentId, document.SourceHash, versionID).Scan(&itemID)
		if err != nil {
			return queuedPackage{}, err
		}
		itemByDocument[document.DocumentId] = itemID
		if _, err = tx.Exec(ctx, `INSERT INTO ingest_package_file(tenant_id,job_id,item_id,relative_path,media_type,content_sha256,size_bytes,content)
			VALUES($1,$2,$3,$4,$5,$6,$7,$8)`, tenantID, jobID, itemID, document.Path, media,
			document.SourceHash, len(file.Content), file.Content); err != nil {
			return queuedPackage{}, err
		}
	}
	if len(itemByDocument) != len(validated.Documents) {
		return queuedPackage{}, errors.New("validated document count changed while queueing")
	}
	if err = auditAdmin(ctx, tx, tenantID, userID, "ingestion.queued", "ingest_job", strconv.FormatInt(jobID, 10),
		"", packageSHA, 0, "validated package queued for isolated parsing", "success",
		map[string]any{"shop_id": shopID, "source_id": validated.SourceId, "manifest_sha256": manifestSHA,
			"document_count": len(validated.Documents)}); err != nil {
		return queuedPackage{}, err
	}
	if err = tx.Commit(ctx); err != nil {
		return queuedPackage{}, err
	}
	return queuedPackage{JobID: jobID, ManifestSHA256: manifestSHA, PackageSHA256: packageSHA,
		ItemCount: len(validated.Documents), Status: "pending"}, nil
}

func mediaTypeForPackagePath(relative string) string {
	switch strings.ToLower(path.Ext(relative)) {
	case ".csv":
		return "text/csv"
	case ".docx":
		return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
	default:
		return "text/markdown"
	}
}

func newWorkerToken(prefix string) (string, error) {
	var raw [16]byte
	if _, err := rand.Read(raw[:]); err != nil {
		return "", err
	}
	return prefix + hex.EncodeToString(raw[:]), nil
}

func (g *gateway) runAdminIngestWorker(ctx context.Context) {
	workerID, err := newWorkerToken("ingest-")
	if err != nil {
		return
	}
	started := time.Now().UTC()
	if err = g.writeWorkerHeartbeat(ctx, workerID, started, "idle", nil, "", 0); err != nil {
		return
	}
	defer func() {
		stopCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_, _ = g.db.Exec(stopCtx, `UPDATE ingest_worker_heartbeat SET status='stopping',heartbeat_at=now(),
			active_tenant_id=NULL,active_job_id=NULL WHERE worker_id=$1`, workerID)
	}()

	for {
		if ctx.Err() != nil {
			return
		}
		claim, claimErr := g.claimNextIngestJob(ctx, workerID)
		if claimErr != nil {
			log.Printf("ingestion worker claim failed: %v", claimErr)
			_ = g.writeWorkerHeartbeat(ctx, workerID, started, "degraded", nil, "claim_failed", 0)
			if !workerWait(ctx, 2*time.Second) {
				return
			}
			continue
		}
		if claim == nil {
			_ = g.writeWorkerHeartbeat(ctx, workerID, started, "idle", nil, "", 0)
			if !workerWait(ctx, ingestWorkerPoll) {
				return
			}
			continue
		}
		_ = g.writeWorkerHeartbeat(ctx, workerID, started, "working", claim, "", 0)
		result, processErr := g.processIngestClaim(ctx, claim)
		state := "idle"
		errorCode := ""
		completed, failed := int64(0), int64(0)
		if processErr != nil {
			state, errorCode, failed = "degraded", result, 1
		} else if result == "completed" {
			completed = 1
		} else if result == "failed" {
			failed = 1
		}
		_ = g.writeWorkerHeartbeat(ctx, workerID, started, state, nil, errorCode, completed, failed)
	}
}

func workerWait(ctx context.Context, duration time.Duration) bool {
	timer := time.NewTimer(duration)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}

func (g *gateway) writeWorkerHeartbeat(ctx context.Context, workerID string, started time.Time,
	state string, claim *ingestClaim, errorCode string, counters ...int64) error {
	var tenantID, jobID any
	if claim != nil {
		tenantID, jobID = claim.TenantID, claim.JobID
	}
	var completed, failed int64
	if len(counters) > 0 {
		completed = counters[0]
	}
	if len(counters) > 1 {
		failed = counters[1]
	}
	_, err := g.db.Exec(ctx, `INSERT INTO ingest_worker_heartbeat(worker_id,status,started_at,heartbeat_at,
		active_tenant_id,active_job_id,completed_jobs,failed_jobs,last_error_code)
		VALUES($1,$2,$3,now(),$4,$5,$6,$7,NULLIF($8,''))
		ON CONFLICT(worker_id) DO UPDATE SET status=EXCLUDED.status,heartbeat_at=now(),
		active_tenant_id=EXCLUDED.active_tenant_id,active_job_id=EXCLUDED.active_job_id,
		completed_jobs=ingest_worker_heartbeat.completed_jobs+EXCLUDED.completed_jobs,
		failed_jobs=ingest_worker_heartbeat.failed_jobs+EXCLUDED.failed_jobs,last_error_code=EXCLUDED.last_error_code`,
		workerID, state, started, tenantID, jobID, completed, failed, errorCode)
	return err
}

func (g *gateway) claimNextIngestJob(ctx context.Context, workerID string) (*ingestClaim, error) {
	tx, err := g.db.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	var claim ingestClaim
	err = tx.QueryRow(ctx, `SELECT j.tenant_id,j.id,j.source_id,j.submitted_by,j.fencing_epoch,j.retry_count,
		j.max_attempts,u.manifest_yaml,u.package_sha256,s.shop_id,j.manifest_sha256
		FROM ingest_job j JOIN source s ON s.tenant_id=j.tenant_id AND s.id=j.source_id
		JOIN ingest_package_upload u ON u.tenant_id=j.tenant_id AND u.job_id=j.id
		WHERE NOT j.cancel_requested AND j.next_attempt_at<=now()
		AND (j.status='pending' OR (j.status='running' AND j.lease_expires_at<=now()))
		ORDER BY j.next_attempt_at,j.created_at,j.id LIMIT 1 FOR UPDATE OF j SKIP LOCKED`).
		Scan(&claim.TenantID, &claim.JobID, &claim.SourceID, &claim.SubmittedBy, &claim.JobEpoch,
			&claim.RetryCount, &claim.MaxAttempts, &claim.Manifest, &claim.PackageSHA, &claim.ShopID, &claim.ManifestSHA)
	if errors.Is(err, pgx.ErrNoRows) {
		if err = tx.Commit(ctx); err != nil {
			return nil, err
		}
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	claim.WorkerID = workerID
	claim.JobEpoch++
	claim.OwnerToken, err = newWorkerToken(workerID + ":")
	if err != nil {
		return nil, err
	}
	if _, err = tx.Exec(ctx, `UPDATE ingest_job SET status='running',stage='parsing',owner_token=$3,
		fencing_epoch=$4,lease_expires_at=now()+$5::interval,updated_at=now(),error_code=NULL
		WHERE tenant_id=$1 AND id=$2`, claim.TenantID, claim.JobID, claim.OwnerToken, claim.JobEpoch,
		leaseInterval(ingestLeaseDuration)); err != nil {
		return nil, err
	}
	rows, err := tx.Query(ctx, `SELECT f.item_id,i.fencing_epoch,i.data_id,f.relative_path,f.content_sha256,
		f.media_type,f.content FROM ingest_package_file f JOIN ingest_job_item i
		ON i.tenant_id=f.tenant_id AND i.id=f.item_id
		WHERE f.tenant_id=$1 AND f.job_id=$2 AND i.cancel_requested=false
		AND i.status IN ('pending','running') AND i.next_attempt_at<=now()
		ORDER BY f.relative_path FOR UPDATE OF i`, claim.TenantID, claim.JobID)
	if err != nil {
		return nil, err
	}
	for rows.Next() {
		var file ingestClaimFile
		if err = rows.Scan(&file.ItemID, &file.ItemEpoch, &file.DocumentID, &file.Path, &file.SourceHash,
			&file.MediaType, &file.Content); err != nil {
			rows.Close()
			return nil, err
		}
		file.OwnerToken = claim.OwnerToken + ":item:" + strconv.FormatInt(file.ItemID, 10)
		file.ItemEpoch++
		claim.Files = append(claim.Files, file)
	}
	if err = rows.Err(); err != nil {
		rows.Close()
		return nil, err
	}
	rows.Close()
	if len(claim.Files) == 0 {
		return nil, errors.New("queued job has no claimable items")
	}
	for _, file := range claim.Files {
		result, updateErr := tx.Exec(ctx, `UPDATE ingest_job_item SET status='running',stage='parsing',owner_token=$3,
			fencing_epoch=$4,lease_expires_at=now()+$5::interval,error_code=NULL,error=NULL
			WHERE tenant_id=$1 AND id=$2 AND status IN ('pending','running') AND cancel_requested=false
			AND next_attempt_at<=now()`, claim.TenantID, file.ItemID, file.OwnerToken, file.ItemEpoch,
			leaseInterval(ingestLeaseDuration))
		if updateErr != nil {
			return nil, updateErr
		}
		if result.RowsAffected() != 1 {
			return nil, errIngestLeaseLost
		}
	}
	if err = tx.Commit(ctx); err != nil {
		return nil, err
	}
	return &claim, nil
}

func leaseInterval(duration time.Duration) string {
	return fmt.Sprintf("%d seconds", int(duration.Seconds()))
}

func (g *gateway) processIngestClaim(parent context.Context, claim *ingestClaim) (string, error) {
	parseCtx, cancelParse := context.WithCancel(parent)
	renewCtx, cancelRenew := context.WithCancel(parent)
	renewDone := make(chan struct{})
	go func() {
		defer close(renewDone)
		ticker := time.NewTicker(ingestLeaseDuration / 3)
		defer ticker.Stop()
		for {
			select {
			case <-renewCtx.Done():
				return
			case <-ticker.C:
				owned, err := g.renewIngestClaim(renewCtx, claim)
				if err != nil || !owned {
					cancelParse()
					return
				}
				if err = g.writeWorkerHeartbeat(renewCtx, claim.WorkerID, time.Now().UTC(),
					"working", claim, "", 0); err != nil {
					cancelParse()
					return
				}
			}
		}
	}()

	files := make([]*ragv1.PackageFile, 0, len(claim.Files))
	for _, file := range claim.Files {
		files = append(files, &ragv1.PackageFile{Path: file.Path, Content: file.Content})
	}
	parseCallCtx, callCancel := context.WithTimeout(parseCtx, 2*time.Minute)
	parsed, parseErr := g.ingest.ParsePackage(parseCallCtx, &ragv1.ParsePackageRequest{
		ManifestYaml: claim.Manifest, Files: files, TenantId: strconv.FormatInt(claim.TenantID, 10), ShopId: claim.ShopID,
	})
	callCancel()
	cancelRenew()
	<-renewDone
	if parent.Err() != nil {
		cancelParse()
		return "", parent.Err()
	}
	if parseErr != nil {
		cancelParse()
		code := "parse_failed"
		retryable := status.Code(parseErr) != codes.InvalidArgument
		if status.Code(parseErr) == codes.Unavailable || status.Code(parseErr) == codes.DeadlineExceeded ||
			status.Code(parseErr) == codes.ResourceExhausted || status.Code(parseErr) == codes.Internal {
			code = "parser_unavailable"
		}
		state, settleErr := g.settleIngestFailure(claim, code, retryable)
		return state, settleErr
	}
	if parsed.ManifestSha256 != claim.ManifestSHA {
		cancelParse()
		state, settleErr := g.settleIngestFailure(claim, "manifest_changed", false)
		return state, settleErr
	}
	completeErr := g.completeParsedIngestClaim(parseCtx, claim, parsed)
	cancelParse()
	if completeErr == nil {
		return "completed", nil
	}
	if errors.Is(completeErr, errIngestLeaseLost) {
		return "lease_lost", completeErr
	}
	var conflict *adminConflictError
	if errors.As(completeErr, &conflict) {
		state, settleErr := g.settleIngestFailure(claim, "document_conflict", false)
		return state, settleErr
	}
	return "completion_deferred", completeErr
}

func (g *gateway) renewIngestClaim(ctx context.Context, claim *ingestClaim) (bool, error) {
	tx, err := g.db.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return false, err
	}
	defer tx.Rollback(ctx)
	result, err := tx.Exec(ctx, `UPDATE ingest_job SET lease_expires_at=now()+$4::interval,updated_at=now()
		WHERE tenant_id=$1 AND id=$2 AND status='running' AND owner_token=$3 AND fencing_epoch=$5
		AND lease_expires_at>now() AND NOT cancel_requested`, claim.TenantID, claim.JobID,
		claim.OwnerToken, leaseInterval(ingestLeaseDuration), claim.JobEpoch)
	if err != nil {
		return false, err
	}
	if result.RowsAffected() != 1 {
		return false, tx.Commit(ctx)
	}
	for _, file := range claim.Files {
		result, err = tx.Exec(ctx, `UPDATE ingest_job_item SET lease_expires_at=now()+$5::interval
			WHERE tenant_id=$1 AND id=$2 AND status='running' AND owner_token=$3 AND fencing_epoch=$4
			AND lease_expires_at>now() AND NOT cancel_requested`, claim.TenantID, file.ItemID,
			file.OwnerToken, file.ItemEpoch, leaseInterval(ingestLeaseDuration))
		if err != nil {
			return false, err
		}
		if result.RowsAffected() != 1 {
			return false, tx.Commit(ctx)
		}
	}
	if err = tx.Commit(ctx); err != nil {
		return false, err
	}
	return true, nil
}

func (g *gateway) completeParsedIngestClaim(ctx context.Context, claim *ingestClaim,
	parsed *ragv1.ParsePackageResponse) error {
	tx, err := g.db.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	var sourceID, submittedBy int64
	var shopID string
	var manifestSHA string
	err = tx.QueryRow(ctx, `SELECT j.source_id,j.submitted_by,s.shop_id,j.manifest_sha256
		FROM ingest_job j JOIN source s ON s.tenant_id=j.tenant_id AND s.id=j.source_id
		WHERE j.tenant_id=$1 AND j.id=$2 AND j.status='running' AND j.owner_token=$3
		AND j.fencing_epoch=$4 AND j.lease_expires_at>now() AND NOT j.cancel_requested
		FOR UPDATE OF j`, claim.TenantID, claim.JobID, claim.OwnerToken, claim.JobEpoch).
		Scan(&sourceID, &submittedBy, &shopID, &manifestSHA)
	if errors.Is(err, pgx.ErrNoRows) {
		return errIngestLeaseLost
	}
	if err != nil {
		return err
	}
	if sourceID != claim.SourceID || submittedBy != claim.SubmittedBy || shopID != claim.ShopID || manifestSHA != parsed.ManifestSha256 {
		return &adminConflictError{message: "queued package identity changed"}
	}
	var authorized bool
	err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM admin_role_grant grant_row
		JOIN app_user u ON u.tenant_id=grant_row.tenant_id AND u.id=grant_row.user_id
		JOIN tenant t ON t.id=u.tenant_id
		WHERE grant_row.tenant_id=$1 AND grant_row.user_id=$2 AND grant_row.role='merchant_admin'
		AND grant_row.status='active' AND u.revoked_at IS NULL AND t.status='active'
		AND (grant_row.scope_kind='tenant' OR grant_row.shop_ids @> ARRAY[$3]::text[]))`,
		claim.TenantID, submittedBy, shopID).Scan(&authorized)
	if err != nil {
		return err
	}
	if !authorized {
		return &adminConflictError{message: "submitter authorization was revoked before processing completed"}
	}
	if len(parsed.Documents) != len(claim.Files) {
		return &adminConflictError{message: "parsed document count does not match queued items"}
	}
	fileByPath := make(map[string]ingestClaimFile, len(claim.Files))
	for _, file := range claim.Files {
		fileByPath[file.Path] = file
	}
	var manifestID int64
	err = tx.QueryRow(ctx, `INSERT INTO source_manifest(tenant_id,source_id,manifest_sha256,dataset_sha256,
		pipeline_version,manifest_yaml,uploaded_by) SELECT $1,$2,$3,$4,$5,u.manifest_yaml,$6
		FROM ingest_package_upload u WHERE u.tenant_id=$1 AND u.job_id=$7 RETURNING id`,
		claim.TenantID, sourceID, parsed.ManifestSha256, parsed.DatasetSha256, parsed.PipelineVersion,
		submittedBy, claim.JobID).Scan(&manifestID)
	if err != nil {
		return err
	}
	for _, document := range parsed.Documents {
		file, exists := fileByPath[document.Path]
		if !exists || file.DocumentID != document.DocumentId || file.SourceHash != document.SourceHash {
			return &adminConflictError{message: "parsed document no longer matches its queued item"}
		}
		var itemVersionID, itemID int64
		err = tx.QueryRow(ctx, `SELECT i.id,i.document_version_id FROM ingest_job_item i
			WHERE i.tenant_id=$1 AND i.job_id=$2 AND i.id=$3 AND i.status='running'
			AND i.owner_token=$4 AND i.fencing_epoch=$5 AND i.lease_expires_at>now()
			AND NOT i.cancel_requested FOR UPDATE`, claim.TenantID, claim.JobID, file.ItemID,
			file.OwnerToken, file.ItemEpoch).Scan(&itemID, &itemVersionID)
		if errors.Is(err, pgx.ErrNoRows) {
			return errIngestLeaseLost
		}
		if err != nil {
			return err
		}
		var fileID int64
		err = tx.QueryRow(ctx, `INSERT INTO source_file(tenant_id,source_manifest_id,document_id,relative_path,
			media_type,content_sha256,size_bytes,content)
			SELECT f.tenant_id,$3,$4,f.relative_path,f.media_type,f.content_sha256,f.size_bytes,f.content
			FROM ingest_package_file f WHERE f.tenant_id=$1 AND f.job_id=$2 AND f.item_id=$5 RETURNING id`,
			claim.TenantID, claim.JobID, manifestID, document.DocumentId, itemID).Scan(&fileID)
		if err != nil {
			return err
		}
		var documentRow int64
		err = tx.QueryRow(ctx, `SELECT d.id FROM document d JOIN document_version v
			ON v.tenant_id=d.tenant_id AND v.document_id=d.id
			WHERE d.tenant_id=$1 AND v.id=$2 AND d.logical_key=$3 FOR UPDATE OF d,v`,
			claim.TenantID, itemVersionID, document.DocumentId).Scan(&documentRow)
		if err != nil {
			return err
		}
		quality := fmt.Sprintf(`{"chunk_count":%d,"empty_chunk_count":0,"parse_status":"passed","embedding_status":"not_run","index_mode":"deterministic_keyword","quality_gate":"passed"}`, len(document.Chunks))
		result, err := tx.Exec(ctx, `UPDATE document_version SET status='ready',chunk_count=$3,source_manifest_id=$4,
			parsed_object_key=$5,quality_json=$6::jsonb
			WHERE tenant_id=$1 AND id=$2 AND status='building' AND source_hash=$7`,
			claim.TenantID, itemVersionID, len(document.Chunks), manifestID,
			"db://source_file/"+strconv.FormatInt(fileID, 10), quality, document.SourceHash)
		if err != nil {
			return err
		}
		if result.RowsAffected() != 1 {
			return &adminConflictError{message: "building document version changed before parse commit"}
		}
		for _, chunk := range document.Chunks {
			if !jsonValid(chunk.MetadataJson) {
				return &adminConflictError{message: "parser returned invalid chunk metadata"}
			}
			_, err = tx.Exec(ctx, `INSERT INTO chunk(version_id,doc_id,tenant_id,shop_id,chunk_index,section_seq,section_chunk_index,
				char_start,char_end,heading_path,content,embed_text,token_count,content_type,split_reason,embedding_model,embedding_dim,
				tokenizer_id,effective_from,effective_to,meta_json,source_object_key,parsed_object_key,chunk_hash)
				VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$11,$12,$13,$14,'not_run',1024,'unicode-char-v1',
				$15::date,$16::date,$17::jsonb,$18,$19,$20)`, itemVersionID, documentRow, claim.TenantID, shopID,
				chunk.ChunkIndex, chunk.SectionSeq, chunk.SectionChunkIndex, chunk.CharStart, chunk.CharEnd,
				chunk.HeadingPath, chunk.Content, chunk.TokenCount, chunk.ContentType, chunk.SplitReason,
				nullableDate(stringPointer(document.EffectiveFrom)), nullableDate(stringPointer(document.EffectiveTo)),
				chunk.MetadataJson, "db://source_file/"+strconv.FormatInt(fileID, 10),
				"db://source_file/"+strconv.FormatInt(fileID, 10), chunk.ChunkHash)
			if err != nil {
				return err
			}
		}
		_, err = tx.Exec(ctx, `UPDATE ingest_job_item SET status='awaiting_review',stage='review',
			document_version_id=$4,error=NULL,error_code=NULL,lease_expires_at=NULL
			WHERE tenant_id=$1 AND job_id=$2 AND id=$3 AND owner_token=$5 AND fencing_epoch=$6`,
			claim.TenantID, claim.JobID, itemID, itemVersionID, file.OwnerToken, file.ItemEpoch)
		if err != nil {
			return err
		}
	}
	_, err = tx.Exec(ctx, `UPDATE ingest_job SET status='awaiting_review',stage='review',dataset_sha256=$3,
		owner_token=NULL,lease_expires_at=NULL,updated_at=now(),error=NULL,error_code=NULL
		WHERE tenant_id=$1 AND id=$2 AND owner_token=$4 AND fencing_epoch=$5`,
		claim.TenantID, claim.JobID, parsed.DatasetSha256, claim.OwnerToken, claim.JobEpoch)
	if err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `DELETE FROM ingest_package_file WHERE tenant_id=$1 AND job_id=$2`, claim.TenantID, claim.JobID); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `DELETE FROM ingest_package_upload WHERE tenant_id=$1 AND job_id=$2`, claim.TenantID, claim.JobID); err != nil {
		return err
	}
	if err = auditAdmin(ctx, tx, claim.TenantID, submittedBy, "ingestion.parsed", "source_manifest", strconv.FormatInt(manifestID, 10),
		strconv.FormatInt(claim.JobID, 10), parsed.DatasetSha256, claim.JobEpoch,
		"worker parsed package and passed deterministic quality gate", "success",
		map[string]any{"shop_id": shopID, "document_count": len(parsed.Documents)}); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func stringPointer(value string) *string {
	if value == "" {
		return nil
	}
	return &value
}

func jsonValid(value string) bool {
	return json.Valid([]byte(value))
}

func (g *gateway) settleIngestFailure(claim *ingestClaim, errorCode string, retryable bool) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	tx, err := g.db.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return "deferred", err
	}
	defer tx.Rollback(ctx)
	var cancelRequested bool
	err = tx.QueryRow(ctx, `SELECT cancel_requested FROM ingest_job WHERE tenant_id=$1 AND id=$2
		AND status='running' AND owner_token=$3 AND fencing_epoch=$4 FOR UPDATE`,
		claim.TenantID, claim.JobID, claim.OwnerToken, claim.JobEpoch).Scan(&cancelRequested)
	if errors.Is(err, pgx.ErrNoRows) {
		return "lease_lost", errIngestLeaseLost
	}
	if err != nil {
		return "deferred", err
	}
	var nextStatus, nextStage, state string
	var retryAt any
	if cancelRequested {
		nextStatus, nextStage, state = "cancelled", "cancelled", "cancelled"
	} else if retryable && claim.RetryCount+1 < claim.MaxAttempts {
		nextStatus, nextStage, state = "pending", "retry_wait", "retrying"
		delay := time.Duration(1<<min(claim.RetryCount, 5)) * time.Second
		retryAt = time.Now().UTC().Add(delay)
	} else {
		nextStatus, nextStage, state = "failed", "failed", "failed"
	}
	_, err = tx.Exec(ctx, `UPDATE ingest_job SET status=$3,stage=$4,retry_count=retry_count+CASE WHEN $3='pending' THEN 1 ELSE 0 END,
		next_attempt_at=COALESCE($5,now()),owner_token=NULL,lease_expires_at=NULL,error_code=$6,
		error=$7,updated_at=now() WHERE tenant_id=$1 AND id=$2`,
		claim.TenantID, claim.JobID, nextStatus, nextStage, retryAt, errorCode, "package processing did not complete")
	if err != nil {
		return "deferred", err
	}
	if nextStatus == "pending" {
		_, err = tx.Exec(ctx, `UPDATE ingest_job_item SET status='pending',stage='queued',retry_count=retry_count+1,
			next_attempt_at=$3,lease_expires_at=NULL,error_code=$4,error=$5
			WHERE tenant_id=$1 AND job_id=$2 AND status='running'`, claim.TenantID, claim.JobID, retryAt, errorCode, "parser will retry")
	} else {
		_, err = tx.Exec(ctx, `UPDATE ingest_job_item SET status=$3,stage=$4,lease_expires_at=NULL,
			error_code=$5,error=$6 WHERE tenant_id=$1 AND job_id=$2 AND status='running'`,
			claim.TenantID, claim.JobID, nextStatus, nextStage, errorCode, "package processing did not complete")
		if err == nil {
			_, err = tx.Exec(ctx, `UPDATE document_version v SET status='failed',quality_json=quality_json ||
				jsonb_build_object('parse_status',$3::text,'quality_gate','failed') FROM ingest_job_item i
				WHERE i.tenant_id=$1 AND i.job_id=$2 AND i.document_version_id=v.id
				AND v.tenant_id=i.tenant_id AND v.status='building'`, claim.TenantID, claim.JobID, state)
		}
	}
	if err != nil {
		return "deferred", err
	}
	if err = auditAdmin(ctx, tx, claim.TenantID, claim.SubmittedBy, "ingestion."+state, "ingest_job",
		strconv.FormatInt(claim.JobID, 10), "", claim.PackageSHA, claim.JobEpoch,
		"worker package processing state changed", state, map[string]any{"error_code": errorCode}); err != nil {
		return "deferred", err
	}
	if err = tx.Commit(ctx); err != nil {
		return "deferred", err
	}
	return state, nil
}

func (g *gateway) adminCancelIngestJob(w http.ResponseWriter, r *http.Request) {
	scope, ok := g.requireAdmin(w, r, adminRoleMerchant)
	if !ok {
		return
	}
	var input adminJobConfirmation
	if err := decodeAdminJobConfirmation(w, r, &input); err != nil {
		writeError(w, 400, "invalid_confirmation", "job cancellation confirmation is invalid")
		return
	}
	jobID, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil || !validJobConfirmation(input, g.session, jobID) {
		writeError(w, 409, "confirmation_conflict", "job scope or version changed; refresh before confirming")
		return
	}
	tx, err := g.db.BeginTx(r.Context(), pgx.TxOptions{})
	if err != nil {
		writeError(w, 503, "database_unavailable", "could not cancel ingestion job")
		return
	}
	defer tx.Rollback(r.Context())
	var statusText, shopID string
	var fence int64
	err = tx.QueryRow(r.Context(), `SELECT j.status,s.shop_id,j.fencing_epoch FROM ingest_job j
		JOIN source s ON s.tenant_id=j.tenant_id AND s.id=j.source_id
		WHERE j.tenant_id=$1 AND j.id=$2 AND s.type='admin_manifest' AND s.shop_id=ANY($3::text[])
		FOR UPDATE OF j`, g.session.tenantID, jobID, scope.shops).Scan(&statusText, &shopID, &fence)
	if errors.Is(err, pgx.ErrNoRows) {
		writeError(w, 404, "not_found", "ingestion job not found")
		return
	}
	if err != nil {
		writeError(w, 503, "database_unavailable", "could not read ingestion job")
		return
	}
	if shopID != input.ShopID || fence != input.ExpectedFencingEpoch || shopID == "" {
		writeError(w, 409, "confirmation_conflict", "job scope or fencing epoch changed")
		return
	}
	var result string
	switch statusText {
	case "pending":
		result = "cancelled"
		_, err = tx.Exec(r.Context(), `UPDATE ingest_job SET status='cancelled',stage='cancelled',cancel_requested=true,
			owner_token=NULL,lease_expires_at=NULL,error_code='cancelled',error='cancelled before worker claim',updated_at=now()
			WHERE tenant_id=$1 AND id=$2`, g.session.tenantID, jobID)
		if err == nil {
			_, err = tx.Exec(r.Context(), `UPDATE ingest_job_item SET status='cancelled',stage='cancelled',cancel_requested=true,
				lease_expires_at=NULL,error_code='cancelled',error='cancelled before worker claim'
				WHERE tenant_id=$1 AND job_id=$2 AND status='pending'`, g.session.tenantID, jobID)
		}
		if err == nil {
			_, err = tx.Exec(r.Context(), `UPDATE document_version v SET status='failed',quality_json=quality_json ||
				'{"parse_status":"cancelled","quality_gate":"failed"}'::jsonb FROM ingest_job_item i
				WHERE i.tenant_id=$1 AND i.job_id=$2 AND i.document_version_id=v.id
				AND v.tenant_id=i.tenant_id AND v.status='building'`, g.session.tenantID, jobID)
		}
		if err == nil {
			_, err = tx.Exec(r.Context(), `DELETE FROM ingest_package_file WHERE tenant_id=$1 AND job_id=$2`, g.session.tenantID, jobID)
		}
		if err == nil {
			_, err = tx.Exec(r.Context(), `DELETE FROM ingest_package_upload WHERE tenant_id=$1 AND job_id=$2`, g.session.tenantID, jobID)
		}
	case "running":
		result = "cancel_requested"
		_, err = tx.Exec(r.Context(), `UPDATE ingest_job SET cancel_requested=true,error_code='cancel_requested',
			updated_at=now() WHERE tenant_id=$1 AND id=$2 AND status='running'`, g.session.tenantID, jobID)
		if err == nil {
			_, err = tx.Exec(r.Context(), `UPDATE ingest_job_item SET cancel_requested=true
				WHERE tenant_id=$1 AND job_id=$2 AND status='running'`, g.session.tenantID, jobID)
		}
	default:
		writeError(w, 409, "job_not_cancellable", "only pending or running ingestion jobs can be cancelled")
		return
	}
	if err != nil {
		writeError(w, 503, "database_unavailable", "job cancellation was not committed")
		return
	}
	if err = auditAdmin(r.Context(), tx, g.session.tenantID, g.session.userID, "ingestion.cancel", "ingest_job",
		strconv.FormatInt(jobID, 10), "", "", fence, strings.TrimSpace(input.Reason), "success",
		map[string]any{"shop_id": shopID, "status": result}); err != nil {
		writeError(w, 503, "audit_unavailable", "job cancellation audit could not be recorded")
		return
	}
	if err = tx.Commit(r.Context()); err != nil {
		writeError(w, 503, "database_unavailable", "job cancellation was not committed")
		return
	}
	writeJSON(w, 200, map[string]any{"job_id": strconv.FormatInt(jobID, 10), "status": result, "audit_result": "success"})
}

func (g *gateway) adminRetryIngestJob(w http.ResponseWriter, r *http.Request) {
	scope, ok := g.requireAdmin(w, r, adminRoleMerchant)
	if !ok {
		return
	}
	var input adminJobConfirmation
	if err := decodeAdminJobConfirmation(w, r, &input); err != nil {
		writeError(w, 400, "invalid_confirmation", "job retry confirmation is invalid")
		return
	}
	jobID, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil || !validJobConfirmation(input, g.session, jobID) {
		writeError(w, 409, "confirmation_conflict", "job scope or version changed; refresh before confirming")
		return
	}
	tx, err := g.db.BeginTx(r.Context(), pgx.TxOptions{})
	if err != nil {
		writeError(w, 503, "database_unavailable", "could not retry ingestion job")
		return
	}
	defer tx.Rollback(r.Context())
	var statusText, shopID string
	var fence int64
	err = tx.QueryRow(r.Context(), `SELECT j.status,s.shop_id,j.fencing_epoch FROM ingest_job j
		JOIN source s ON s.tenant_id=j.tenant_id AND s.id=j.source_id
		WHERE j.tenant_id=$1 AND j.id=$2 AND s.type='admin_manifest' AND s.shop_id=ANY($3::text[])
		FOR UPDATE OF j`, g.session.tenantID, jobID, scope.shops).Scan(&statusText, &shopID, &fence)
	if errors.Is(err, pgx.ErrNoRows) {
		writeError(w, 404, "not_found", "ingestion job not found")
		return
	}
	if err != nil {
		writeError(w, 503, "database_unavailable", "could not read ingestion job")
		return
	}
	if shopID != input.ShopID || fence != input.ExpectedFencingEpoch || statusText != "failed" {
		writeError(w, 409, "job_not_retryable", "job status, scope or fencing epoch changed")
		return
	}
	var stagedFiles int
	if err = tx.QueryRow(r.Context(), `SELECT count(*) FROM ingest_package_file WHERE tenant_id=$1 AND job_id=$2`,
		g.session.tenantID, jobID).Scan(&stagedFiles); err != nil || stagedFiles == 0 {
		writeError(w, 409, "retry_payload_missing", "failed job no longer has a retryable package")
		return
	}
	if _, err = tx.Exec(r.Context(), `UPDATE ingest_job SET status='pending',stage='queued',retry_count=0,next_attempt_at=now(),
		cancel_requested=false,owner_token=NULL,lease_expires_at=NULL,error=NULL,error_code=NULL,updated_at=now()
		WHERE tenant_id=$1 AND id=$2 AND status='failed'`, g.session.tenantID, jobID); err != nil {
		writeError(w, 503, "database_unavailable", "job retry was not committed")
		return
	}
	if _, err = tx.Exec(r.Context(), `UPDATE ingest_job_item SET status='pending',stage='queued',retry_count=0,
		next_attempt_at=now(),cancel_requested=false,owner_token=NULL,lease_expires_at=NULL,error=NULL,error_code=NULL
		WHERE tenant_id=$1 AND job_id=$2 AND status='failed'`, g.session.tenantID, jobID); err != nil {
		writeError(w, 503, "database_unavailable", "job retry was not committed")
		return
	}
	if _, err = tx.Exec(r.Context(), `UPDATE document_version v SET status='building',quality_json=quality_json ||
		'{"parse_status":"pending","quality_gate":"pending"}'::jsonb FROM ingest_job_item i
		WHERE i.tenant_id=$1 AND i.job_id=$2 AND i.document_version_id=v.id
		AND v.tenant_id=i.tenant_id AND v.status='failed'`, g.session.tenantID, jobID); err != nil {
		writeError(w, 503, "database_unavailable", "document version retry was not committed")
		return
	}
	if err = auditAdmin(r.Context(), tx, g.session.tenantID, g.session.userID, "ingestion.retry", "ingest_job",
		strconv.FormatInt(jobID, 10), "", "", fence, strings.TrimSpace(input.Reason), "success",
		map[string]any{"shop_id": shopID}); err != nil {
		writeError(w, 503, "audit_unavailable", "job retry audit could not be recorded")
		return
	}
	if err = tx.Commit(r.Context()); err != nil {
		writeError(w, 503, "database_unavailable", "job retry was not committed")
		return
	}
	writeJSON(w, 202, map[string]any{"job_id": strconv.FormatInt(jobID, 10), "status": "pending", "audit_result": "success"})
}

func decodeAdminJobConfirmation(w http.ResponseWriter, r *http.Request, input *adminJobConfirmation) error {
	r.Body = http.MaxBytesReader(w, r.Body, 16*1024)
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(input); err != nil {
		return err
	}
	if input.JobID == "" || !input.Confirm || len(strings.TrimSpace(input.Reason)) < 10 || len(input.Reason) > 2000 {
		return errors.New("job confirmation fields are incomplete")
	}
	return nil
}

func validJobConfirmation(input adminJobConfirmation, session mockSession, jobID int64) bool {
	return input.Confirm && input.JobID == strconv.FormatInt(jobID, 10) &&
		input.TenantID == session.tenant && input.PermissionRevision == session.permissionRevision &&
		len(strings.TrimSpace(input.Reason)) >= 10 && len(input.Reason) <= 2000
}
