BEGIN;
SELECT pg_advisory_xact_lock(742018);

CREATE TABLE admin_role_grant (
    id BIGSERIAL PRIMARY KEY,
    tenant_id BIGINT NOT NULL REFERENCES tenant(id),
    user_id BIGINT NOT NULL REFERENCES app_user(id),
    role TEXT NOT NULL CHECK (role IN ('platform_observer','access_admin','merchant_admin','knowledge_reviewer')),
    scope_kind TEXT NOT NULL CHECK (scope_kind IN ('tenant','shops')),
    shop_ids TEXT[] NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked')),
    granted_by BIGINT NOT NULL REFERENCES app_user(id),
    granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_by BIGINT REFERENCES app_user(id),
    revoked_at TIMESTAMPTZ,
    CHECK ((scope_kind='tenant' AND cardinality(shop_ids)=0) OR
           (scope_kind='shops' AND cardinality(shop_ids)>0)),
    CHECK ((status='active' AND revoked_at IS NULL AND revoked_by IS NULL) OR
           (status='revoked' AND revoked_at IS NOT NULL AND revoked_by IS NOT NULL)),
    UNIQUE (tenant_id,user_id,role)
);

CREATE TABLE admin_permission_request (
    id BIGSERIAL PRIMARY KEY,
    tenant_id BIGINT NOT NULL REFERENCES tenant(id),
    requested_by BIGINT NOT NULL REFERENCES app_user(id),
    role TEXT NOT NULL CHECK (role IN ('platform_observer','merchant_admin','knowledge_reviewer')),
    scope_kind TEXT NOT NULL CHECK (scope_kind IN ('tenant','shops')),
    shop_ids TEXT[] NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL CHECK (length(btrim(reason)) BETWEEN 10 AND 2000),
    payload_hash TEXT NOT NULL CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','needs_revision')),
    reviewed_by BIGINT REFERENCES app_user(id),
    reviewed_at TIMESTAMPTZ,
    decision_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((scope_kind='tenant' AND cardinality(shop_ids)=0) OR
           (scope_kind='shops' AND cardinality(shop_ids)>0)),
    CHECK ((status='pending' AND reviewed_by IS NULL AND reviewed_at IS NULL) OR
           (status<>'pending' AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL))
);

CREATE TABLE admin_audit (
    id BIGSERIAL PRIMARY KEY,
    tenant_id BIGINT NOT NULL REFERENCES tenant(id),
    actor_user_id BIGINT NOT NULL REFERENCES app_user(id),
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    resource_version TEXT,
    content_hash TEXT,
    fencing_epoch BIGINT,
    reason TEXT,
    result TEXT NOT NULL CHECK (result IN ('success','denied','conflict','failed')),
    metadata_json JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE source_manifest (
    id BIGSERIAL PRIMARY KEY,
    tenant_id BIGINT NOT NULL REFERENCES tenant(id),
    source_id BIGINT NOT NULL REFERENCES source(id),
    manifest_sha256 TEXT NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    dataset_sha256 TEXT NOT NULL CHECK (dataset_sha256 ~ '^[0-9a-f]{64}$'),
    pipeline_version TEXT NOT NULL,
    manifest_yaml TEXT NOT NULL,
    uploaded_by BIGINT NOT NULL REFERENCES app_user(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id,source_id,manifest_sha256),
    UNIQUE (tenant_id,id)
);

CREATE TABLE source_file (
    id BIGSERIAL PRIMARY KEY,
    tenant_id BIGINT NOT NULL REFERENCES tenant(id),
    source_manifest_id BIGINT NOT NULL,
    document_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    media_type TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes BIGINT NOT NULL CHECK (size_bytes BETWEEN 0 AND 8388608),
    content BYTEA NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id,source_manifest_id,relative_path),
    FOREIGN KEY (tenant_id,source_manifest_id) REFERENCES source_manifest(tenant_id,id)
);

ALTER TABLE document_version
    ADD COLUMN disclosure_class TEXT NOT NULL DEFAULT 'unclassified'
        CHECK (disclosure_class IN ('external_allowed','internal_only','unclassified')),
    ADD COLUMN external_allowed BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN review_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (review_status IN ('pending','approved','rejected')),
    ADD COLUMN reviewed_hash TEXT,
    ADD COLUMN reviewed_by BIGINT REFERENCES app_user(id),
    ADD COLUMN reviewed_at TIMESTAMPTZ,
    ADD COLUMN review_reason TEXT,
    ADD COLUMN quality_json JSONB NOT NULL DEFAULT '{}',
    ADD COLUMN fencing_epoch BIGINT NOT NULL DEFAULT 0 CHECK (fencing_epoch >= 0),
    ADD COLUMN source_manifest_id BIGINT REFERENCES source_manifest(id),
    ADD CONSTRAINT document_version_disclosure_allowed_check
        CHECK (NOT external_allowed OR disclosure_class='external_allowed');
