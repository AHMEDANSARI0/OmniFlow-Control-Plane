-- 013: business core (catalog, action audit log)
-- Applied automatically by portal_db.ensure_tables(); kept here as the
-- canonical migration record (001-013).
CREATE TABLE IF NOT EXISTS portal_catalog (
  id BIGSERIAL PRIMARY KEY,
  client_id BIGINT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'product',
  name TEXT NOT NULL,
  price_text TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_portal_catalog
  ON portal_catalog (client_id, kind, is_active);
CREATE TABLE IF NOT EXISTS portal_action_log (
  id BIGSERIAL PRIMARY KEY,
  client_id BIGINT NOT NULL,
  action TEXT NOT NULL,
  actor_kind TEXT NOT NULL DEFAULT 'customer_user',
  actor_user_id BIGINT,
  conversation_id BIGINT,
  note TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_portal_action_log
  ON portal_action_log (client_id, id DESC);
