ALTER TABLE owner_tokens ADD COLUMN IF NOT EXISTS refresh_ciphertext text;
-- statement-break
ALTER TABLE owner_tokens ADD COLUMN IF NOT EXISTS refresh_nonce text;
-- statement-break
ALTER TABLE owner_tokens ADD COLUMN IF NOT EXISTS refresh_tag text;
-- statement-break
ALTER TABLE owner_tokens ADD COLUMN IF NOT EXISTS access_expires_at timestamptz;
-- statement-break
UPDATE owner_tokens SET access_expires_at = now() + interval '2 hours' WHERE access_expires_at IS NULL;
-- statement-break
ALTER TABLE owner_tokens ALTER COLUMN access_expires_at SET DEFAULT (now() + interval '2 hours');
-- statement-break
ALTER TABLE owner_tokens ALTER COLUMN access_expires_at SET NOT NULL;
-- statement-break
ALTER TABLE owner_tokens ADD COLUMN IF NOT EXISTS owner_auth_required boolean NOT NULL DEFAULT false;
