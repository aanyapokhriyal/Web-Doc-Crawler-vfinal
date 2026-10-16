"""
storage.py — persistence layer for the multi-site document crawler.

Design decision (from project planning): SQLite is the single source of
truth across ALL sites (one shared crawler.db, not one DB per site — this
mirrors web-doc-crawler's pattern, since it's what makes cross-site dedup,
run history, and the breakers log possible at all). Excel is never written
to directly during a crawl; it is always *generated from* the database on
demand. This avoids the class of bugs multisite_crawler had (writing
straight to .xlsx meant partial/corrupt state on a crash, and no real
dedup beyond exact URL string matching).

Three Excel outputs come out of this module:
  1. export_site_workbook()   — one workbook per site, the human-reviewed
                                 deliverable (15 columns + Change_Log +
                                 Naming_Convention tabs).
  2. export_master_rollup()   — one row per site, current totals across
                                 however many of the 300 sites have run.
  3. export_breakers_log()    — ONE continuous file across every site and
                                 every run. It is regenerated in full from
                                 the `breakers` table on every export call,
                                 but because that table is cumulative and
                                 nothing is ever deleted from it, the
                                 *effect* is append-only: nothing already
                                 logged is ever lost, even though the file
                                 on disk is technically rewritten each
                                 time. This was chosen over incrementally
                                 editing an existing .xlsx in place, which
                                 risks partial writes / formatting drift
                                 across hundreds of runs.

Nothing in this file makes network calls or knows about Playwright/
requests — it only persists and reports on data crawler.py hands it.
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_DB_PATH = "crawler.db"

# Excel's underlying file format outright REJECTS certain control
# characters (this is openpyxl's own detection regex, reused here) —
# writing one raises IllegalCharacterError and crashes the ENTIRE export,
# not just the one bad cell, since a workbook is only saved to disk once
# the whole thing has been built. These sometimes end up embedded in text
# scraped from real government sites (a common artifact of how some CMS
# platforms convert Word documents to HTML). A real CERC crawl hit this on
# a single document title after 21,000+ successful downloads, losing
# EVERY export for that run — not just that one row — since the crash
# happens mid-build, before wb.save() is ever reached.
_ILLEGAL_EXCEL_CHARACTERS_RE = re.compile(r"[\000-\010]|[\013-\014]|[\016-\037]")


def _sanitize_for_excel(value):
    """Strips characters Excel's file format cannot store at all. Applied
    to every value written to any cell, in every export function —
    scraped text is scraped text, any field could carry one of these."""
    if isinstance(value, str):
        return _ILLEGAL_EXCEL_CHARACTERS_RE.sub("", value)
    return value


def _sanitize_row(row: list) -> list:
    return [_sanitize_for_excel(v) for v in row]

# The 14 columns from the original project spec, plus "Local File Path"
# (added from web-doc-crawler — not part of the original spec, kept
# because it's genuinely useful; see project plan doc for that correction).
SITE_WORKBOOK_COLUMNS = [
    "Sl No",
    "Category",
    "Subcategory",
    "Document Title (readable)",
    "Original Source URL",
    "Found On Page",
    "Original Filename",
    "New Standardized Filename",
    "Naming Scheme",
    "Target Folder Path",
    "File Type",
    "Language",
    "Document Date (YYYY-MM-DD)",
    "Date Basis / Note",
    "Local File Path",
    "Verification Needed",
    "Download Status",
]

BREAKERS_LOG_COLUMNS = [
    "Timestamp", "Site Tag", "URL", "Tier Attempted",
    "Signal", "Action Taken", "Outcome",
]

MASTER_ROLLUP_COLUMNS = [
    "Site", "Pages Crawled (last run)", "Total Files Known",
    "PDF", "DOC", "DOCX", "XLS", "XLSX", "CSV", "Other File Types",
    "Downloaded", "Failed", "Pending",
    "Crawl Time (min, last run)", "Download Time (min, last run)",
    "First Run At", "Last Run At",
]


# ---------------------------------------------------------------------------
# Connection + schema
# ---------------------------------------------------------------------------

def get_connection(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Concurrent downloads (cross-site parallelism, or bounded download-phase
    # concurrency) mean multiple connections can hit this file at once.
    # busy_timeout makes a writer WAIT for a lock instead of immediately
    # raising "database is locked" — each caller still needs its OWN
    # connection object per thread (SQLite connections aren't shareable
    # across threads), this just makes concurrent access to the same file
    # from separate connections actually work.
    conn.execute("PRAGMA busy_timeout = 30000")
    # WAL mode lets readers and a writer proceed concurrently instead of
    # every write needing an exclusive lock on the whole file -- the
    # default journal mode serializes heavily under the kind of wide
    # parallel-site load (8+ sites at once) that surfaced "database is
    # locked" even with busy_timeout set.
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, col_type: str) -> None:
    """Adds `column` to `table` if it isn't there yet. CREATE TABLE IF NOT
    EXISTS is a no-op on a database that already exists from a prior run —
    this is what actually lets a new column reach an existing crawler.db
    without anyone having to delete and rebuild it."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        except sqlite3.OperationalError as e:
            # Benign race: another site thread's init_db() already added this
            # column between our PRAGMA check and this ALTER TABLE — nothing
            # left to do. Anything else is a real error and must still raise.
            if "duplicate column name" not in str(e):
                raise


