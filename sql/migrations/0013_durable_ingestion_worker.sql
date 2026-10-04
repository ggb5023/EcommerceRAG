BEGIN;
SELECT pg_advisory_xact_lock(742018);

ALTER TABLE ingest_job
    ADD COLUMN owner_token TEXT,
    ADD COLUMN next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN max_attempts SMALLINT NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 10),
    ADD COLUMN error_code TEXT;

ALTER TABLE ingest_job_item
    ADD COLUMN next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD CONSTRAINT ingest_job_item_status_check
        CHECK (status IN ('pending','running','awaiting_review','done','failed','cancelled'));

CREATE TABLE ingest_package_upload (
    tenant_id BIGINT NOT NULL,
    job_id BIGINT NOT NULL,
    manifest_yaml TEXT NOT NULL,
    package_sha256 TEXT NOT NULL CHECK (package_sha256 ~ '^[0-9a-f]{64}$'),
    uploaded_by BIGINT NOT NULL REFERENCES app_user(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,job_id),
    FOREIGN KEY (tenant_id,job_id) REFERENCES ingest_job(tenant_id,id)
);

CREATE TABLE ingest_package_file (
    tenant_id BIGINT NOT NULL,
    job_id BIGINT NOT NULL,
    item_id BIGINT NOT NULL,
    relative_path TEXT NOT NULL,
    media_type TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes BIGINT NOT NULL CHECK (size_bytes BETWEEN 0 AND 8388608),
    content BYTEA NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,job_id,relative_path),
    UNIQUE (tenant_id,item_id),
    FOREIGN KEY (tenant_id,job_id) REFERENCES ingest_job(tenant_id,id),
    FOREIGN KEY (tenant_id,item_id) REFERENCES ingest_job_item(tenant_id,id)
);

CREATE TABLE ingest_worker_heartbeat (
    worker_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('idle','working','degraded','stopping')),
    started_at TIMESTAMPTZ NOT NULL,
    heartbeat_at TIMESTAMPTZ NOT NULL,
    active_tenant_id BIGINT,
    active_job_id BIGINT,
    completed_jobs BIGINT NOT NULL DEFAULT 0 CHECK (completed_jobs >= 0),
    failed_jobs BIGINT NOT NULL DEFAULT 0 CHECK (failed_jobs >= 0),
    last_error_code TEXT,
    CHECK ((active_tenant_id IS NULL) = (active_job_id IS NULL))
);

CREATE INDEX idx_ingest_job_claim ON ingest_job(status,next_attempt_at,lease_expires_at,created_at)
    WHERE status IN ('pending','running');
CREATE INDEX idx_ingest_item_next_attempt ON ingest_job_item(tenant_id,status,next_attempt_at,lease_expires_at);
CREATE INDEX idx_ingest_worker_heartbeat ON ingest_worker_heartbeat(heartbeat_at DESC);

CREATE FUNCTION guard_ingest_job_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.fencing_epoch < OLD.fencing_epoch OR NEW.fencing_epoch > OLD.fencing_epoch + 1 THEN
        RAISE EXCEPTION 'ingest job fencing epoch is invalid' USING ERRCODE='40001';
    END IF;
    IF NEW.owner_token IS DISTINCT FROM OLD.owner_token
       AND OLD.status='running' AND OLD.lease_expires_at > now()
       AND NOT OLD.cancel_requested AND NEW.status='running' THEN
        RAISE EXCEPTION 'active ingest job lease cannot be taken over' USING ERRCODE='55000';
    END IF;
    IF NEW.owner_token IS DISTINCT FROM OLD.owner_token
       AND NEW.owner_token IS NOT NULL AND NEW.fencing_epoch <= OLD.fencing_epoch THEN
        RAISE EXCEPTION 'new ingest job owner must advance the fencing epoch' USING ERRCODE='55000';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_ingest_job_fencing BEFORE UPDATE ON ingest_job
    FOR EACH ROW EXECUTE FUNCTION guard_ingest_job_update();

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='rag_app') THEN
        GRANT SELECT,INSERT,UPDATE ON ingest_worker_heartbeat TO rag_app;
        GRANT SELECT,INSERT,UPDATE,DELETE ON ingest_package_upload,ingest_package_file TO rag_app;
    END IF;
END $$;

INSERT INTO schema_migration(version,checksum_sha256)
VALUES ('0013', :'migration_checksum') ON CONFLICT(version) DO NOTHING;
COMMIT;
