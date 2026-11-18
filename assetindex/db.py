"""SQLite schema and connection helper."""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id       INTEGER PRIMARY KEY,
    root     TEXT    NOT NULL,
    rel_path TEXT    NOT NULL,
    name     TEXT    NOT NULL,
    ext      TEXT    NOT NULL,
    kind     TEXT    NOT NULL,
    size     INTEGER NOT NULL,
    mtime    REAL    NOT NULL,
    scan_id  INTEGER NOT NULL,
    seq_key    TEXT,     -- see assetindex/sequences.py
    seq_index  INTEGER,
    seq_frames INTEGER,
    UNIQUE (root, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_files_kind ON files(kind);
CREATE INDEX IF NOT EXISTS idx_files_rel_path ON files(rel_path, id);
CREATE INDEX IF NOT EXISTS idx_files_root_scan ON files(root, scan_id);

-- Trigram index makes LIKE '%term%' fast for terms of 3+ chars;
-- shorter terms (e.g. 2-char Chinese) still work via a full scan.
CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
    rel_path, content='files', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
    INSERT INTO files_fts(rowid, rel_path) VALUES (new.id, new.rel_path);
END;
CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
    INSERT INTO files_fts(files_fts, rowid, rel_path) VALUES ('delete', old.id, old.rel_path);
END;
-- Only rel_path is indexed; rescans that just bump scan_id/size don't touch FTS.
CREATE TRIGGER IF NOT EXISTS files_au AFTER UPDATE OF rel_path ON files BEGIN
    INSERT INTO files_fts(files_fts, rowid, rel_path) VALUES ('delete', old.id, old.rel_path);
    INSERT INTO files_fts(rowid, rel_path) VALUES (new.id, new.rel_path);
END;

-- Archive contents (see assetindex/archive_index.py). size/mtime are the archive file's
-- values when listed: a mismatch with `files` means it must be listed again.
CREATE TABLE IF NOT EXISTS archives (
    file_id        INTEGER PRIMARY KEY,
    size           INTEGER NOT NULL,
    mtime          REAL    NOT NULL,
    status         TEXT    NOT NULL,  -- 'ok' | 'error'
    error          TEXT,
    entry_count    INTEGER NOT NULL DEFAULT 0,
    truncated      INTEGER NOT NULL DEFAULT 0,
    engine         TEXT,              -- 'unreal' | 'unity' | NULL
    engine_version TEXT,              -- inferred from folder names, e.g. '5.1'
    preview_state  INTEGER,           -- NULL pending, 0 nothing to show, 1 sheet built
    sheet_version  INTEGER            -- layout the sheet was built with; older ones get rebuilt
);
CREATE TABLE IF NOT EXISTS archive_entries (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER NOT NULL,
    path        TEXT    NOT NULL,
    ext         TEXT    NOT NULL,
    size        INTEGER NOT NULL,
    guid        TEXT,                 -- unitypackage asset folder
    has_preview INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_entries_file ON archive_entries(file_id, path);
-- Archives the scanner was working on when it last died (see assetindex/attempts.py).
CREATE TABLE IF NOT EXISTS scan_attempts (
    file_id  INTEGER NOT NULL,
    stage    TEXT    NOT NULL,  -- 'list' | 'preview'
    attempts INTEGER NOT NULL,
    PRIMARY KEY (file_id, stage)
);
CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
    path, content='archive_entries', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON archive_entries BEGIN
    INSERT INTO entries_fts(rowid, path) VALUES (new.id, new.path);
END;
CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON archive_entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, path) VALUES ('delete', old.id, old.path);
END;
-- A trigger rather than ON DELETE CASCADE: foreign keys are off unless enabled per connection.
CREATE TRIGGER IF NOT EXISTS files_ad_archive AFTER DELETE ON files BEGIN
    DELETE FROM archive_entries WHERE file_id = old.id;
    DELETE FROM archives WHERE file_id = old.id;
END;
-- Semantic search vectors (see assetindex/embed_stage.py), one per card and source.
CREATE TABLE IF NOT EXISTS embeddings (
    file_id INTEGER NOT NULL,
    source  TEXT    NOT NULL,  -- 'text' | 'image'
    model   TEXT    NOT NULL,
    mtime   REAL    NOT NULL,  -- the file's mtime when embedded: a change means redo
    vector  BLOB,              -- float32, unit length; NULL when the input was rejected
    error   TEXT,
    PRIMARY KEY (file_id, source)
);
CREATE TRIGGER IF NOT EXISTS files_ad_embeddings AFTER DELETE ON files BEGIN
    DELETE FROM embeddings WHERE file_id = old.id;
END;
-- What a vision model says a card's picture shows (see assetindex/caption_job.py).
CREATE TABLE IF NOT EXISTS captions (
    file_id INTEGER PRIMARY KEY,
    model   TEXT    NOT NULL,
    mtime   REAL    NOT NULL,  -- the file's mtime when described: a change means redo
    text    TEXT,              -- NULL when describing failed
    error   TEXT,
    created REAL    NOT NULL
);
-- Write captions with INSERT ... ON CONFLICT DO UPDATE, not INSERT OR REPLACE: REPLACE
-- skips delete triggers and would leave stale rows in this index.
CREATE VIRTUAL TABLE IF NOT EXISTS captions_fts USING fts5(
    text, content='captions', content_rowid='file_id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS captions_ai AFTER INSERT ON captions BEGIN
    INSERT INTO captions_fts(rowid, text) VALUES (new.file_id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS captions_ad AFTER DELETE ON captions BEGIN
    INSERT INTO captions_fts(captions_fts, rowid, text) VALUES ('delete', old.file_id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS captions_au AFTER UPDATE ON captions BEGIN
    INSERT INTO captions_fts(captions_fts, rowid, text) VALUES ('delete', old.file_id, old.text);
    INSERT INTO captions_fts(rowid, text) VALUES (new.file_id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS files_ad_captions AFTER DELETE ON files BEGIN
    DELETE FROM captions WHERE file_id = old.id;
END;
-- Separate trigger so databases created before scan_attempts existed get it too.
CREATE TRIGGER IF NOT EXISTS files_ad_attempts AFTER DELETE ON files BEGIN
    DELETE FROM scan_attempts WHERE file_id = old.id;  -- ids get reused
END;
-- Organizing (see assetindex/organize.py). Content hashes, only for files whose size
-- another file shares: the rest can't have a duplicate.
CREATE TABLE IF NOT EXISTS file_hashes (
    file_id INTEGER PRIMARY KEY,
    size    INTEGER NOT NULL,
    mtime   REAL    NOT NULL,  -- the file's when hashed: a change means hash again
    hash    TEXT    NOT NULL
);
CREATE TRIGGER IF NOT EXISTS files_ad_hashes AFTER DELETE ON files BEGIN
    DELETE FROM file_hashes WHERE file_id = old.id;
END;
-- A cheap first look: the size plus three small slices (start, middle, end). Only files
-- whose sample matches another's, in packs that could be twins, get a full hash.
CREATE TABLE IF NOT EXISTS file_samples (
    file_id INTEGER PRIMARY KEY,
    size    INTEGER NOT NULL,
    mtime   REAL    NOT NULL,
    sample  TEXT    NOT NULL
);
CREATE TRIGGER IF NOT EXISTS files_ad_samples AFTER DELETE ON files BEGIN
    DELETE FROM file_samples WHERE file_id = old.id;
END;
-- Folders the user opened: their children are units (packs) of their own.
CREATE TABLE IF NOT EXISTS unit_splits (
    root TEXT NOT NULL,
    path TEXT NOT NULL,
    PRIMARY KEY (root, path)
);
-- Folders the user (or a decision on them) keeps as one pack, whatever the default rule says.
-- Tags the vision model gave a pack for search (see assetindex/tags.py).
CREATE TABLE IF NOT EXISTS pack_tags (
    root    TEXT NOT NULL,
    path    TEXT NOT NULL,
    view    TEXT NOT NULL,
    style   TEXT NOT NULL,
    themes  TEXT NOT NULL,  -- joined by 、
    model   TEXT NOT NULL,
    created REAL NOT NULL,
    PRIMARY KEY (root, path)
);
CREATE TABLE IF NOT EXISTS unit_joins (
    root TEXT NOT NULL,
    path TEXT NOT NULL,
    PRIMARY KEY (root, path)
);
-- Copies the user chose to keep when identical packs are found (else the default pick).
CREATE TABLE IF NOT EXISTS dup_keep (
    root TEXT NOT NULL,
    path TEXT NOT NULL,
    PRIMARY KEY (root, path)
);
-- One row per unit: where the organizer proposes to put it, and what the user decided.
CREATE TABLE IF NOT EXISTS organize_units (
    root      TEXT    NOT NULL,
    path      TEXT    NOT NULL,
    is_dir    INTEGER NOT NULL,
    files     INTEGER NOT NULL,
    size      INTEGER NOT NULL,
    signature TEXT    NOT NULL,  -- of its files (paths, sizes, mtimes): a change makes the proposal stale
    dup_of    TEXT,              -- path of the identical unit that is kept
    category  TEXT,
    name      TEXT,
    target    TEXT,              -- proposed path under the root
    reason    TEXT,
    status    TEXT    NOT NULL DEFAULT 'todo',  -- todo | proposed | accepted | rejected
    model     TEXT,
    error     TEXT,
    updated   REAL,
    PRIMARY KEY (root, path)
);
-- Links queued for the fetcher service (see assetindex/downloads.py, fetch_job.py).
CREATE TABLE IF NOT EXISTS downloads (
    id       INTEGER PRIMARY KEY,
    root     TEXT    NOT NULL,
    url      TEXT    NOT NULL,
    title    TEXT,
    source   TEXT,              -- site it was found on, NULL for a pasted link
    source_id TEXT,             -- the asset's id on that site
    md5      TEXT,              -- given by the site: aria2 checks the file against it
    size     INTEGER,           -- expected bytes, if known
    status   TEXT    NOT NULL DEFAULT 'queued',  -- queued | active | done | duplicate | failed | cancelled
    gid      TEXT,              -- aria2's id while it downloads
    path     TEXT,              -- where it landed, under the root
    dup_of   TEXT,              -- an identical file already in the library
    error    TEXT,
    created  REAL    NOT NULL,
    finished REAL
);
"""

# Columns added after the first release; old databases get them via ALTER TABLE.
ADDED_COLUMNS = (
    ("files", "seq_key", "TEXT"),
    ("files", "seq_index", "INTEGER"),
    ("files", "seq_frames", "INTEGER"),
    ("archives", "preview_state", "INTEGER"),
    ("archives", "sheet_version", "INTEGER"),
    ("archives", "preview_postponed", "INTEGER"),  # scans whose preview timed out, in a row
    ("downloads", "source_id", "TEXT"),
)
# Created after migration, since they reference added columns.
POST_MIGRATION_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_files_seq ON files(root, seq_key, seq_index)",
    # Maps a matched hidden frame to its sequence head during search.
    "CREATE INDEX IF NOT EXISTS idx_files_seq_head ON files(root, seq_key) WHERE seq_frames > 0",
)


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {
        table: {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for table in {t for t, _, _ in ADDED_COLUMNS}
    }
    with conn:
        for table, name, sql_type in ADDED_COLUMNS:
            if name not in existing[table]:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
                except sqlite3.OperationalError as err:
                    # Web server and scanner may migrate the same old DB at once.
                    if "duplicate column" not in str(err):
                        raise
        for sql in POST_MIGRATION_SQL:
            conn.execute(sql)


def connect(db_path: Path, init_schema: bool = True) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # FastAPI may run a request's dependency and handler on different threadpool threads.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    # Keep SQLite temp files (statement journals, FTS merges) in memory: the system
    # temp dir may be unwritable or scanned by antivirus, which fails big transactions.
    conn.execute("PRAGMA temp_store=MEMORY")
    if init_schema:
        conn.executescript(SCHEMA)
        _migrate(conn)
    return conn