def init_db(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Create every table if it doesn't exist yet. Safe to call every run —
    existing rows (and everything discovered in earlier runs) are kept."""
    conn = get_connection(db_path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS documents (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_url             TEXT NOT NULL UNIQUE,
            normalized_identity TEXT NOT NULL,
            site_tag            TEXT NOT NULL,
            found_on            TEXT,
            category            TEXT,
            subcategory         TEXT,
            document_title      TEXT,
            original_filename   TEXT,
            file_type           TEXT,
            language            TEXT,
            document_date       TEXT,
            date_basis_note     TEXT,
            new_filename        TEXT,
            target_folder_path  TEXT,
            local_path          TEXT,
            needs_verification  TEXT,
            naming_scheme       TEXT,
            downloaded          INTEGER DEFAULT 0,
            download_error      TEXT,
            sha256              TEXT,
            downloaded_at       TIMESTAMP,
            discovered_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_documents_site_tag
            ON documents(site_tag);
        CREATE INDEX IF NOT EXISTS idx_documents_identity
            ON documents(site_tag, normalized_identity);

        CREATE TABLE IF NOT EXISTS runs (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            site_tag             TEXT NOT NULL,
            start_url            TEXT,
            mode                 TEXT NOT NULL,
            started_at           TEXT NOT NULL,
            finished_at          TEXT DEFAULT CURRENT_TIMESTAMP,
            pages_visited        INTEGER NOT NULL,
            documents_found      INTEGER NOT NULL,
            documents_new        INTEGER NOT NULL,
            documents_known      INTEGER NOT NULL,
            downloaded_count     INTEGER NOT NULL,
            failed_download_count INTEGER NOT NULL,
            crawl_duration_seconds    REAL,
            download_duration_seconds REAL
        );

        CREATE TABLE IF NOT EXISTS run_file_type_counts (
            run_id    INTEGER NOT NULL REFERENCES runs(id),
            file_type TEXT NOT NULL,
            count     INTEGER NOT NULL,
            PRIMARY KEY (run_id, file_type)
        );

        CREATE TABLE IF NOT EXISTS categories (
            site_tag      TEXT NOT NULL,
            category      TEXT NOT NULL,
            folder_number INTEGER NOT NULL,
            PRIMARY KEY (site_tag, category)
        );

        CREATE TABLE IF NOT EXISTS breakers (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp     TEXT DEFAULT CURRENT_TIMESTAMP,
            site_tag      TEXT NOT NULL,
            url           TEXT,
            tier_attempted TEXT,
            signal        TEXT,
            action_taken  TEXT,
            outcome       TEXT
        );
        """
    )
    _ensure_column(conn, "documents", "naming_scheme", "TEXT")
    _ensure_column(conn, "documents", "download_attempts", "INTEGER DEFAULT 0")
    _ensure_column(conn, "runs", "crawl_duration_seconds", "REAL")
    _ensure_column(conn, "runs", "download_duration_seconds", "REAL")
    conn.commit()
    return conn


