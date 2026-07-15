"""SQLite 저장소. PMID를 자연키로 쓰고, 논문↔카테고리는 N:M."""
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    pmid           TEXT PRIMARY KEY,
    doi            TEXT,
    title          TEXT NOT NULL,
    abstract       TEXT,
    journal        TEXT,
    journal_issn   TEXT,
    pub_year       INTEGER,
    pub_date       TEXT,
    authors        TEXT,          -- JSON 배열
    pub_types      TEXT,          -- JSON 배열
    mesh_terms     TEXT,          -- JSON 배열
    metric_value   REAL,          -- IF 프록시 (OpenAlex 2yr mean citedness)
    metric_source  TEXT,
    passed_filter  INTEGER DEFAULT 0,   -- 0=탈락, 1=통과, 2=지표불명(flag)
    summary        TEXT,          -- Fable 산출물 (nullable)
    tags           TEXT,          -- Fable 산출물, JSON 배열
    fetched_at     TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS paper_categories (
    pmid        TEXT NOT NULL,
    category_id TEXT NOT NULL,
    PRIMARY KEY (pmid, category_id),
    FOREIGN KEY (pmid) REFERENCES papers(pmid) ON DELETE CASCADE
);

-- 저널 지표 캐시. 매번 OpenAlex를 때리지 않기 위함.
CREATE TABLE IF NOT EXISTS journal_metrics (
    issn         TEXT PRIMARY KEY,
    journal_name TEXT,
    metric_value REAL,
    source       TEXT,
    updated_at   TEXT DEFAULT (datetime('now'))
);

-- 증분 수집용 워터마크. 카테고리별 마지막 성공 실행 시각.
CREATE TABLE IF NOT EXISTS run_state (
    category_id TEXT PRIMARY KEY,
    last_run_at TEXT,
    last_edat   TEXT      -- PubMed Entrez date 기준 커서 (YYYY/MM/DD)
);

CREATE INDEX IF NOT EXISTS idx_papers_year   ON papers(pub_year);
CREATE INDEX IF NOT EXISTS idx_papers_pass   ON papers(passed_filter);
CREATE INDEX IF NOT EXISTS idx_cat_category  ON paper_categories(category_id);
"""


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def upsert_paper(conn, rec: dict) -> None:
    cols = ["pmid", "doi", "title", "abstract", "journal", "journal_issn",
            "pub_year", "pub_date", "authors", "pub_types", "mesh_terms"]
    vals = [rec.get(c) for c in cols]
    placeholders = ",".join("?" * len(cols))
    updates = ",".join(f"{c}=excluded.{c}" for c in cols[1:])
    conn.execute(
        f"INSERT INTO papers ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(pmid) DO UPDATE SET {updates}",
        vals,
    )


def link_category(conn, pmid: str, category_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO paper_categories (pmid, category_id) VALUES (?, ?)",
        (pmid, category_id),
    )


def set_filter_result(conn, pmid: str, value, source: str, passed: int) -> None:
    conn.execute(
        "UPDATE papers SET metric_value=?, metric_source=?, passed_filter=? WHERE pmid=?",
        (value, source, passed, pmid),
    )


def get_cursor(conn, category_id: str):
    row = conn.execute(
        "SELECT last_edat FROM run_state WHERE category_id=?", (category_id,)
    ).fetchone()
    return row["last_edat"] if row else None


def set_cursor(conn, category_id: str, edat: str) -> None:
    conn.execute(
        "INSERT INTO run_state (category_id, last_run_at, last_edat) "
        "VALUES (?, datetime('now'), ?) "
        "ON CONFLICT(category_id) DO UPDATE SET last_run_at=datetime('now'), last_edat=excluded.last_edat",
        (category_id, edat),
    )
