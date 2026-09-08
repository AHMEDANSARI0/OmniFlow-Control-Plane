-- 011: follow-up agent (settings + queue)
-- Applied automatically by portal_db.ensure_tables(); kept here as the
-- canonical migration record (001-011).
CREATE TABLE IF NOT EXISTS portal_followup_settings (
  client_id BIGINT PRIMARY KEY,
  enabled BOOLEAN NOT NULL DEFAULT FALSE,
  delay_hours INT NOT NULL DEFAULT 24,
  max_attempts INT NOT NULL DEFAULT 2,
  message_template TEXT NOT NULL DEFAULT '',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS portal_followups (
  id BIGSERIAL PRIMARY KEY,
  client_id BIGINT NOT NULL,
  conversation_id BIGINT NOT NULL UNIQUE,
  contact_id TEXT NOT NULL,
  contact_name TEXT,
  stage TEXT NOT NULL DEFAULT 'waiting',
  intent TEXT,
  attempts INT NOT NULL DEFAULT 0,
  last_note TEXT,
  next_due_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_portal_followups_due
  ON portal_followups (client_id, stage, next_due_at);
