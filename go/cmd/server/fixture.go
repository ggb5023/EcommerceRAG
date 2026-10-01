package main

import (
	"context"
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"

	"github.com/jackc/pgx/v5"
)

//go:embed fixtures/m1_shipping.md
var fixtureFiles embed.FS

type markdownFixture struct {
	raw             []byte
	title           string
	documentID      string
	shopID          string
	disclosureClass string
	content         string
	hash            string
	versionID       string
}

func loadM1Fixture() (markdownFixture, error) {
	raw, err := fixtureFiles.ReadFile("fixtures/m1_shipping.md")
	if err != nil {
		return markdownFixture{}, err
	}
	parts := strings.SplitN(string(raw), "---", 3)
	if len(parts) != 3 || strings.TrimSpace(parts[0]) != "" {
		return markdownFixture{}, errors.New("invalid M1 fixture front matter")
	}
	metadata := map[string]string{}
	for _, line := range strings.Split(parts[1], "\n") {
		key, value, found := strings.Cut(line, ":")
		if found {
			metadata[strings.TrimSpace(key)] = strings.TrimSpace(value)
		}
	}
	fixture := markdownFixture{
		raw: raw, title: metadata["title"], documentID: metadata["document_id"],
		shopID: metadata["shop_id"], disclosureClass: metadata["disclosure_class"],
		content: strings.TrimSpace(parts[2]),
	}
	if fixture.title == "" || fixture.documentID == "" || fixture.content == "" {
		return markdownFixture{}, errors.New("M1 fixture is missing required metadata or content")
	}
	if fixture.shopID != "shop-demo" || fixture.disclosureClass != "external_allowed" {
		return markdownFixture{}, errors.New("M1 fixture scope or disclosure classification is not allowed")
	}
	sum := sha256.Sum256(raw)
	fixture.hash = hex.EncodeToString(sum[:])
	fixture.versionID = "m1-" + fixture.hash[:16]
	return fixture, nil
}

