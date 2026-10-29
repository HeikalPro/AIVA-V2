-- TEST FIXTURE ONLY — isolated copies of kb_corpus / kb_chunk for integration tests.
--
-- Same columns, JSON/VECTOR types and natural-key unique index as
-- embedding_service/db/sql/01_schema.sql, under DI_TEST_* names, so the publish SQL
-- (VECTOR insert, SELECT ... FOR UPDATE config edit, delete-by-parent) can be
-- exercised without ever touching the real kb_corpus / kb_chunk.
-- Apply:    python -m backend.doc_intel.migrate apply --file backend/tests/doc_intel/sql/DI_TEST_kb_tables.sql --confirm-schema <SCHEMA> --yes
-- Remove:   python -m backend.doc_intel.migrate rollback --file backend/tests/doc_intel/sql/DI_TEST_kb_tables.sql --confirm-schema <SCHEMA> --yes

CREATE TABLE DI_TEST_KB_CORPUS (
    corpus_id    RAW(16)        NOT NULL,
    name         VARCHAR2(256)  NOT NULL,
    slug         VARCHAR2(128)  NOT NULL,
    config_json  JSON           NOT NULL,
    created_at   TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    updated_at   TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT pk_di_test_kb_corpus PRIMARY KEY (corpus_id),
    CONSTRAINT uk_di_test_kb_corpus_slug UNIQUE (slug)
);

CREATE TABLE DI_TEST_KB_CHUNK (
    chunk_id            RAW(16)       NOT NULL,
    corpus_id           RAW(16)       NOT NULL,
    external_parent_id  VARCHAR2(64)  NOT NULL,
    chunk_index         NUMBER(10,0)  DEFAULT 0 NOT NULL,
    chunker_version     VARCHAR2(32)  DEFAULT '1' NOT NULL,
    content_hash        VARCHAR2(64)  NOT NULL,
    chunk_text          CLOB          NOT NULL,
    payload_json        JSON,
    embedding           VECTOR(1536, FLOAT32),
    embedding_model     VARCHAR2(256),
    embedding_version   VARCHAR2(32),
    source_uri          VARCHAR2(1024),
    source_line         NUMBER(12,0),
    created_at          TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    updated_at          TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT pk_di_test_kb_chunk PRIMARY KEY (chunk_id),
    CONSTRAINT fk_di_test_kb_chunk_corpus FOREIGN KEY (corpus_id)
        REFERENCES DI_TEST_KB_CORPUS (corpus_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX uk_di_test_kb_chunk_natural
    ON DI_TEST_KB_CHUNK (corpus_id, external_parent_id, chunk_index, chunker_version);

CREATE INDEX idx_di_test_kb_chunk_parent ON DI_TEST_KB_CHUNK (corpus_id, external_parent_id);
