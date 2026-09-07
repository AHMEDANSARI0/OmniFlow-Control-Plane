-- 010: intent tags for analytics, follow-ups and lead scoring
-- Applied automatically by portal_db.ensure_tables(); kept here as the
-- canonical migration record (001-010).
ALTER TABLE portal_messages
    ADD COLUMN IF NOT EXISTS intent TEXT;
ALTER TABLE portal_conversations
    ADD COLUMN IF NOT EXISTS last_intent TEXT;
