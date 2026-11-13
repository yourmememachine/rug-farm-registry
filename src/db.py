"""SQLite storage for the Rug-Farm Registry (v2.14 "Pro").

The registry used to live in one JSON file (``data/registry.json``).
JSON is fine for a read-only verdict table, but it cannot dedup
transfers, query funding edges, or record model runs — so the
working data now lives in SQLite (stdlib ``sqlite3``, no new
dependency), while ``data/registry.json`` keeps its role as the
human-readable verdict table the daily scan commits.

Schema (versioned, migrated in place — see ``migrate``):

    mints         one row per watched mint (mint, creator, first seen)
    devs          one row per dev wallet (launches, rugged count, verdict)
    transfers     one row per parsed token transfer; a signature is
                  stored at most once, so re-indexing never duplicates
    funding_edges one row per (from, to, kind) funding link
    outcomes      one current outcome per mint (label + rule version)
    alerts        one alert per (mint, verdict) — never re-alerted
    model_runs    one row per ML training run (metrics, rule version)
    holders       a VIEW over transfers: net token balance per wallet

``init_db`` creates the file and applies every pending migration;
opening an old database upgrades it, never wipes it. Every writer
is idempotent: calling it twice with the same data changes
nothing the second time.

No wallet, no funds, no network in this module — it only stores
what other modules hand it.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "rugfarm.db"

SCHEMA_VERSION = 2

# Migration SQL, applied in order. Each migration is idempotent
# (CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS) so a
# half-finished upgrade can be re-run safely.
MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mints (
            mint TEXT PRIMARY KEY,
            name TEXT,
            symbol TEXT,
            creator TEXT,
            created_at TEXT,
            first_seen TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS devs (
            wallet TEXT PRIMARY KEY,
            launches INTEGER NOT NULL DEFAULT 0,
            rugged_count INTEGER NOT NULL DEFAULT 0,
            verdict TEXT,
            first_seen TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS transfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mint TEXT NOT NULL,
            wallet TEXT NOT NULL,
            side TEXT NOT NULL,
            amount REAL NOT NULL,
            slot INTEGER,
            signature TEXT UNIQUE,
            seen_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS funding_edges (
            from_wallet TEXT NOT NULL,
            to_wallet TEXT NOT NULL,
            amount_lamports INTEGER NOT NULL DEFAULT 0,
            first_seen TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'transfer',
            PRIMARY KEY (from_wallet, to_wallet, kind)
        );
        CREATE TABLE IF NOT EXISTS outcomes (
            mint TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            evidence_json TEXT,
            rule_version TEXT,
            labelled_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mint TEXT NOT NULL,
            dev TEXT,
            verdict TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (mint, verdict)
        );
        CREATE TABLE IF NOT EXISTS model_runs (
            run_id TEXT PRIMARY KEY,
            rule_version TEXT,
            train_n INTEGER NOT NULL DEFAULT 0,
            metrics_json TEXT,
            created_at TEXT NOT NULL
        );
        CREATE VIEW IF NOT EXISTS holders AS
            SELECT mint, wallet,
                   SUM(CASE WHEN side = 'buy' THEN amount
                            WHEN side = 'sell' THEN -amount
                            ELSE 0 END) AS net_amount,
                   COUNT(*) AS transfers
            FROM transfers
            GROUP BY mint, wallet
            HAVING net_amount > 0;
        """,
    ),
    (
        2,
        """
        CREATE INDEX IF NOT EXISTS idx_transfers_mint
            ON transfers (mint);
        CREATE INDEX IF NOT EXISTS idx_transfers_wallet
            ON transfers (wallet);
        CREATE INDEX IF NOT EXISTS idx_funding_edges_from
            ON funding_edges (from_wallet);
        CREATE INDEX IF NOT EXISTS idx_funding_edges_to
            ON funding_edges (to_wallet);
        CREATE INDEX IF NOT EXISTS idx_outcomes_label
            ON outcomes (label);
        CREATE INDEX IF NOT EXISTS idx_alerts_mint
            ON alerts (mint);
        """,
    ),
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection (row dicts, foreign keys on)."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    """Current schema version (0 when the DB has no schema yet)."""
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0  # Fresh database: no schema_meta table yet.
    if row is None:
        return 0
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return 0


def migrate(conn: sqlite3.Connection) -> int:
    """Apply every pending migration, in order. Returns the version."""
    current = schema_version(conn)
    for version, sql in MIGRATIONS:
        if version <= current:
            continue
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(version),),
        )
        conn.commit()
        current = version
    return current


def init_db(path: str | Path) -> sqlite3.Connection:
    """Create (or open and migrate) the database. Returns a connection."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(target)
    migrate(conn)
    return conn


