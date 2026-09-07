-- 009: conversations read tracking (unread badges)
-- Applied automatically by portal_db.ensure_tables(); kept here as the
-- canonical migration record (001-009).
ALTER TABLE portal_conversations
    ADD COLUMN IF NOT EXISTS last_read_at TIMESTAMPTZ;