ALTER TABLE document_version ADD CONSTRAINT document_version_review_hash_check
    CHECK (reviewed_hash IS NULL OR reviewed_hash ~ '^[0-9a-f]{64}$');
ALTER TABLE document_version ADD CONSTRAINT document_version_review_actor_check
    CHECK ((review_status='pending' AND reviewed_hash IS NULL AND reviewed_by IS NULL AND reviewed_at IS NULL)
        OR (review_status<>'pending' AND reviewed_hash IS NOT NULL AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL));
ALTER TABLE ingest_job
    ADD COLUMN manifest_sha256 TEXT,
    ADD COLUMN dataset_sha256 TEXT,
    ADD COLUMN submitted_by BIGINT REFERENCES app_user(id),
    ADD COLUMN fencing_epoch BIGINT NOT NULL DEFAULT 0 CHECK (fencing_epoch >= 0);
ALTER TABLE ingest_job DROP CONSTRAINT ingest_job_status_check;
ALTER TABLE ingest_job ADD CONSTRAINT ingest_job_status_check
    CHECK (status IN ('pending','running','awaiting_review','done','failed','cancelled'));

ALTER TABLE ingest_job_item
    ADD COLUMN document_version_id BIGINT,
    ADD COLUMN stage TEXT NOT NULL DEFAULT 'pending',
    ADD COLUMN retry_count INT NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    ADD COLUMN error_code TEXT;
ALTER TABLE ingest_job_item ADD CONSTRAINT ingest_item_version_tenant_fk
    FOREIGN KEY (tenant_id,document_version_id) REFERENCES document_version(tenant_id,id);

CREATE INDEX idx_admin_grant_active ON admin_role_grant(tenant_id,user_id,role)
    WHERE status='active';
CREATE INDEX idx_admin_request_queue ON admin_permission_request(tenant_id,status,created_at DESC);
CREATE INDEX idx_admin_audit_timeline ON admin_audit(tenant_id,created_at DESC,id DESC);
CREATE INDEX idx_source_manifest_latest ON source_manifest(tenant_id,source_id,created_at DESC);
CREATE INDEX idx_document_version_review_queue ON document_version(tenant_id,review_status,created_at DESC);
CREATE INDEX idx_source_file_doc ON source_file(tenant_id,document_id,created_at DESC);
CREATE UNIQUE INDEX uq_source_scope_uri ON source(tenant_id,shop_id,type,uri) WHERE uri IS NOT NULL;

CREATE FUNCTION ecr_validate_admin_scope() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    target_tenant BIGINT;
BEGIN
    SELECT tenant_id INTO target_tenant FROM app_user WHERE id=NEW.user_id;
    IF target_tenant IS DISTINCT FROM NEW.tenant_id THEN
        RAISE EXCEPTION 'admin grant user is outside tenant' USING ERRCODE='23503';
    END IF;
    IF NEW.scope_kind='shops' AND EXISTS (
        SELECT 1 FROM unnest(NEW.shop_ids) requested(shop_id)
        WHERE NOT EXISTS (SELECT 1 FROM shop s WHERE s.tenant_id=NEW.tenant_id
            AND s.id=requested.shop_id AND s.status='active')
    ) THEN
        RAISE EXCEPTION 'admin grant contains a shop outside tenant' USING ERRCODE='23503';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_admin_grant_scope BEFORE INSERT OR UPDATE ON admin_role_grant
    FOR EACH ROW EXECUTE FUNCTION ecr_validate_admin_scope();