# A document that has failed this many times stops being automatically
# retried on every future --mode download / download-only run. Without
# this, a genuinely permanently-broken URL (a real 404, a page that will
# never come back) gets re-attempted forever, on every single future run,
# accumulating wasted time indefinitely as a site's database of known-
# broken URLs grows. This does NOT distinguish transient vs. permanent
# errors — it's a simple attempt cap, deliberately, rather than trying to
# classify error types (which risks misfiring in either direction).
MAX_DOWNLOAD_ATTEMPTS = 5


# ---------------------------------------------------------------------------
# URL identity / dedup (web-doc-crawler's pattern)
# ---------------------------------------------------------------------------

def normalize_url_identity(url: str) -> str:
    """Same real resource can appear as http/https and www/non-www variants
    — normalize those away so they're recognized as the same document.
    The PATH is also lowercased: many government sites (especially
    IIS/Windows-hosted ones, common Indian government infrastructure)
    serve files case-insensitively, so /orders/Report.pdf and
    /orders/report.pdf are typically the exact same file, not two
    different ones — without this, both would be inserted as separate
    documents and potentially downloaded twice.

    The QUERY STRING's case is deliberately left untouched — a
    case-sensitive token there (e.g. a base64-ish id) can be a genuinely
    meaningful distinction on some systems, unlike a path segment.

    Deliberately KEEPS the query string. Dropping it (an earlier version of
    this function did) is a real bug on any site that serves documents
    through a shared download endpoint differentiated only by query string
    — e.g. /download?id=12345 vs /download?id=67890 are two completely
    different files, but both would collapse to the identical identity
    "host/download" if the query string were stripped, silently merging
    a genuinely new document into an unrelated existing one instead of
    recording it. This was caught from a real run against iexindia.com
    where documents served this way were being under-counted."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.lower()
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{host}{path}{query}"


def get_document(conn: sqlite3.Connection, doc_url: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM documents WHERE doc_url = ?", (doc_url,)
    ).fetchone()
    return dict(row) if row else None


def find_by_identity(conn: sqlite3.Connection, doc_url: str, site_tag: str) -> dict | None:
    """Catches the same document recorded under a different http/https or
    www/non-www variant, which would otherwise be treated as a second,
    separate document despite being identical content."""
    identity = normalize_url_identity(doc_url)
    rows = conn.execute(
        "SELECT * FROM documents WHERE site_tag = ? AND doc_url != ?",
        (site_tag, doc_url),
    ).fetchall()
    for row in rows:
        if row["normalized_identity"] == identity:
            return dict(row)
    return None


# ---------------------------------------------------------------------------
# Category folder numbering (web-doc-crawler's pattern)
# ---------------------------------------------------------------------------

def get_category_folder_number(conn: sqlite3.Connection, site_tag: str, category: str) -> int:
    """Assigns the next free folder number the first time a category is
    seen for this site, then reuses it on every later call/run."""
    row = conn.execute(
        "SELECT folder_number FROM categories WHERE site_tag = ? AND category = ?",
        (site_tag, category),
    ).fetchone()
    if row:
        return row["folder_number"]

    next_number = conn.execute(
        "SELECT COALESCE(MAX(folder_number), 0) + 1 FROM categories WHERE site_tag = ?",
        (site_tag,),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO categories (site_tag, category, folder_number) VALUES (?, ?, ?)",
        (site_tag, category, next_number),
    )
    conn.commit()
    return next_number


# ---------------------------------------------------------------------------
# Filename collision handling (web-doc-crawler's pattern)
# ---------------------------------------------------------------------------

def disambiguate_filename(conn: sqlite3.Connection, new_filename: str, doc_url: str, site_tag: str) -> str:
    """Guarantees new_filename doesn't collide with a different document's
    filename within the same site — two distinct doc_urls can otherwise
    slugify to the same name, which would silently overwrite one file with
    another on disk."""
    collision = conn.execute(
        "SELECT 1 FROM documents WHERE site_tag = ? AND new_filename = ? AND doc_url != ? LIMIT 1",
        (site_tag, new_filename, doc_url),
    ).fetchone()
    if not collision:
        return new_filename

    import hashlib
    suffix = hashlib.sha1(doc_url.encode("utf-8")).hexdigest()[:8].upper()
    name_part, _, extension = new_filename.rpartition(".")
    if not name_part:
        name_part, extension = new_filename, ""
    disambiguated = f"{name_part}-{suffix}"
    return f"{disambiguated}.{extension}" if extension else disambiguated


# ---------------------------------------------------------------------------
# Document insert / update
# ---------------------------------------------------------------------------

def insert_document(conn: sqlite3.Connection, record: dict) -> None:
    """record must contain at least: doc_url, site_tag. Everything else is
    optional and defaults to NULL."""
    record = dict(record)
    record.setdefault("normalized_identity", normalize_url_identity(record["doc_url"]))
    columns = [
        "doc_url", "normalized_identity", "site_tag", "found_on", "category",
        "subcategory", "document_title", "original_filename", "file_type",
        "language", "document_date", "date_basis_note", "new_filename",
        "target_folder_path", "needs_verification", "naming_scheme",
    ]
    values = [record.get(c) for c in columns]
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT OR IGNORE INTO documents ({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )
    conn.commit()


def update_document_fields(conn: sqlite3.Connection, doc_url: str, fields: dict) -> None:
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE documents SET {set_clause} WHERE doc_url = ?",
        [*fields.values(), doc_url],
    )
    conn.commit()


def mark_downloaded(conn: sqlite3.Connection, doc_url: str, success: bool,
                     error: str | None, local_path: str | None, sha256: str | None = None) -> None:
    conn.execute(
        """
        UPDATE documents
        SET downloaded = ?, download_error = ?, local_path = ?, sha256 = ?,
            downloaded_at = CURRENT_TIMESTAMP,
            download_attempts = COALESCE(download_attempts, 0) + 1
        WHERE doc_url = ?
        """,
        (1 if success else 0, None if success else error,
         local_path if success else None, sha256 if success else None, doc_url),
    )
    conn.commit()


def get_document_type_counts(conn: sqlite3.Connection, site_tag: str) -> dict:
    rows = conn.execute(
        "SELECT file_type, COUNT(*) as c FROM documents WHERE site_tag = ? GROUP BY file_type",
        (site_tag,),
    ).fetchall()
    return {row["file_type"]: row["c"] for row in rows}


def get_total_document_count(conn: sqlite3.Connection, site_tag: str) -> int:
    """Total documents on file for a site, across every run ever done for
    it — not just what this particular run newly discovered."""
    return conn.execute(
        "SELECT COUNT(*) FROM documents WHERE site_tag = ?", (site_tag,)
    ).fetchone()[0]


def get_download_status_counts(conn: sqlite3.Connection, site_tag: str) -> dict:
    """Cumulative download status for a site across EVERY run ever done,
    not just the current one — this is what a summary should report,
    since 'this run' can look like nothing happened when most documents
    were already downloaded in an earlier run and only a few stragglers
    were still pending."""
    downloaded = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE site_tag = ? AND downloaded = 1", (site_tag,)
    ).fetchone()[0]
    failed = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE site_tag = ? AND downloaded = 0 AND download_error IS NOT NULL",
        (site_tag,),
    ).fetchone()[0]
    pending = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE site_tag = ? AND downloaded = 0 AND download_error IS NULL",
        (site_tag,),
    ).fetchone()[0]
    return {"downloaded": downloaded, "failed": failed, "pending": pending}


def get_failed_documents(conn: sqlite3.Connection, site_tag: str) -> list[dict]:
    """Every document that failed to download and is still failed as of
    right now — used to surface a explicit failure list in the console at
    the end of a run, on top of the highlighted rows in the workbook."""
    rows = conn.execute(
        "SELECT doc_url, new_filename, download_error FROM documents "
        "WHERE site_tag = ? AND downloaded = 0 AND download_error IS NOT NULL "
        "ORDER BY id",
        (site_tag,),
    ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Run history
# ---------------------------------------------------------------------------

def record_run(conn: sqlite3.Connection, site_tag: str, start_url: str, mode: str,
                started_at: str, pages_visited: int, documents_found: int,
                documents_new: int, documents_known: int, downloaded_count: int,
                failed_download_count: int, file_type_counts: dict,
                crawl_duration_seconds: float | None = None,
                download_duration_seconds: float | None = None) -> int:
    cursor = conn.execute(
        """
        INSERT INTO runs (
            site_tag, start_url, mode, started_at, pages_visited,
            documents_found, documents_new, documents_known,
            downloaded_count, failed_download_count,
            crawl_duration_seconds, download_duration_seconds
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (site_tag, start_url, mode, started_at, pages_visited, documents_found,
         documents_new, documents_known, downloaded_count, failed_download_count,
         crawl_duration_seconds, download_duration_seconds),
    )
    run_id = cursor.lastrowid
    for file_type, count in file_type_counts.items():
        conn.execute(
            "INSERT INTO run_file_type_counts (run_id, file_type, count) VALUES (?, ?, ?)",
            (run_id, file_type, count),
        )
    conn.commit()
    return run_id


