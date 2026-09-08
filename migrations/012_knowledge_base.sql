-- 012: knowledge base (settings + entries)
-- Applied automatically by portal_db.ensure_tables(); kept here as the
-- canonical migration record (001-012).
CREATE TABLE IF NOT EXISTS portal_kb_settings (
  client_id BIGINT PRIMARY KEY,
  auto_reply BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS portal_kb_entries (
  id BIGSERIAL PRIMARY KEY,
  client_id BIGINT NOT NULL,
  title TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT 'general',
  keywords TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL,
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  usage_count INT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_portal_kb_entries
  ON portal_kb_entries (client_id, is_active, updated_at DESC);
