import sqlite3
from pathlib import Path
import config as _config
from config import BASE_DIR


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(_config.DB_PATH)  # read at call time — respects runtime dir changes
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    schema_path = BASE_DIR / "db" / "schema.sql"
    with get_connection() as conn:
        conn.executescript(schema_path.read_text())
    migrate_db()


def migrate_db():
    """
    Apply incremental schema migrations that cannot be expressed in schema.sql
    (CREATE TABLE IF NOT EXISTS never adds new columns to existing tables).
    Safe to call repeatedly — each ALTER is wrapped in try/except.
    """
    new_columns = [
        ("variants",  "TEXT"),   # JSON: {normal,reverse,holo,firstEdition,wPromo}
        ("set_total", "TEXT"),   # total cards in set, e.g. "102"
    ]
    with get_connection() as conn:
        for col, col_type in new_columns:
            try:
                conn.execute(f"ALTER TABLE cards ADD COLUMN {col} {col_type}")
            except sqlite3.OperationalError:
                pass  # column already exists — normal on subsequent startups


def card_count() -> int:
    with get_connection() as conn:
        row = conn.execute("SELECT COUNT(*) FROM cards").fetchone()
        return row[0]


def hash_count() -> int:
    with get_connection() as conn:
        row = conn.execute("SELECT COUNT(DISTINCT card_id) FROM card_hashes").fetchone()
        return row[0]


def upsert_card(card: dict):
    sql = """
        INSERT INTO cards (id, name, set_id, set_name, series, number, rarity, category, hp, types, image_url, local_image_path)
        VALUES (:id, :name, :set_id, :set_name, :series, :number, :rarity, :category, :hp, :types, :image_url, :local_image_path)
        ON CONFLICT(id) DO UPDATE SET
            local_image_path = excluded.local_image_path,
            image_url = excluded.image_url
    """
    with get_connection() as conn:
        conn.execute(sql, card)


def upsert_cards_batch(cards: list[dict]):
    sql = """
        INSERT INTO cards (id, name, set_id, set_name, series, number, rarity, category, hp, types, image_url, local_image_path)
        VALUES (:id, :name, :set_id, :set_name, :series, :number, :rarity, :category, :hp, :types, :image_url, :local_image_path)
        ON CONFLICT(id) DO UPDATE SET
            name             = excluded.name,
            set_id           = excluded.set_id,
            set_name         = excluded.set_name,
            series           = excluded.series,
            number           = excluded.number,
            rarity           = excluded.rarity,
            category         = excluded.category,
            hp               = excluded.hp,
            types            = excluded.types,
            image_url        = COALESCE(excluded.image_url, cards.image_url),
            local_image_path = COALESCE(excluded.local_image_path, cards.local_image_path)
    """
    with get_connection() as conn:
        conn.executemany(sql, cards)


def upsert_hashes_batch(hashes: list[dict]):
    sql = """
        INSERT OR REPLACE INTO card_hashes (card_id, hash_type, hash_value)
        VALUES (:card_id, :hash_type, :hash_value)
    """
    with get_connection() as conn:
        conn.executemany(sql, hashes)


def get_cards_without_images() -> list[sqlite3.Row]:
    """Return all cards that have no local image file yet, regardless of whether image_url is set."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT id, image_url FROM cards WHERE local_image_path IS NULL"
        ).fetchall()


def get_cards_without_image_url() -> list[sqlite3.Row]:
    """Return cards that have no image_url — these need a full individual API fetch to recover one."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT id FROM cards WHERE image_url IS NULL"
        ).fetchall()


def update_image_url(card_id: str, url: str):
    with get_connection() as conn:
        conn.execute(
            "UPDATE cards SET image_url = ? WHERE id = ?",
            (url, card_id)
        )


def clear_all_hashes():
    """Delete all rows from card_hashes (used when hash parameters change and a full rehash is needed)."""
    with get_connection() as conn:
        conn.execute("DELETE FROM card_hashes")


def get_cards_without_hashes() -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute("""
            SELECT c.id, c.local_image_path
            FROM cards c
            LEFT JOIN card_hashes h ON c.id = h.card_id
            WHERE c.local_image_path IS NOT NULL AND h.card_id IS NULL
        """).fetchall()


