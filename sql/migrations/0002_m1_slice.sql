-- M1 vertical slice: conversations, executions, evidence, replay and ingest items.
BEGIN;
SELECT pg_advisory_xact_lock(742018);

CREATE TABLE IF NOT EXISTS shop (
    tenant_id BIGINT NOT NULL REFERENCES tenant(id),
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    PRIMARY KEY (tenant_id, id)
);

INSERT INTO tenant (name, status)
SELECT 'M1 isolated prototype', 'active'
WHERE NOT EXISTS (SELECT 1 FROM tenant WHERE name = 'M1 isolated prototype');

INSERT INTO app_user (tenant_id, external_id, role, shop_ids)
SELECT id, 'm1-user', 'operator', ARRAY['shop-demo']
FROM tenant WHERE name = 'M1 isolated prototype'
ON CONFLICT (tenant_id, external_id) DO UPDATE
SET role = EXCLUDED.role, shop_ids = EXCLUDED.shop_ids;

INSERT INTO shop (tenant_id, id, name, status)
SELECT id, 'shop-demo', 'M1 演示店铺', 'active'
FROM tenant WHERE name = 'M1 isolated prototype'
ON CONFLICT (tenant_id, id) DO UPDATE SET name = EXCLUDED.name, status = 'active';

CREATE TABLE IF NOT EXISTS conversation_execution (
    tenant_id BIGINT NOT NULL REFERENCES tenant(id),
    id BIGSERIAL PRIMARY KEY,
    turn_id BIGINT NOT NULL,
    execution_no INT NOT NULL,
    request_id TEXT NOT NULL,
    status TEXT NOT NULL,
    finish_reason TEXT,
    answer TEXT,
    is_mock BOOLEAN NOT NULL DEFAULT false,
    answer_hash TEXT,
    deadline_at TIMESTAMPTZ,
    cancel_requested BOOLEAN NOT NULL DEFAULT false,
    error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    UNIQUE (tenant_id, request_id),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, turn_id, execution_no),
    FOREIGN KEY (tenant_id, turn_id) REFERENCES turn(tenant_id, id)
);

CREATE TABLE IF NOT EXISTS evidence_snapshot (
    tenant_id BIGINT NOT NULL REFERENCES tenant(id),
    id BIGSERIAL PRIMARY KEY,
    execution_id BIGINT NOT NULL,
    evidence_id TEXT NOT NULL,
    document_id TEXT,
    version_id TEXT,
    source_ref TEXT,
    content TEXT NOT NULL,
    disclosure_class TEXT NOT NULL,
    customer_eligible BOOLEAN NOT NULL DEFAULT false,
    citation_index INT,
    UNIQUE (tenant_id, execution_id, evidence_id),
    FOREIGN KEY (tenant_id, execution_id) REFERENCES conversation_execution(tenant_id, id)
);

CREATE TABLE IF NOT EXISTS sse_event (
    tenant_id BIGINT NOT NULL REFERENCES tenant(id),
    execution_id BIGINT NOT NULL,
    sequence BIGINT NOT NULL,
    event_type TEXT NOT NULL,
    data_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, execution_id, sequence),
    FOREIGN KEY (tenant_id, execution_id) REFERENCES conversation_execution(tenant_id, id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ingest_job_tenant_id ON ingest_job (tenant_id, id);

CREATE TABLE IF NOT EXISTS ingest_job_item (
    tenant_id BIGINT NOT NULL REFERENCES tenant(id),
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL,
    data_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    owner_token TEXT,
    fencing_epoch BIGINT NOT NULL DEFAULT 0,
    lease_expires_at TIMESTAMPTZ,
    cancel_requested BOOLEAN NOT NULL DEFAULT false,
    error TEXT,
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, job_id, data_id),
    FOREIGN KEY (tenant_id, job_id) REFERENCES ingest_job(tenant_id, id)
);

CREATE TABLE IF NOT EXISTS turn_idempotency (
    tenant_id BIGINT NOT NULL REFERENCES tenant(id),
    conversation_id BIGINT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    turn_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, conversation_id, idempotency_key),
    FOREIGN KEY (tenant_id, conversation_id) REFERENCES conversation(tenant_id, id),
    FOREIGN KEY (tenant_id, turn_id) REFERENCES turn(tenant_id, id)
);

CREATE TABLE IF NOT EXISTS citation (
    tenant_id BIGINT NOT NULL REFERENCES tenant(id),
    id BIGSERIAL PRIMARY KEY,
    execution_id BIGINT NOT NULL,
    evidence_id TEXT NOT NULL,
    citation_index INT NOT NULL,
    UNIQUE (tenant_id, execution_id, citation_index),
    FOREIGN KEY (tenant_id, execution_id) REFERENCES conversation_execution(tenant_id, id),
    FOREIGN KEY (tenant_id, execution_id, evidence_id)
        REFERENCES evidence_snapshot(tenant_id, execution_id, evidence_id)
);

