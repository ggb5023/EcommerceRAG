-- Canonical executable schema; validate in an isolated database first.
-- 18 domain tables + schema_migration bookkeeping. Do not run on startup.
BEGIN;
SELECT pg_advisory_xact_lock(742018);
CREATE TABLE IF NOT EXISTS schema_migration (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now());

CREATE TABLE tenant (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT        NOT NULL,
    status      TEXT        NOT NULL DEFAULT 'active',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE app_user (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   BIGINT      NOT NULL REFERENCES tenant(id),
    external_id TEXT,
    role        TEXT        NOT NULL,
    shop_ids    TEXT[],
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, external_id)
);

CREATE TABLE acl (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL,
    resource_type TEXT   NOT NULL,
    resource_id   BIGINT NOT NULL,
    subject_type  TEXT   NOT NULL,
    subject_id    TEXT   NOT NULL,
    permission    TEXT   NOT NULL,
    UNIQUE (resource_type, resource_id, subject_type, subject_id)
);

CREATE TABLE source (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       BIGINT      NOT NULL REFERENCES tenant(id),
    shop_id         TEXT,
    type            TEXT        NOT NULL,
    uri             TEXT,
    schedule        TEXT,
    last_checked_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE document (
    id                BIGSERIAL PRIMARY KEY,
    tenant_id         BIGINT      NOT NULL REFERENCES tenant(id),
    source_id         BIGINT      REFERENCES source(id),
    logical_key       TEXT        NOT NULL,
    title             TEXT,
    doc_type          TEXT,
    active_version_id BIGINT,
    status            TEXT        NOT NULL DEFAULT 'active',
    meta_json         JSONB       NOT NULL DEFAULT '{}',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, logical_key)
);

CREATE TABLE document_version (
    tenant_id BIGINT NOT NULL REFERENCES tenant(id),
    id                  BIGSERIAL PRIMARY KEY,
    document_id         BIGINT      NOT NULL REFERENCES document(id),
    source_hash         TEXT        NOT NULL,

    parser_version      TEXT        NOT NULL,
    chunk_rule_version  TEXT        NOT NULL,
    embedding_model     TEXT        NOT NULL,
    pipeline_version    TEXT        NOT NULL,

    object_key          TEXT        NOT NULL,
    parsed_object_key   TEXT,

    status              TEXT        NOT NULL,
    chunk_count         INT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at        TIMESTAMPTZ
);

CREATE TABLE ingest_stage (
    tenant_id BIGINT NOT NULL REFERENCES tenant(id),
    id                  BIGSERIAL PRIMARY KEY,
    source_id           BIGINT      NOT NULL,
    content_hash        TEXT        NOT NULL,
    stage               TEXT        NOT NULL,
    stage_key           TEXT        NOT NULL,
    document_version_id BIGINT,
    status              TEXT        NOT NULL,
    error               TEXT,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (source_id, content_hash, stage, stage_key)
);

