-- seekdb DDL for 讲义库索引器 (vault)
-- 连接: mysql -h127.0.0.1 -P20881 -uroot < schema.sql

CREATE DATABASE IF NOT EXISTS vault;

CREATE TABLE IF NOT EXISTS vault.notes (
  note_id         BIGINT NOT NULL PRIMARY KEY,
  rel_path        VARCHAR(768) NOT NULL,
  title           VARCHAR(512),
  course          VARCHAR(256),
  course_code     VARCHAR(32),
  lecture_no      INT,
  kind            VARCHAR(16),
  agent           VARCHAR(32),
  zh_title        VARCHAR(256),
  en_title        VARCHAR(512),
  has_frontmatter TINYINT DEFAULT 0,
  char_count      INT,
  ingested_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_notes_path (rel_path(255)),
  KEY idx_notes_course (course(64))
);

CREATE TABLE IF NOT EXISTS vault.chunks (
  chunk_id     BIGINT NOT NULL PRIMARY KEY,
  note_id      BIGINT NOT NULL,
  seq          INT NOT NULL,
  heading_path VARCHAR(768),
  content      TEXT,
  page_start   INT,
  page_end     INT,
  char_count   INT,
  emb          VECTOR(512),
  KEY idx_chunks_note (note_id),
  FULLTEXT INDEX ft_chunks_content (content) WITH PARSER ik,
  VECTOR INDEX vidx_chunks_emb (emb) WITH (DISTANCE=l2, TYPE=hnsw, LIB=vsag)
) ORGANIZATION = HEAP;

CREATE TABLE IF NOT EXISTS vault.glossary (
  glossary_id   BIGINT NOT NULL PRIMARY KEY,
  note_id       BIGINT NOT NULL,
  term          VARCHAR(128) NOT NULL,
  term_en       VARCHAR(256),
  definition    TEXT,
  first_seen    VARCHAR(64),
  introduced_in VARCHAR(64),
  KEY idx_gloss_note (note_id),
  KEY idx_gloss_term (term)
);

CREATE TABLE IF NOT EXISTS vault.links (
  link_id      BIGINT NOT NULL PRIMARY KEY,
  src_note_id  BIGINT NOT NULL,
  src_chunk_id BIGINT,
  link_text    VARCHAR(256),
  target_rel   VARCHAR(768),
  anchor       VARCHAR(256),
  dst_note_id  BIGINT,
  valid        TINYINT NOT NULL DEFAULT 0,
  KEY idx_links_src (src_note_id),
  KEY idx_links_dst (dst_note_id),
  KEY idx_links_valid (valid)
);