CREATE FUNCTION ecr_validate_admin_request() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    requester_tenant BIGINT;
BEGIN
    SELECT tenant_id INTO requester_tenant FROM app_user WHERE id=NEW.requested_by;
    IF requester_tenant IS DISTINCT FROM NEW.tenant_id THEN
        RAISE EXCEPTION 'requester is outside tenant' USING ERRCODE='23503';
    END IF;
    IF NEW.scope_kind='shops' AND EXISTS (
        SELECT 1 FROM unnest(NEW.shop_ids) requested(shop_id)
        WHERE NOT EXISTS (SELECT 1 FROM shop s WHERE s.tenant_id=NEW.tenant_id
            AND s.id=requested.shop_id AND s.status='active')
    ) THEN
        RAISE EXCEPTION 'request contains a shop outside tenant' USING ERRCODE='23503';
    END IF;
    IF TG_OP='UPDATE' AND (NEW.tenant_id,NEW.requested_by,NEW.role,NEW.scope_kind,NEW.shop_ids,
        NEW.reason,NEW.payload_hash,NEW.created_at) IS DISTINCT FROM (OLD.tenant_id,OLD.requested_by,
        OLD.role,OLD.scope_kind,OLD.shop_ids,OLD.reason,OLD.payload_hash,OLD.created_at) THEN
        RAISE EXCEPTION 'permission request payload is immutable' USING ERRCODE='55000';
    END IF;
    IF TG_OP='UPDATE' AND OLD.status<>'pending' THEN
        RAISE EXCEPTION 'permission request decision is immutable' USING ERRCODE='55000';
    END IF;
    IF NEW.status<>'pending' THEN
        IF NEW.reviewed_by=NEW.requested_by THEN
            RAISE EXCEPTION 'requester cannot approve their own request' USING ERRCODE='42501';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM admin_role_grant actor
            JOIN app_user u ON u.tenant_id=actor.tenant_id AND u.id=actor.user_id
            JOIN tenant t ON t.id=u.tenant_id
            WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.reviewed_by
              AND actor.role IN ('access_admin','merchant_admin') AND actor.status='active'
              AND u.revoked_at IS NULL AND t.status='active'
              AND (actor.scope_kind='tenant' OR (NEW.scope_kind='shops' AND actor.shop_ids @> NEW.shop_ids))) THEN
            RAISE EXCEPTION 'reviewer lacks active request scope' USING ERRCODE='42501';
        END IF;
        IF NEW.status='approved' AND NOT EXISTS (SELECT 1 FROM admin_role_grant target
            WHERE target.tenant_id=NEW.tenant_id AND target.user_id=NEW.requested_by
              AND target.role=NEW.role AND target.scope_kind=NEW.scope_kind
              AND target.shop_ids=NEW.shop_ids AND target.status='active'
              AND target.granted_by=NEW.reviewed_by) THEN
            RAISE EXCEPTION 'approved request has no matching active grant' USING ERRCODE='42501';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_admin_permission_request BEFORE INSERT OR UPDATE ON admin_permission_request
    FOR EACH ROW EXECUTE FUNCTION ecr_validate_admin_request();

CREATE FUNCTION ecr_immutable_admin_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'admin audit records are append-only' USING ERRCODE='55000';
END $$;
CREATE TRIGGER trg_admin_audit_immutable BEFORE UPDATE OR DELETE ON admin_audit
    FOR EACH ROW EXECUTE FUNCTION ecr_immutable_admin_audit();

CREATE FUNCTION ecr_guard_document_version_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.fencing_epoch < OLD.fencing_epoch OR NEW.fencing_epoch > OLD.fencing_epoch + 1 THEN
        RAISE EXCEPTION 'document version fencing epoch is invalid' USING ERRCODE='40001';
    END IF;
    IF NEW.status='active' AND (NEW.review_status<>'approved'
        OR NEW.reviewed_hash IS DISTINCT FROM NEW.source_hash
        OR NEW.external_allowed IS DISTINCT FROM (NEW.disclosure_class='external_allowed')) THEN
        RAISE EXCEPTION 'unreviewed or changed version cannot be activated' USING ERRCODE='42501';
    END IF;
    IF OLD.status='revoked' AND NEW.status<>'revoked' THEN
        RAISE EXCEPTION 'revoked versions cannot be reactivated' USING ERRCODE='55000';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_document_version_transition BEFORE UPDATE ON document_version
    FOR EACH ROW EXECUTE FUNCTION ecr_guard_document_version_transition();

CREATE FUNCTION ecr_issue_admin_grant(
    p_tenant_id BIGINT,p_user_id BIGINT,p_role TEXT,p_scope_kind TEXT,p_shop_ids TEXT[],
    p_actor_id BIGINT,p_request_id BIGINT,p_reason TEXT
) RETURNS BIGINT LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE
    new_id BIGINT;