CREATE TABLE IF NOT EXISTS customer_reply (
    tenant_id BIGINT NOT NULL REFERENCES tenant(id),
    execution_id BIGINT NOT NULL,
    text_plain TEXT,
    is_mock BOOLEAN NOT NULL DEFAULT true,
    can_copy BOOLEAN NOT NULL DEFAULT false,
    blocked_reason TEXT NOT NULL DEFAULT 'mock',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, execution_id),
    FOREIGN KEY (tenant_id, execution_id) REFERENCES conversation_execution(tenant_id, id),
    CHECK (NOT is_mock OR NOT can_copy)
);

CREATE TABLE IF NOT EXISTS ingest_payload (
    tenant_id BIGINT NOT NULL REFERENCES tenant(id),
    job_id BIGINT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, job_id),
    UNIQUE (tenant_id, idempotency_key),
    FOREIGN KEY (tenant_id, job_id) REFERENCES ingest_job(tenant_id, id)
);

CREATE TABLE IF NOT EXISTS ingest_publication (
    tenant_id BIGINT NOT NULL REFERENCES tenant(id),
    item_id BIGINT NOT NULL,
    fencing_epoch BIGINT NOT NULL,
    document_version_id BIGINT NOT NULL,
    published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, item_id),
    FOREIGN KEY (tenant_id, item_id) REFERENCES ingest_job_item(tenant_id, id),
    FOREIGN KEY (tenant_id, document_version_id)
        REFERENCES document_version(tenant_id, id)
);

CREATE OR REPLACE FUNCTION guard_ingest_item_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.fencing_epoch < OLD.fencing_epoch THEN
        RAISE EXCEPTION 'fencing epoch cannot move backwards' USING ERRCODE = '55000';
    END IF;
    IF NEW.owner_token IS DISTINCT FROM OLD.owner_token
       AND OLD.status = 'running' AND OLD.lease_expires_at > now()
       AND NOT OLD.cancel_requested THEN
        RAISE EXCEPTION 'active ingest lease cannot be taken over' USING ERRCODE = '55000';
    END IF;
    IF NEW.owner_token IS DISTINCT FROM OLD.owner_token
       AND NEW.fencing_epoch <= OLD.fencing_epoch THEN
        RAISE EXCEPTION 'new ingest owner must advance the fencing epoch' USING ERRCODE = '55000';
    END IF;
    IF NEW.status = 'done' AND OLD.status <> 'done' AND pg_trigger_depth() = 1 THEN
        RAISE EXCEPTION 'ingest item can only complete through publication' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION verify_ingest_publication_owner() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    item ingest_job_item%ROWTYPE;
BEGIN
    SELECT * INTO item FROM ingest_job_item
    WHERE tenant_id=NEW.tenant_id AND id=NEW.item_id FOR UPDATE;
    IF NOT FOUND OR item.status <> 'running' OR item.cancel_requested
       OR item.owner_token IS NULL OR item.fencing_epoch <> NEW.fencing_epoch
       OR item.lease_expires_at IS NULL OR item.lease_expires_at <= now() THEN
        RAISE EXCEPTION 'stale, cancelled, or expired worker cannot publish' USING ERRCODE = '55000';
    END IF;
    UPDATE ingest_job_item SET status='done', lease_expires_at=NULL
    WHERE tenant_id=NEW.tenant_id AND id=NEW.item_id
      AND owner_token=item.owner_token AND fencing_epoch=NEW.fencing_epoch
      AND status='running' AND NOT cancel_requested;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'worker lost ingest fencing token before publish' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_ingest_item_fencing ON ingest_job_item;
CREATE TRIGGER trg_ingest_item_fencing BEFORE UPDATE ON ingest_job_item
FOR EACH ROW EXECUTE FUNCTION guard_ingest_item_update();
DROP TRIGGER IF EXISTS trg_ingest_publication_owner ON ingest_publication;
CREATE TRIGGER trg_ingest_publication_owner BEFORE INSERT ON ingest_publication
FOR EACH ROW EXECUTE FUNCTION verify_ingest_publication_owner();

CREATE INDEX IF NOT EXISTS idx_execution_turn ON conversation_execution (tenant_id, turn_id, execution_no);
CREATE INDEX IF NOT EXISTS idx_sse_replay ON sse_event (tenant_id, execution_id, sequence);
CREATE INDEX IF NOT EXISTS idx_ingest_item_lease ON ingest_job_item (tenant_id, status, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_ingest_item_owner ON ingest_job_item (tenant_id, owner_token, fencing_epoch);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rag_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON source, document, document_version,
            ingest_job, chunk, ingest_stage, shop, conversation_execution,
            evidence_snapshot, citation, customer_reply, sse_event,
            turn_idempotency, ingest_job_item, ingest_payload TO rag_app;
        GRANT SELECT, INSERT ON ingest_publication TO rag_app;
        GRANT SELECT ON tenant, app_user TO rag_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON conversation, turn, message TO rag_app;
        GRANT USAGE, SELECT ON SEQUENCE source_id_seq, document_id_seq, document_version_id_seq,
            ingest_job_id_seq, ingest_stage_id_seq, chunk_id_seq, conversation_id_seq, turn_id_seq, message_id_seq,
            conversation_execution_id_seq, evidence_snapshot_id_seq, citation_id_seq,
            ingest_job_item_id_seq TO rag_app;
    END IF;
END $$;

INSERT INTO schema_migration(version) VALUES ('0002') ON CONFLICT (version) DO NOTHING;
COMMIT;
