-- Oracle 23ai: KB tables for configurable embedding service
-- Replace VECTOR dimension (1536) if your embedding model differs.
-- Requires compatible parameter for VECTOR features (see Oracle AI Vector Search docs).

--------------------------------------------------------------------------------
-- Corpus: one logical dataset + JSON config (validated by application)
--------------------------------------------------------------------------------
CREATE TABLE kb_corpus (
    corpus_id       RAW(16) NOT NULL,
    name            VARCHAR2(256) NOT NULL,
    slug            VARCHAR2(128) NOT NULL,
    config_json     JSON NOT NULL,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT pk_kb_corpus PRIMARY KEY (corpus_id),
    CONSTRAINT uk_kb_corpus_slug UNIQUE (slug)
);

CREATE INDEX idx_kb_corpus_name ON kb_corpus (name);

--------------------------------------------------------------------------------
-- Chunks: one row per embeddable unit (multiple per parent when chunked)
-- Partitioning: LIST by corpus_id keeps large tenants physically separate.
-- Add new corpus values via ALTER TABLE ... MODIFY PARTITION ... ADD VALUES, or use single DEFAULT partition in dev.
--------------------------------------------------------------------------------
CREATE TABLE kb_chunk (
    chunk_id            RAW(16) NOT NULL,
    corpus_id           RAW(16) NOT NULL,
    external_parent_id  VARCHAR2(64) NOT NULL,
    chunk_index         NUMBER(10,0) DEFAULT 0 NOT NULL,
    chunker_version     VARCHAR2(32) DEFAULT '1' NOT NULL,
    content_hash        VARCHAR2(64) NOT NULL,
    chunk_text          CLOB NOT NULL,
    payload_json        JSON,
    embedding           VECTOR(1536, FLOAT32),
    embedding_model     VARCHAR2(256),
    embedding_version   VARCHAR2(32),
    source_uri          VARCHAR2(1024),
    source_line         NUMBER(12,0),
    created_at          TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    updated_at          TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT pk_kb_chunk PRIMARY KEY (chunk_id),
    CONSTRAINT fk_kb_chunk_corpus FOREIGN KEY (corpus_id) REFERENCES kb_corpus (corpus_id) ON DELETE CASCADE
)
PARTITION BY LIST (corpus_id) (
    PARTITION p_default VALUES (DEFAULT)
);

-- One logical chunk slot per (corpus, parent, index, chunker_version); MERGE updates hash/text/vector when content changes.
CREATE UNIQUE INDEX uk_kb_chunk_natural ON kb_chunk (corpus_id, external_parent_id, chunk_index, chunker_version)
    LOCAL;

CREATE INDEX idx_kb_chunk_corpus_parent ON kb_chunk (corpus_id, external_parent_id) LOCAL;

--------------------------------------------------------------------------------
-- Ingest / reindex jobs
--------------------------------------------------------------------------------
CREATE TABLE kb_ingest_job (
    job_id        RAW(16) NOT NULL,
    corpus_id     RAW(16) NOT NULL,
    job_type      VARCHAR2(32) NOT NULL,
    status        VARCHAR2(32) NOT NULL,
    stats_json    JSON,
    error_msg     VARCHAR2(4000),
    created_at    TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    updated_at    TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT pk_kb_ingest_job PRIMARY KEY (job_id),
    CONSTRAINT fk_kb_ingest_job_corpus FOREIGN KEY (corpus_id) REFERENCES kb_corpus (corpus_id) ON DELETE CASCADE
);

CREATE INDEX idx_kb_ingest_job_corpus ON kb_ingest_job (corpus_id, created_at DESC);

COMMENT ON TABLE kb_corpus IS 'Logical dataset + per-corpus embedding/chunking config (JSON).';
COMMENT ON TABLE kb_chunk IS 'Embeddable text chunks + VECTOR column; partition by corpus_id.';
COMMENT ON TABLE kb_ingest_job IS 'Async ingest/reindex job tracking.';