BEGIN
    IF length(btrim(p_reason)) NOT BETWEEN 10 AND 2000 THEN
        RAISE EXCEPTION 'decision reason is required' USING ERRCODE='22023';
    END IF;
    IF p_role NOT IN ('platform_observer','merchant_admin','knowledge_reviewer') THEN
        RAISE EXCEPTION 'role requires out-of-band privileged grant' USING ERRCODE='42501';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM admin_role_grant WHERE tenant_id=p_tenant_id
        AND user_id=p_actor_id AND role IN ('access_admin','merchant_admin') AND status='active'
        AND (scope_kind='tenant' OR (p_scope_kind='shops' AND shop_ids @> p_shop_ids)) FOR SHARE)
       OR NOT EXISTS (SELECT 1 FROM app_user u JOIN tenant t ON t.id=u.tenant_id
        WHERE u.tenant_id=p_tenant_id AND u.id=p_actor_id AND u.revoked_at IS NULL AND t.status='active') THEN
        RAISE EXCEPTION 'actor lacks permission grant scope' USING ERRCODE='42501';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM admin_permission_request WHERE tenant_id=p_tenant_id
        AND id=p_request_id AND requested_by=p_user_id AND role=p_role AND scope_kind=p_scope_kind
        AND shop_ids=p_shop_ids AND status='pending') THEN
        RAISE EXCEPTION 'permission request payload or state changed' USING ERRCODE='40001';
    END IF;
    INSERT INTO admin_role_grant(tenant_id,user_id,role,scope_kind,shop_ids,granted_by)
      VALUES(p_tenant_id,p_user_id,p_role,p_scope_kind,p_shop_ids,p_actor_id)
      ON CONFLICT(tenant_id,user_id,role) DO UPDATE SET scope_kind=EXCLUDED.scope_kind,
        shop_ids=EXCLUDED.shop_ids,status='active',granted_by=EXCLUDED.granted_by,
        granted_at=now(),revoked_by=NULL,revoked_at=NULL
      RETURNING id INTO new_id;
    UPDATE admin_permission_request SET status='approved',reviewed_by=p_actor_id,
        reviewed_at=now(),decision_reason=p_reason WHERE tenant_id=p_tenant_id AND id=p_request_id
        AND requested_by=p_user_id AND role=p_role AND status='pending';
    IF NOT FOUND THEN RAISE EXCEPTION 'permission request is no longer pending' USING ERRCODE='40001'; END IF;
    INSERT INTO admin_audit(tenant_id,actor_user_id,action,resource_type,resource_id,reason,result,metadata_json)
      VALUES(p_tenant_id,p_actor_id,'permission.approve','admin_permission_request',p_request_id::text,
        p_reason,'success',jsonb_build_object('role',p_role,'scope_kind',p_scope_kind,
        'shop_count',cardinality(p_shop_ids),'shop_ids',p_shop_ids));
    RETURN new_id;
END $$;

CREATE FUNCTION ecr_revoke_admin_grant(p_tenant_id BIGINT,p_grant_id BIGINT,p_actor_id BIGINT,p_reason TEXT)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE
    target admin_role_grant%ROWTYPE;
BEGIN
    IF length(btrim(p_reason)) NOT BETWEEN 10 AND 2000 THEN
        RAISE EXCEPTION 'revocation reason is required' USING ERRCODE='22023';
    END IF;
    SELECT * INTO target FROM admin_role_grant WHERE tenant_id=p_tenant_id AND id=p_grant_id FOR UPDATE;
    IF NOT FOUND OR target.status<>'active' THEN
        RAISE EXCEPTION 'active grant not found' USING ERRCODE='40001';
    END IF;
    IF target.user_id=p_actor_id OR NOT EXISTS (
        SELECT 1 FROM admin_role_grant actor WHERE actor.tenant_id=p_tenant_id AND actor.user_id=p_actor_id
          AND actor.role='access_admin' AND actor.status='active'
          AND (actor.scope_kind='tenant' OR (target.scope_kind='shops' AND actor.shop_ids @> target.shop_ids))
    ) OR NOT EXISTS (SELECT 1 FROM app_user u JOIN tenant t ON t.id=u.tenant_id
        WHERE u.tenant_id=p_tenant_id AND u.id=p_actor_id AND u.revoked_at IS NULL AND t.status='active') THEN
        RAISE EXCEPTION 'actor cannot revoke this grant' USING ERRCODE='42501';
    END IF;
    UPDATE admin_role_grant SET status='revoked',revoked_by=p_actor_id,revoked_at=now()
      WHERE tenant_id=p_tenant_id AND id=p_grant_id;
    INSERT INTO admin_audit(tenant_id,actor_user_id,action,resource_type,resource_id,reason,result,metadata_json)
      VALUES(p_tenant_id,p_actor_id,'permission.revoke','admin_role_grant',p_grant_id::text,
        p_reason,'success',jsonb_build_object('target_user_id',target.user_id,'role',target.role,
        'shop_ids',target.shop_ids));
END $$;

