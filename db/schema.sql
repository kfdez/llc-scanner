CREATE TABLE IF NOT EXISTS cards (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    set_id TEXT,
    set_name TEXT,
    series TEXT,
    number TEXT,
    rarity TEXT,
    category TEXT,
    hp TEXT,
    types TEXT,
    image_url TEXT,
    local_image_path TEXT
);

CREATE TABLE IF NOT EXISTS card_hashes (
    card_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    hash_type TEXT NOT NULL,
    hash_value TEXT NOT NULL,
    PRIMARY KEY (card_id, hash_type)
);

CREATE INDEX IF NOT EXISTS idx_card_hashes_type ON card_hashes(hash_type);

CREATE TABLE IF NOT EXISTS card_embeddings (
    card_id   TEXT PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE,
    embedding BLOB NOT NULL  -- float32 numpy tobytes(), shape (1280,) = 5120 bytes/row
);