CREATE TABLE ingest_job (
    tenant_id BIGINT NOT NULL REFERENCES tenant(id),
    id                  BIGSERIAL PRIMARY KEY,
    source_id           BIGINT      NOT NULL,
    document_version_id BIGINT,
    idempotency_key     TEXT        NOT NULL,
    stage               TEXT        NOT NULL,
    status              TEXT        NOT NULL,
    lease_expires_at     TIMESTAMPTZ,
    cancel_requested    BOOLEAN NOT NULL DEFAULT false,
    retry_count         INT         NOT NULL DEFAULT 0,
    error               TEXT,

    mineru_task_id      TEXT,
    mineru_batch_id     TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE chunk (
    id                  BIGSERIAL PRIMARY KEY,

    version_id          BIGINT      NOT NULL REFERENCES document_version(id),
    doc_id              BIGINT      NOT NULL,
    tenant_id           BIGINT      NOT NULL,
    shop_id             TEXT,

    chunk_index         INT         NOT NULL,
    section_seq         INT         NOT NULL,
    section_chunk_index INT         NOT NULL,
    char_start          INT,
    char_end            INT,
    page_no             INT,
    heading_path        TEXT,
    heading_path_ids    BIGINT[],
    heading_level       SMALLINT,

    content             TEXT        NOT NULL,
    embed_text          TEXT        NOT NULL,
    token_count         INT         NOT NULL,
    content_type        TEXT        NOT NULL,
    split_reason        TEXT        NOT NULL,

    embedding           halfvec(1024),
    sparse_embedding    sparsevec,
    embedding_model     TEXT        NOT NULL,
    embedding_dim       SMALLINT    NOT NULL,
    tokenizer_id        TEXT        NOT NULL,

    effective_from      DATE,
    effective_to        DATE,
    meta_json           JSONB       NOT NULL DEFAULT '{}',

    image_refs          JSONB       NOT NULL DEFAULT '[]',

    source_object_key   TEXT,
    parsed_object_key   TEXT,
    chunk_hash          TEXT        NOT NULL,
    lang                TEXT        NOT NULL DEFAULT 'zh',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE conversation (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   BIGINT      NOT NULL,
    user_id     BIGINT      NOT NULL,
    shop_id     TEXT,
    title       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE turn (
    tenant_id BIGINT NOT NULL REFERENCES tenant(id),
    id                  BIGSERIAL PRIMARY KEY,
    conversation_id     BIGINT      NOT NULL REFERENCES conversation(id),
    turn_no             INT         NOT NULL,
    request_id          TEXT        NOT NULL,
    user_query          TEXT        NOT NULL,
    answer              TEXT,
    status              TEXT        NOT NULL,
    intent              TEXT,
    information_source  TEXT,
    latency_ms          INT,
    degraded_stages     TEXT[],
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (conversation_id, turn_no),
    UNIQUE (request_id)
);

CREATE TABLE message (
    tenant_id BIGINT NOT NULL REFERENCES tenant(id),
    id              BIGSERIAL PRIMARY KEY,
    turn_id         BIGINT      NOT NULL REFERENCES turn(id),
    role            TEXT        NOT NULL,
    content         TEXT,
    tool_calls_json JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE retrieval_trace (
    tenant_id BIGINT NOT NULL REFERENCES tenant(id),
    id              BIGSERIAL PRIMARY KEY,
    request_id      TEXT        NOT NULL,
    turn_no         INT,
    stage           TEXT        NOT NULL,
    candidates_json JSONB,
    scores_json     JSONB,
    latency_ms      INT,
    degraded        BOOLEAN     NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE routing_trace (
    tenant_id BIGINT NOT NULL REFERENCES tenant(id),
    id                  BIGSERIAL PRIMARY KEY,
    request_id          TEXT        NOT NULL,
    turn_no             INT,
    intent              TEXT,
    information_source  TEXT,
    confidence          NUMERIC(4,3),
    route_source        TEXT,
    entities_json       JSONB,
    reason              TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE tool_call_trace (
    tenant_id BIGINT NOT NULL REFERENCES tenant(id),
    id              BIGSERIAL PRIMARY KEY,
    request_id      TEXT        NOT NULL,
    turn_no         INT,
    tool_name       TEXT        NOT NULL,
    args_json       JSONB,
    result_status   TEXT,
    latency_ms      INT,
    cache_hit       BOOLEAN     NOT NULL DEFAULT false,
    degraded        BOOLEAN     NOT NULL DEFAULT false,
    credits_used    NUMERIC(8,4),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE feedback (
    tenant_id BIGINT NOT NULL REFERENCES tenant(id),
    id          BIGSERIAL PRIMARY KEY,
    turn_id     BIGINT      NOT NULL,
    user_id     BIGINT,
    type        TEXT        NOT NULL,
    comment     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE eval_case (
    tenant_id BIGINT NOT NULL REFERENCES tenant(id),
    id                  BIGSERIAL PRIMARY KEY,
    query               TEXT        NOT NULL,
    expected_doc_ids    BIGINT[],
    expected_answer     TEXT,
    intent              TEXT,
    information_source  TEXT,
    tags                TEXT[],
    source              TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE eval_run (
    tenant_id BIGINT NOT NULL REFERENCES tenant(id),
    id                  BIGSERIAL PRIMARY KEY,
    eval_set_version    TEXT        NOT NULL,
    pipeline_version    TEXT        NOT NULL,
    model_version       TEXT        NOT NULL,
    ndcg5               NUMERIC(5,4),
    recall              NUMERIC(5,4),
    pass_gate           BOOLEAN     NOT NULL,
    detail_json         JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE app_user ADD UNIQUE (tenant_id, id);
ALTER TABLE acl ADD FOREIGN KEY (tenant_id) REFERENCES tenant(id);
ALTER TABLE source ADD UNIQUE (tenant_id, id);
ALTER TABLE document ADD UNIQUE (tenant_id, id);
ALTER TABLE document ADD FOREIGN KEY (tenant_id, source_id) REFERENCES source(tenant_id, id);
ALTER TABLE document_version ADD UNIQUE (tenant_id, document_id, id);
ALTER TABLE document_version ADD UNIQUE (tenant_id, id);
ALTER TABLE document_version ADD FOREIGN KEY (tenant_id, document_id) REFERENCES document(tenant_id, id);
ALTER TABLE document ADD FOREIGN KEY (tenant_id, id, active_version_id)
    REFERENCES document_version(tenant_id, document_id, id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE ingest_stage ADD FOREIGN KEY (tenant_id, source_id) REFERENCES source(tenant_id, id);
ALTER TABLE ingest_stage ADD FOREIGN KEY (tenant_id, document_version_id) REFERENCES document_version(tenant_id, id);
ALTER TABLE ingest_job ADD FOREIGN KEY (tenant_id, source_id) REFERENCES source(tenant_id, id);
ALTER TABLE ingest_job ADD FOREIGN KEY (tenant_id, document_version_id) REFERENCES document_version(tenant_id, id);
ALTER TABLE ingest_job ADD UNIQUE (tenant_id, idempotency_key);
ALTER TABLE ingest_job ADD CHECK (status IN ('pending','running','done','failed','cancelled'));
ALTER TABLE ingest_stage ADD CHECK (status IN ('pending','running','done','failed','cancelled'));
ALTER TABLE chunk ADD FOREIGN KEY (tenant_id, doc_id, version_id) REFERENCES document_version(tenant_id, document_id, id);
ALTER TABLE chunk ADD UNIQUE (version_id, chunk_index);
ALTER TABLE chunk ADD UNIQUE (version_id, section_seq, section_chunk_index);
ALTER TABLE chunk ADD CHECK (embedding_dim = 1024);
ALTER TABLE chunk ADD CHECK (token_count > 0 AND token_count <= 1024);
ALTER TABLE chunk ADD CHECK (section_seq >= 1 AND section_chunk_index >= 0 AND chunk_index >= 0);
ALTER TABLE chunk ADD CHECK (effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from);
ALTER TABLE conversation ADD UNIQUE (tenant_id, id);
ALTER TABLE conversation ADD FOREIGN KEY (tenant_id, user_id) REFERENCES app_user(tenant_id, id);
ALTER TABLE turn ADD UNIQUE (tenant_id, id);
ALTER TABLE turn ADD UNIQUE (tenant_id, request_id);
ALTER TABLE turn ADD FOREIGN KEY (tenant_id, conversation_id) REFERENCES conversation(tenant_id, id);
ALTER TABLE message ADD FOREIGN KEY (tenant_id, turn_id) REFERENCES turn(tenant_id, id);
ALTER TABLE feedback ADD FOREIGN KEY (tenant_id, turn_id) REFERENCES turn(tenant_id, id);
ALTER TABLE feedback ADD FOREIGN KEY (tenant_id, user_id) REFERENCES app_user(tenant_id, id);
ALTER TABLE retrieval_trace ADD FOREIGN KEY (tenant_id, request_id) REFERENCES turn(tenant_id, request_id);
ALTER TABLE routing_trace ADD FOREIGN KEY (tenant_id, request_id) REFERENCES turn(tenant_id, request_id);
ALTER TABLE tool_call_trace ADD FOREIGN KEY (tenant_id, request_id) REFERENCES turn(tenant_id, request_id);

CREATE INDEX idx_chunk_embedding ON chunk USING hnsw (embedding halfvec_cosine_ops);
CREATE INDEX idx_chunk_tenant_shop ON chunk (tenant_id, shop_id);
CREATE INDEX idx_chunk_hash ON chunk (version_id, chunk_hash);
CREATE INDEX idx_chunk_meta ON chunk USING gin (meta_json jsonb_path_ops);
CREATE INDEX idx_chunk_policy_time ON chunk (tenant_id, effective_from, effective_to) WHERE effective_from IS NOT NULL;
CREATE INDEX idx_job_poll ON ingest_job (tenant_id, status, updated_at);
INSERT INTO schema_migration(version) VALUES ('0001');
COMMIT;