func (g *gateway) importM1Fixture(ctx context.Context) (markdownFixture, bool, error) {
	fixture, err := loadM1Fixture()
	if err != nil {
		return markdownFixture{}, false, err
	}
	if fixture.shopID != g.session.shop {
		return markdownFixture{}, false, errors.New("fixture shop is outside the mock session")
	}
	key := "m1-fixture:" + fixture.documentID
	tx, err := g.db.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return markdownFixture{}, false, err
	}
	defer tx.Rollback(ctx)
	if _, err = tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtext($1)::bigint)", key); err != nil {
		return markdownFixture{}, false, err
	}
	var oldHash string
	err = tx.QueryRow(ctx, `SELECT payload_hash FROM ingest_payload WHERE tenant_id=$1 AND idempotency_key=$2`,
		g.session.tenantID, key).Scan(&oldHash)
	if err == nil {
		if oldHash != fixture.hash {
			return markdownFixture{}, false, fmt.Errorf("fixture idempotency conflict for %s", fixture.documentID)
		}
		if err = tx.Commit(ctx); err != nil {
			return markdownFixture{}, false, err
		}
		return fixture, false, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return markdownFixture{}, false, err
	}
	var sourceID, documentID, jobID, versionRowID, itemID int64
	if err = tx.QueryRow(ctx, `INSERT INTO source(tenant_id,shop_id,type,uri)
		VALUES($1,$2,'fixture','fixture://m1/shipping') RETURNING id`,
		g.session.tenantID, fixture.shopID).Scan(&sourceID); err != nil {
		return markdownFixture{}, false, err
	}
	if err = tx.QueryRow(ctx, `INSERT INTO document(tenant_id,source_id,logical_key,title,doc_type,meta_json)
		VALUES($1,$2,$3,$4,'markdown_fixture',$5::jsonb)
		ON CONFLICT(tenant_id,logical_key) DO UPDATE SET title=EXCLUDED.title
		RETURNING id`, g.session.tenantID, sourceID, fixture.documentID, fixture.title,
		`{"disclosure_class":"external_allowed","shop_id":"shop-demo"}`).Scan(&documentID); err != nil {
		return markdownFixture{}, false, err
	}
	if err = tx.QueryRow(ctx, `INSERT INTO ingest_job(tenant_id,source_id,idempotency_key,stage,status)
		VALUES($1,$2,$3,'markdown_fixture','running') RETURNING id`,
		g.session.tenantID, sourceID, key).Scan(&jobID); err != nil {
		return markdownFixture{}, false, err
	}
	if err = tx.QueryRow(ctx, `INSERT INTO document_version(tenant_id,document_id,source_hash,parser_version,
		chunk_rule_version,embedding_model,pipeline_version,object_key,parsed_object_key,status,chunk_count,activated_at)
		VALUES($1,$2,$3,'m1-markdown-v1','m1-paragraph-v1','m1-deterministic','m1-v1',$4,$4,'ready',1,now()) RETURNING id`,
		g.session.tenantID, documentID, fixture.hash, "fixture://m1/shipping/"+fixture.hash).Scan(&versionRowID); err != nil {
		return markdownFixture{}, false, err
	}
	if _, err = tx.Exec(ctx, `UPDATE document SET active_version_id=$3,status='active'
		WHERE tenant_id=$1 AND id=$2`, g.session.tenantID, documentID, versionRowID); err != nil {
		return markdownFixture{}, false, err
	}
	tokens := strings.Fields(fixture.content)
	if len(tokens) == 0 || len(tokens) > 1024 {
		return markdownFixture{}, false, errors.New("M1 fixture chunk exceeds the configured token bound")
	}
	metadata, _ := json.Marshal(map[string]string{
		"title": fixture.title, "source_ref": "fixture://m1/shipping",
		"disclosure_class": fixture.disclosureClass, "fixture_version": fixture.versionID,
	})
	if _, err = tx.Exec(ctx, `INSERT INTO chunk(version_id,doc_id,tenant_id,shop_id,chunk_index,section_seq,
		section_chunk_index,char_start,char_end,content,embed_text,token_count,content_type,split_reason,
		embedding_model,embedding_dim,tokenizer_id,meta_json,source_object_key,parsed_object_key,chunk_hash)
		VALUES($1,$2,$3,$4,0,1,0,0,$5,$6,$6,$7,'text','markdown_fixture','m1-deterministic',1024,
		'm1-whitespace-v1',$8::jsonb,$9,$9,$10)`, versionRowID, documentID, g.session.tenantID,
		fixture.shopID, len([]rune(fixture.content)), fixture.content, len(tokens), string(metadata),
		"fixture://m1/shipping/"+fixture.hash, fixture.hash); err != nil {
		return markdownFixture{}, false, err
	}
	owner := "m1-import-" + fixture.hash[:16]
	if err = tx.QueryRow(ctx, `INSERT INTO ingest_job_item(tenant_id,job_id,data_id,payload_hash,status,owner_token,fencing_epoch,lease_expires_at)
		VALUES($1,$2,$3,$4,'running',$5,1,now()+interval '5 minutes') RETURNING id`,
		g.session.tenantID, jobID, fixture.documentID, fixture.hash, owner).Scan(&itemID); err != nil {
		return markdownFixture{}, false, err
	}
	if _, err = tx.Exec(ctx, `INSERT INTO ingest_payload(tenant_id,job_id,idempotency_key,payload_hash)
		VALUES($1,$2,$3,$4)`, g.session.tenantID, jobID, key, fixture.hash); err != nil {
		return markdownFixture{}, false, err
	}
	if _, err = tx.Exec(ctx, `INSERT INTO ingest_publication(tenant_id,item_id,fencing_epoch,document_version_id)
		VALUES($1,$2,1,$3)`, g.session.tenantID, itemID, versionRowID); err != nil {
		return markdownFixture{}, false, err
	}
	if _, err = tx.Exec(ctx, `UPDATE ingest_job SET document_version_id=$3,status='done',updated_at=now()
		WHERE tenant_id=$1 AND id=$2`, g.session.tenantID, jobID, versionRowID); err != nil {
		return markdownFixture{}, false, err
	}
	if err = tx.Commit(ctx); err != nil {
		return markdownFixture{}, false, err
	}
	return fixture, true, nil
}

func (g *gateway) importFixtureRoute(w http.ResponseWriter, r *http.Request) {
	if !g.requireWriteRole(w) {
		return
	}
	fixture, imported, err := g.importM1Fixture(r.Context())
	if err != nil {
		writeError(w, http.StatusConflict, "fixture_import_failed", "fixture import failed")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"document_id": fixture.documentID, "version_id": fixture.versionID,
		"source_ref": "fixture://m1/shipping", "imported": imported, "is_mock": true,
	})
}

// These compiled fixture IDs are trusted registrations, never manifest grants.
func (g *gateway) registerM1Resources(ctx context.Context) error {
	_, err := g.db.Exec(ctx, `INSERT INTO document(tenant_id,source_id,logical_key,title,doc_type,meta_json)
        SELECT d.tenant_id,d.source_id,ids.logical_key,'M1 compiled fixture','markdown_fixture',
            '{"disclosure_class":"external_allowed","shop_id":"shop-demo"}'::jsonb
        FROM document d CROSS JOIN (VALUES ('m1-demo-bottle'),('m1-demo-care'),('m1-demo-storage'),('m1-demo-unanswerable')) ids(logical_key)
        WHERE d.tenant_id=$1 AND d.logical_key='m1-demo-shipping'
        ON CONFLICT(tenant_id,logical_key) DO NOTHING`, g.session.tenantID)
	return err
}