# ======================================================================
# Writers — all idempotent
# ======================================================================
def upsert_mint(
    conn: sqlite3.Connection,
    mint: str,
    name: str | None = None,
    symbol: str | None = None,
    creator: str | None = None,
    created_at: str | None = None,
) -> None:
    """Insert a mint, or fill in the fields that are still empty.

    Existing non-empty values are never overwritten with None, and
    ``first_seen`` is set once and never moved.
    """
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO mints (mint, name, symbol, creator, created_at, first_seen)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(mint) DO UPDATE SET
            name = COALESCE(mints.name, excluded.name),
            symbol = COALESCE(mints.symbol, excluded.symbol),
            creator = COALESCE(mints.creator, excluded.creator),
            created_at = COALESCE(mints.created_at, excluded.created_at)
        """,
        (mint, name, symbol, creator, created_at, now),
    )
    conn.commit()


def upsert_dev(
    conn: sqlite3.Connection,
    wallet: str,
    launches: int | None = None,
    rugged_count: int | None = None,
    verdict: str | None = None,
) -> None:
    """Insert a dev wallet, or update the fields that are given.

    Counts are absolute values when given (the caller owns the
    counting); None means "keep what is stored".
    """
    now = _now_iso()
    # Two steps, so "not given" (None) can never overwrite a stored
    # count: insert the row if it is new (counts start at 0), then
    # update only the fields the caller actually passed.
    conn.execute(
        """
        INSERT OR IGNORE INTO devs
            (wallet, launches, rugged_count, verdict, first_seen, updated_at)
        VALUES (?, 0, 0, NULL, ?, ?)
        """,
        (wallet, now, now),
    )
    sets = ["updated_at = ?"]
    params: list = [now]
    if launches is not None:
        sets.append("launches = ?")
        params.append(launches)
    if rugged_count is not None:
        sets.append("rugged_count = ?")
        params.append(rugged_count)
    if verdict is not None:
        sets.append("verdict = ?")
        params.append(verdict)
    conn.execute(
        f"UPDATE devs SET {', '.join(sets)} WHERE wallet = ?",
        (*params, wallet),
    )
    conn.commit()


def record_transfer(
    conn: sqlite3.Connection,
    mint: str,
    wallet: str,
    side: str,
    amount: float,
    slot: int | None = None,
    signature: str | None = None,
) -> bool:
    """Record one token transfer. Returns True when it was new.

    A transfer with a signature is stored at most once — re-indexing
    the same transaction returns False and changes nothing. A
    transfer without a signature is always recorded (no dedup key).
    """
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")
    if amount < 0:
        raise ValueError("amount must not be negative")
    if signature is not None:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO transfers
                (mint, wallet, side, amount, slot, signature, seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (mint, wallet, side, amount, slot, signature, _now_iso()),
        )
        conn.commit()
        return cur.rowcount == 1
    conn.execute(
        """
        INSERT INTO transfers (mint, wallet, side, amount, slot, signature, seen_at)
        VALUES (?, ?, ?, ?, ?, NULL, ?)
        """,
        (mint, wallet, side, amount, slot, _now_iso()),
    )
    conn.commit()
    return True


def record_funding_edge(
    conn: sqlite3.Connection,
    from_wallet: str,
    to_wallet: str,
    amount_lamports: int,
    kind: str = "transfer",
) -> bool:
    """Record a funding link. Returns True when it was new.

    One row per (from, to, kind): the first amount seen is kept,
    so re-indexing never inflates the graph.
    """
    if from_wallet == to_wallet:
        return False
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO funding_edges
            (from_wallet, to_wallet, amount_lamports, first_seen, kind)
        VALUES (?, ?, ?, ?, ?)
        """,
        (from_wallet, to_wallet, amount_lamports, _now_iso(), kind),
    )
    conn.commit()
    return cur.rowcount == 1