CREATE FUNCTION ecr_decide_admin_request(
    p_tenant_id BIGINT,p_request_id BIGINT,p_actor_id BIGINT,p_decision TEXT,p_reason TEXT
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE
    request_row admin_permission_request%ROWTYPE;
BEGIN
    IF p_decision NOT IN ('rejected','needs_revision') OR length(btrim(p_reason)) NOT BETWEEN 10 AND 2000 THEN
        RAISE EXCEPTION 'decision and reason are required' USING ERRCODE='22023';
    END IF;
    SELECT * INTO request_row FROM admin_permission_request
      WHERE tenant_id=p_tenant_id AND id=p_request_id AND status='pending' FOR UPDATE;
    IF NOT FOUND OR request_row.requested_by=p_actor_id THEN
        RAISE EXCEPTION 'request is unavailable or actor cannot review it' USING ERRCODE='40001';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM admin_role_grant actor
        JOIN app_user u ON u.tenant_id=actor.tenant_id AND u.id=actor.user_id
        JOIN tenant t ON t.id=u.tenant_id
        WHERE actor.tenant_id=p_tenant_id AND actor.user_id=p_actor_id
          AND actor.role IN ('access_admin','merchant_admin') AND actor.status='active'
          AND u.revoked_at IS NULL AND t.status='active'
          AND (actor.scope_kind='tenant' OR (request_row.scope_kind='shops' AND actor.shop_ids @> request_row.shop_ids))) THEN
        RAISE EXCEPTION 'actor lacks permission decision scope' USING ERRCODE='42501';
    END IF;
    UPDATE admin_permission_request SET status=p_decision,reviewed_by=p_actor_id,
        reviewed_at=now(),decision_reason=p_reason WHERE tenant_id=p_tenant_id AND id=p_request_id;
    INSERT INTO admin_audit(tenant_id,actor_user_id,action,resource_type,resource_id,reason,result,metadata_json)
      VALUES(p_tenant_id,p_actor_id,'permission.'||p_decision,'admin_permission_request',p_request_id::text,
        p_reason,'success',jsonb_build_object('role',request_row.role,'scope_kind',request_row.scope_kind,
        'shop_ids',request_row.shop_ids));
END $$;

REVOKE ALL ON FUNCTION ecr_issue_admin_grant(BIGINT,BIGINT,TEXT,TEXT,TEXT[],BIGINT,BIGINT,TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION ecr_revoke_admin_grant(BIGINT,BIGINT,BIGINT,TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION ecr_decide_admin_request(BIGINT,BIGINT,BIGINT,TEXT,TEXT) FROM PUBLIC;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='rag_app') THEN
        GRANT SELECT ON admin_role_grant,admin_permission_request,admin_audit,source_manifest,source_file TO rag_app;
        GRANT INSERT ON admin_permission_request TO rag_app;
        GRANT INSERT ON admin_audit TO rag_app;
        GRANT SELECT,INSERT ON source_manifest TO rag_app;
        GRANT INSERT ON source_file TO rag_app;
        GRANT USAGE,SELECT ON SEQUENCE admin_role_grant_id_seq,admin_permission_request_id_seq,
            admin_audit_id_seq,source_manifest_id_seq,source_file_id_seq TO rag_app;
        GRANT SELECT,UPDATE ON document_version TO rag_app;
        GRANT UPDATE ON document TO rag_app;
        GRANT SELECT,INSERT,UPDATE ON source,ingest_job,ingest_job_item,chunk TO rag_app;
        GRANT USAGE,SELECT ON SEQUENCE source_id_seq,ingest_job_id_seq,ingest_job_item_id_seq,chunk_id_seq TO rag_app;
        REVOKE INSERT,UPDATE,DELETE,TRUNCATE ON admin_role_grant FROM rag_app;
        REVOKE UPDATE,DELETE,TRUNCATE ON admin_audit FROM rag_app;
        REVOKE UPDATE,DELETE,TRUNCATE ON source_manifest FROM rag_app;
        REVOKE UPDATE,DELETE,TRUNCATE ON source_file FROM rag_app;
        GRANT EXECUTE ON FUNCTION ecr_issue_admin_grant(BIGINT,BIGINT,TEXT,TEXT,TEXT[],BIGINT,BIGINT,TEXT) TO rag_app;
        GRANT EXECUTE ON FUNCTION ecr_revoke_admin_grant(BIGINT,BIGINT,BIGINT,TEXT) TO rag_app;
        GRANT EXECUTE ON FUNCTION ecr_decide_admin_request(BIGINT,BIGINT,BIGINT,TEXT,TEXT) TO rag_app;
    END IF;
END $$;

INSERT INTO schema_migration(version,checksum_sha256)
VALUES ('0010', :'migration_checksum') ON CONFLICT(version) DO NOTHING;
COMMIT;