def _excluded_set_prefix_clause(card_id_col: str = "card_id") -> tuple[str, list]:
    """Return a SQL WHERE fragment and params that exclude configured set prefixes.

    Uses the card id prefix as a proxy for set_id (e.g. 'A1-001' → prefix 'A').
    Returns ("", []) when EXCLUDED_SET_ID_PREFIXES is empty (no filtering).
    """
    prefixes = getattr(_config, "EXCLUDED_SET_ID_PREFIXES", [])
    if not prefixes:
        return "", []
    clauses = " AND ".join(f"{card_id_col} NOT LIKE ?" for _ in prefixes)
    params = [f"{p}%" for p in prefixes]
    return f"AND ({clauses})", params


def get_all_hashes(hash_type: str) -> list[sqlite3.Row]:
    excl_clause, excl_params = _excluded_set_prefix_clause("card_id")
    with get_connection() as conn:
        return conn.execute(
            f"SELECT card_id, hash_value FROM card_hashes WHERE hash_type = ? {excl_clause}",
            [hash_type] + excl_params,
        ).fetchall()


def get_card_by_id(card_id: str) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()


def update_local_image_path(card_id: str, path: str):
    with get_connection() as conn:
        conn.execute(
            "UPDATE cards SET local_image_path = ? WHERE id = ?",
            (path, card_id)
        )


# ── ML Embedding functions ────────────────────────────────────────────────────

def embedding_count() -> int:
    """Return the number of cards with a stored embedding."""
    with get_connection() as conn:
        row = conn.execute("SELECT COUNT(*) FROM card_embeddings").fetchone()
        return row[0]


def get_cards_without_embeddings() -> list[sqlite3.Row]:
    """Return cards that have a local image but no stored embedding yet."""
    with get_connection() as conn:
        return conn.execute("""
            SELECT c.id, c.local_image_path
            FROM cards c
            LEFT JOIN card_embeddings e ON c.id = e.card_id
            WHERE c.local_image_path IS NOT NULL AND e.card_id IS NULL
        """).fetchall()


def upsert_embeddings_batch(embeddings: list[dict]) -> None:
    """
    Insert or replace embeddings for a batch of cards.
    Each dict must have keys: card_id (str), embedding_bytes (bytes).
    """
    sql = """
        INSERT OR REPLACE INTO card_embeddings (card_id, embedding)
        VALUES (:card_id, :embedding_bytes)
    """
    with get_connection() as conn:
        conn.executemany(sql, embeddings)


def get_all_embeddings() -> list[sqlite3.Row]:
    """Return all (card_id, embedding) rows for building the FAISS index."""
    excl_clause, excl_params = _excluded_set_prefix_clause("card_id")
    with get_connection() as conn:
        return conn.execute(
            f"SELECT card_id, embedding FROM card_embeddings WHERE 1=1 {excl_clause}",
            excl_params,
        ).fetchall()


def clear_all_embeddings() -> None:
    """Delete all rows from card_embeddings (used before a full re-embed)."""
    with get_connection() as conn:
        conn.execute("DELETE FROM card_embeddings")


def get_all_cards() -> list[sqlite3.Row]:
    """Return all cards ordered by name — used by the batch search dialog."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT id, name, set_id, set_name, number, rarity, category, hp, types, "
            "image_url, local_image_path, variants, set_total "
            "FROM cards ORDER BY name"
        ).fetchall()


def update_card_details(card_id: str, variants_json: str | None, set_total: str | None,
                        types_json: str | None = None) -> None:
    """Store enriched variant, set-total, and types data fetched from the full TCGdex Card object."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE cards SET variants = ?, set_total = ?, types = COALESCE(?, types) WHERE id = ?",
            (variants_json, set_total, types_json, card_id),
        )


def update_card_full_metadata(card_id: str, set_name: str | None, rarity: str | None,
                               category: str | None, hp: str | None,
                               variants_json: str | None, set_total: str | None,
                               types_json: str | None = None) -> None:
    """Update all metadata fields that are unavailable from the list endpoint."""
    with get_connection() as conn:
        conn.execute(
            """UPDATE cards
               SET set_name  = COALESCE(?, set_name),
                   rarity    = COALESCE(?, rarity),
                   category  = COALESCE(?, category),
                   hp        = COALESCE(?, hp),
                   variants  = COALESCE(?, variants),
                   set_total = COALESCE(?, set_total),
                   types     = COALESCE(?, types)
               WHERE id = ?""",
            (set_name, rarity, category, hp, variants_json, set_total, types_json, card_id),
        )


def get_cards_without_set_name() -> list[sqlite3.Row]:
    """Return all cards that still have a NULL set_name (need full metadata backfill)."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT id FROM cards WHERE set_name IS NULL"
        ).fetchall()