def record_outcome(
    conn: sqlite3.Connection,
    mint: str,
    label: str,
    evidence_json: str | None = None,
    rule_version: str | None = None,
) -> None:
    """Record (or replace) the current outcome for a mint."""
    if label not in ("rugged", "failed_but_honest", "inconclusive", "survived"):
        raise ValueError(f"unknown outcome label {label!r}")
    conn.execute(
        """
        INSERT INTO outcomes (mint, label, evidence_json, rule_version, labelled_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(mint) DO UPDATE SET
            label = excluded.label,
            evidence_json = excluded.evidence_json,
            rule_version = excluded.rule_version,
            labelled_at = excluded.labelled_at
        """,
        (mint, label, evidence_json, rule_version, _now_iso()),
    )
    conn.commit()


def record_alert(
    conn: sqlite3.Connection,
    mint: str,
    dev: str | None,
    verdict: str,
) -> bool:
    """Record an alert. Returns True when it was new.

    One alert per (mint, verdict): a second alert for the same
    mint and verdict is ignored — the watcher must never re-alert
    the same mint.
    """
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO alerts (mint, dev, verdict, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (mint, dev, verdict, _now_iso()),
    )
    conn.commit()
    return cur.rowcount == 1


def record_model_run(
    conn: sqlite3.Connection,
    run_id: str,
    rule_version: str | None,
    train_n: int,
    metrics_json: str | None = None,
) -> None:
    """Record (or replace) one ML training run, keyed by run_id."""
    conn.execute(
        """
        INSERT INTO model_runs (run_id, rule_version, train_n, metrics_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            rule_version = excluded.rule_version,
            train_n = excluded.train_n,
            metrics_json = excluded.metrics_json,
            created_at = excluded.created_at
        """,
        (run_id, rule_version, train_n, metrics_json, _now_iso()),
    )
    conn.commit()


# ======================================================================
# Readers
# ======================================================================
def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def get_mint(conn: sqlite3.Connection, mint: str) -> dict | None:
    return _row_to_dict(
        conn.execute("SELECT * FROM mints WHERE mint = ?", (mint,)).fetchone()
    )


def get_dev(conn: sqlite3.Connection, wallet: str) -> dict | None:
    return _row_to_dict(
        conn.execute("SELECT * FROM devs WHERE wallet = ?", (wallet,)).fetchone()
    )


def get_outcome(conn: sqlite3.Connection, mint: str) -> dict | None:
    return _row_to_dict(
        conn.execute(
            "SELECT * FROM outcomes WHERE mint = ?", (mint,)
        ).fetchone()
    )


def has_alert(
    conn: sqlite3.Connection, mint: str, verdict: str | None = None
) -> bool:
    """True when an alert already exists for this mint (and verdict)."""
    if verdict is None:
        row = conn.execute(
            "SELECT 1 FROM alerts WHERE mint = ? LIMIT 1", (mint,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM alerts WHERE mint = ? AND verdict = ? LIMIT 1",
            (mint, verdict),
        ).fetchone()
    return row is not None


def list_holders(conn: sqlite3.Connection, mint: str) -> list[dict]:
    """Wallets holding a net positive amount of a mint, largest first."""
    rows = conn.execute(
        "SELECT mint, wallet, net_amount, transfers FROM holders"
        " WHERE mint = ? ORDER BY net_amount DESC",
        (mint,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_flagged_devs(conn: sqlite3.Connection) -> list[dict]:
    """Devs whose stored verdict is 'flagged'."""
    rows = conn.execute(
        "SELECT * FROM devs WHERE verdict = 'flagged' ORDER BY wallet"
    ).fetchall()
    return [dict(r) for r in rows]


def list_alerts(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM alerts ORDER BY created_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_model_runs(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM model_runs ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def count_table(conn: sqlite3.Connection, table: str) -> int:
    """Row count for a known table (table name is never user input)."""
    allowed = {
        "mints",
        "devs",
        "transfers",
        "funding_edges",
        "outcomes",
        "alerts",
        "model_runs",
    }
    if table not in allowed:
        raise ValueError(f"unknown table {table!r}")
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return int(row["n"])