def get_run_history(conn: sqlite3.Connection, site_tag: str | None = None) -> list[dict]:
    if site_tag:
        rows = conn.execute(
            "SELECT * FROM runs WHERE site_tag = ? ORDER BY id DESC", (site_tag,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM runs ORDER BY id DESC").fetchall()

    runs = [dict(row) for row in rows]
    for run in runs:
        type_rows = conn.execute(
            "SELECT file_type, count FROM run_file_type_counts WHERE run_id = ? ORDER BY count DESC",
            (run["id"],),
        ).fetchall()
        run["file_type_counts"] = {r["file_type"]: r["count"] for r in type_rows}
    return runs


def get_latest_run(conn: sqlite3.Connection, site_tag: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM runs WHERE site_tag = ? ORDER BY id DESC LIMIT 1", (site_tag,)
    ).fetchone()
    return dict(row) if row else None


def get_first_run(conn: sqlite3.Connection, site_tag: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM runs WHERE site_tag = ? ORDER BY id ASC LIMIT 1", (site_tag,)
    ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Breakers log (live, append-only in effect)
# ---------------------------------------------------------------------------

def log_breaker(conn: sqlite3.Connection, site_tag: str, url: str, tier_attempted: str,
                 signal: str, action_taken: str, outcome: str) -> None:
    conn.execute(
        """
        INSERT INTO breakers (site_tag, url, tier_attempted, signal, action_taken, outcome)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (site_tag, url, tier_attempted, signal, action_taken, outcome),
    )
    conn.commit()


def get_breakers(conn: sqlite3.Connection, site_tag: str | None = None) -> list[dict]:
    if site_tag:
        rows = conn.execute(
            "SELECT * FROM breakers WHERE site_tag = ? ORDER BY id", (site_tag,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM breakers ORDER BY id").fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Change diffing (multisite's NEW/CHANGED/REMOVED/UNCHANGED pattern,
# keyed off the DB instead of a manifest.json file)
# ---------------------------------------------------------------------------

def get_all_shas(conn: sqlite3.Connection, site_tag: str) -> dict:
    """Snapshot of doc_url -> sha256 for every currently-downloaded document
    of a site. Call this BEFORE a download run starts, so it reflects the
    prior run's state, not the one about to happen."""
    rows = conn.execute(
        "SELECT doc_url, sha256 FROM documents WHERE site_tag = ? AND downloaded = 1",
        (site_tag,),
    ).fetchall()
    return {row["doc_url"]: row["sha256"] for row in rows}


def diff_shas(previous: dict, current: dict) -> list[tuple[str, str]]:
    changes = []
    for url, sha in current.items():
        if url not in previous:
            changes.append((url, "NEW"))
        elif previous[url] != sha:
            changes.append((url, "CHANGED / REPLACED"))
        else:
            changes.append((url, "UNCHANGED"))
    for url in previous:
        if url not in current:
            changes.append((url, "REMOVED / MISSING"))
    return changes


# ---------------------------------------------------------------------------
# Excel export #1 — per-site workbook
# ---------------------------------------------------------------------------

def export_site_workbook(conn: sqlite3.Connection, site_tag: str,
                          change_log: list[tuple[str, str]] | None = None,
                          output_path: str | Path | None = None) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import PatternFill

    output_path = Path(output_path or f"{site_tag.upper()}_documents.xlsx")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    rows = conn.execute(
        """
        SELECT doc_url, found_on, original_filename, new_filename, target_folder_path, file_type,
               category, subcategory, document_title, language, document_date,
               date_basis_note, local_path, needs_verification, naming_scheme,
               downloaded, download_error, download_attempts
        FROM documents WHERE site_tag = ? ORDER BY id
        """,
        (site_tag,),
    ).fetchall()

    wb = Workbook()
    sheet = wb.active
    sheet.title = "File_Tracking"
    sheet.append(SITE_WORKBOOK_COLUMNS)
    sheet.freeze_panes = "A2"

    # "Not able to download" rows are highlighted so they're visible at a
    # glance, not just findable by reading the Download Status text.
    failed_fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")

    for index, row in enumerate(rows, start=1):
        failed = bool(row["download_error"]) and not row["downloaded"]
        if row["downloaded"]:
            status = "Downloaded"
        elif row["download_error"]:
            attempts = row["download_attempts"] or 0
            if attempts >= MAX_DOWNLOAD_ATTEMPTS:
                status = f"NOT DOWNLOADED (gave up after {attempts} attempts) — {row['download_error']}"
            else:
                status = f"NOT DOWNLOADED — {row['download_error']}"
        else:
            status = "Pending"

        sheet.append(_sanitize_row([
            index, row["category"], row["subcategory"], row["document_title"],
            row["doc_url"], row["found_on"] or "", row["original_filename"], row["new_filename"],
            row["naming_scheme"] or "GENERIC", row["target_folder_path"],
            (row["file_type"] or "").upper(), row["language"], row["document_date"] or "",
            row["date_basis_note"], row["local_path"] or "", row["needs_verification"], status,
        ]))

        if failed:
            excel_row = sheet.max_row
            for col in range(1, len(SITE_WORKBOOK_COLUMNS) + 1):
                sheet.cell(row=excel_row, column=col).fill = failed_fill

    naming = wb.create_sheet("Naming_Convention")
    naming.append(["Field", "Rule"])
    naming.append(["GENERIC pattern (fallback)",
                    f"{site_tag.upper()}__CATEGORY__SUBCATEGORY__DOCUMENT-TITLE__LANG_YYYY-MM-DD.ext"])
    naming.append(["SERC pattern (regulatory docs only)",
                    "<SERC>-<SUBJECT>-P<YYYY>-A<n>[-R<n>of<YYYY>]-<TYPE>[-<LANG>]-<YYYYMMDD>.ext"])
    naming.append(["When SERC pattern applies",
                    "Only when the site is a recognized SERC/CERC code AND a document TYPE AND a "
                    "subject code both match — otherwise the GENERIC pattern is used. See the "
                    "Naming Scheme column in File_Tracking for which one each row got."])
    naming.append(["Amendment number caveat",
                    "SERC-pattern filenames always use A0 — not verified against an official "
                    "regulation history. See the Date Basis / Note column for affected rows."])
    naming.append(["SOURCE", f"Fixed as {site_tag.upper()} for every file from this site's domains."])

    change_sheet = wb.create_sheet("Change_Log")
    change_sheet.append(["Run Timestamp", "Original Source URL", "Status"])
    if change_log:
        ts = datetime.now().isoformat(timespec="seconds")
        for url, status in change_log:
            change_sheet.append(_sanitize_row([ts, url, status]))

    try:
        wb.save(output_path)
    except Exception as e:
        print(f"  [warning] could not save {output_path} — is it open in another program? ({e})")
        log_breaker(conn, site_tag, str(output_path), "n/a", f"workbook save failed: {e}",
                    "site's document data unchanged in DB, only this export skipped", "n/a")
    return output_path


# ---------------------------------------------------------------------------
# Excel export #2 — master rollup across every site
# ---------------------------------------------------------------------------

DEFAULT_MASTER_SUMMARY_PATH = "_MASTER_REPORTS/MASTER_SUMMARY.xlsx"

# --config all runs multiple sites concurrently via a ThreadPoolExecutor in
# ONE process (see crawler.py's run_all_sites) — all of those threads can
# call update_master_summary() around the same moment. Without this lock,
# two threads can both load the file before either saves, and whichever
# saves second silently overwrites the first thread's just-written row
# with its own stale copy of the rest of the file. A single process-wide
# lock fully serializes the read-modify-write cycle; a thread lock is
# sufficient here specifically because every site in one --config all run
# shares this same process (were sites ever split across separate
# processes, this would need a cross-process file lock instead).
_master_summary_lock = threading.Lock()

# Same hazard, same fix, for BREAKERS_LOG.xlsx: every site's thread calls
# export_breakers_log() against the same fixed shared path, and without a
# lock two threads' concurrent wb.save() calls to that one path can
# interleave and corrupt the file (confirmed real incident). A separate
# lock from _master_summary_lock so writing one file never blocks on the
# other.
_breakers_log_lock = threading.Lock()


def _acquire_cross_process_lock(lock_path: Path, timeout: float = 60.0, poll_interval: float = 0.1) -> None:
    """threading.Lock only protects threads inside ONE process -- it does
    nothing across separate OS processes, e.g. several terminal windows
    each running crawler.py against the same MASTER_SUMMARY.xlsx at once
    (a real, confirmed usage pattern). This is a dependency-free
    cross-process mutex using atomic exclusive file creation, which
    fails immediately if another process already holds it, working the
    same way on Windows and elsewhere without needing a new package."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.close(fd)
            return
        except FileExistsError:
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"Could not acquire {lock_path} within {timeout}s. If no other "
                    f"crawler process is actually running, delete this file manually.")
            time.sleep(poll_interval)


def _release_cross_process_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


def _compute_master_row(conn: sqlite3.Connection, site_tag: str) -> list:
    latest_run = get_latest_run(conn, site_tag)
    first_run = get_first_run(conn, site_tag)
    type_counts = get_document_type_counts(conn, site_tag)
    total = sum(type_counts.values())
    named_types = {"pdf", "doc", "docx", "xls", "xlsx", "csv"}
    other_types = sum(count for ext, count in type_counts.items() if ext not in named_types)
    downloaded = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE site_tag = ? AND downloaded = 1", (site_tag,)
    ).fetchone()[0]
    failed = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE site_tag = ? AND download_error IS NOT NULL", (site_tag,)
    ).fetchone()[0]
    pending = total - downloaded - failed

    def _minutes(seconds) -> str | int:
        return round(seconds / 60, 1) if seconds is not None else ""

    crawl_minutes = _minutes(latest_run["crawl_duration_seconds"]) if latest_run else ""
    download_minutes = _minutes(latest_run["download_duration_seconds"]) if latest_run else ""

    return _sanitize_row([
        site_tag,
        latest_run["pages_visited"] if latest_run else 0,
        total,
        type_counts.get("pdf", 0), type_counts.get("doc", 0),
        type_counts.get("docx", 0), type_counts.get("xls", 0),
        type_counts.get("xlsx", 0), type_counts.get("csv", 0),
        other_types,
        downloaded, failed, pending,
        crawl_minutes, download_minutes,
        first_run["started_at"] if first_run else "",
        latest_run["finished_at"] if latest_run else "",
    ])


def export_master_rollup(conn: sqlite3.Connection, site_tags: list[str],
                          output_path: str | Path | None = None) -> Path:
    from openpyxl import Workbook

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = Path(output_path or f"_MASTER_REPORTS/master_summary_{ts}.xlsx")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    sheet = wb.active
    sheet.title = "Master_Rollup"
    sheet.append(MASTER_ROLLUP_COLUMNS)

    for site_tag in site_tags:
        sheet.append(_compute_master_row(conn, site_tag))

    try:
        wb.save(output_path)
    except Exception as e:
        print(f"  [warning] could not save {output_path} — is it open in another program? ({e})")
        log_breaker(conn, "ALL", str(output_path), "n/a", f"workbook save failed: {e}",
                    "master rollup snapshot for this run was not written", "n/a")
    return output_path


def update_master_summary(conn: sqlite3.Connection, site_tag: str,
                           output_path: str | Path | None = None) -> Path:
    """Refreshes ONE site's row in a single persistent file, at the same
    path every time — unlike export_master_rollup()'s timestamped
    snapshots (one new file per --config all run), this file always
    exists at a known location and stays current after ANY run, single-
    site or --config all. Every other site's most recently known row is
    left untouched; only the calling site's row is replaced (or appended,
    if this is the first time this site has run)."""
    from openpyxl import Workbook, load_workbook

    output_path = Path(output_path or DEFAULT_MASTER_SUMMARY_PATH)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output_path.parent / f".{output_path.name}.lock"

    with _master_summary_lock:
        _acquire_cross_process_lock(lock_path)
        try:
            if output_path.exists():
                wb = load_workbook(output_path)
                sheet = wb["Master_Rollup"] if "Master_Rollup" in wb.sheetnames else wb.active
                existing_header = [c.value for c in sheet[1]] if sheet.max_row >= 1 else []
                if existing_header != MASTER_ROLLUP_COLUMNS:
                    # The column layout changed (e.g. a new column added). MUST
                    # migrate every existing row forward by name, never delete
                    # them -- this file is the cross-site run history, not a
                    # disposable cache. A prior version of this code wiped the
                    # whole sheet here, destroying every other site's history;
                    # that was a real bug, fixed by preserving each row's
                    # existing values and only defaulting genuinely new columns.
                    old_rows = []
                    for row_idx in range(2, sheet.max_row + 1):
                        old_rows.append({existing_header[i]: sheet.cell(row=row_idx, column=i + 1).value
                                          for i in range(len(existing_header))})
                    sheet.delete_rows(1, sheet.max_row)
                    sheet.append(MASTER_ROLLUP_COLUMNS)
                    for row_data in old_rows:
                        sheet.append([row_data.get(col, 0) for col in MASTER_ROLLUP_COLUMNS])
                    print(f"  [info] MASTER_SUMMARY.xlsx column layout updated — all "
                          f"{len(old_rows)} existing site row(s) preserved and migrated forward.")
            else:
                wb = Workbook()
                sheet = wb.active
                sheet.title = "Master_Rollup"
                sheet.append(MASTER_ROLLUP_COLUMNS)

            new_row = _compute_master_row(conn, site_tag)

            target_row_index = None
            for row_idx in range(2, sheet.max_row + 1):
                if sheet.cell(row=row_idx, column=1).value == site_tag:
                    target_row_index = row_idx
                    break

            if target_row_index is not None:
                for col_idx, value in enumerate(new_row, start=1):
                    sheet.cell(row=target_row_index, column=col_idx, value=value)
            else:
                sheet.append(new_row)

            try:
                wb.save(output_path)
            except Exception as e:
                print(f"  [warning] could not save {output_path} — is it open in another program? ({e})")
                log_breaker(conn, site_tag, str(output_path), "n/a", f"workbook save failed: {e}",
                            "this site's row was updated in memory but not persisted to MASTER_SUMMARY.xlsx",
                            "n/a")
            return output_path
        finally:
            _release_cross_process_lock(lock_path)


# ---------------------------------------------------------------------------
# Excel export #3 — breakers log (one continuous file, all sites, all runs)
# ---------------------------------------------------------------------------

def export_breakers_log(conn: sqlite3.Connection, output_path: str | Path = "BREAKERS_LOG.xlsx") -> Path:
    from openpyxl import Workbook

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output_path.parent / f".{output_path.name}.lock"

    with _breakers_log_lock:
        _acquire_cross_process_lock(lock_path)
        try:
            breakers = get_breakers(conn)

            wb = Workbook()
            sheet = wb.active
            sheet.title = "Breakers"
            sheet.append(BREAKERS_LOG_COLUMNS)
            sheet.freeze_panes = "A2"

            for entry in breakers:
                sheet.append(_sanitize_row([
                    entry["timestamp"], entry["site_tag"], entry["url"],
                    entry["tier_attempted"], entry["signal"],
                    entry["action_taken"], entry["outcome"],
                ]))

            try:
                wb.save(output_path)
            except Exception as e:
                print(f"  [warning] could not save {output_path} — is it open in another program? ({e})")
                # Can't log this INTO the file that just failed to save --
                # written to the breakers table anyway (that write still
                # succeeds even though this run's Excel export didn't), so
                # it surfaces on the next successful export instead of
                # being lost to terminal scrollback.
                log_breaker(conn, "ALL", str(output_path), "n/a", f"workbook save failed: {e}",
                            "breakers log Excel export skipped this run, data still in DB", "n/a")
            return output_path
        finally:
            _release_cross_process_lock(lock_path)