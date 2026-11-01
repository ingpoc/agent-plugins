ALTER TABLE trials ADD COLUMN IF NOT EXISTS device_pubkey text;
-- statement-break
CREATE TABLE IF NOT EXISTS device_keys (
  device_pubkey text PRIMARY KEY,
  device_id text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
-- statement-break
WITH seen_keys AS (
  SELECT device_pubkey, device_id, created_at FROM claims
  UNION ALL
  SELECT device_pubkey, device_id, bound_at FROM bindings
), encoded_keys AS (
  SELECT device_id, created_at,
    regexp_replace(split_part(translate(device_pubkey, '-_', '+/'), '=', 1), '[^A-Za-z0-9+/]', '', 'g') AS key_text
  FROM seen_keys
), normalized_keys AS (
  SELECT device_id, created_at,
    replace(encode(decode(key_text || repeat('=', (4 - length(key_text) % 4) % 4), 'base64'), 'base64'), E'\n', '') AS device_pubkey
  FROM encoded_keys
)
INSERT INTO device_keys (device_pubkey, device_id, created_at)
SELECT DISTINCT ON (device_pubkey) device_pubkey, device_id, created_at
FROM normalized_keys ORDER BY device_pubkey, created_at, device_id
ON CONFLICT (device_pubkey) DO NOTHING;
-- statement-break
WITH encoded_keys AS (
  SELECT t.id, t.created_at,
    regexp_replace(split_part(translate(c.device_pubkey, '-_', '+/'), '=', 1), '[^A-Za-z0-9+/]', '', 'g') AS key_text
  FROM trials t JOIN claims c ON c.id = t.claim_id
), normalized_keys AS (
  SELECT id, created_at,
    replace(encode(decode(key_text || repeat('=', (4 - length(key_text) % 4) % 4), 'base64'), 'base64'), E'\n', '') AS device_pubkey
  FROM encoded_keys
), keys AS (
  SELECT id, device_pubkey,
    row_number() OVER (PARTITION BY device_pubkey ORDER BY created_at, id) AS ordinal
  FROM normalized_keys
)
UPDATE trials t SET device_pubkey = keys.device_pubkey FROM keys
WHERE t.id = keys.id AND keys.ordinal = 1 AND t.device_pubkey IS NULL
  AND NOT EXISTS (SELECT 1 FROM trials assigned WHERE assigned.device_pubkey = keys.device_pubkey);
-- statement-break
CREATE UNIQUE INDEX IF NOT EXISTS trials_device_pubkey_unique ON trials (device_pubkey) WHERE device_pubkey IS NOT NULL;
-- statement-break
CREATE TABLE IF NOT EXISTS x_identities (
  x_user_id text PRIMARY KEY,
  handle text NOT NULL UNIQUE
);
-- statement-break
INSERT INTO x_identities (x_user_id, handle)
SELECT x_user_id, handle FROM (
  SELECT x_user_id, handle FROM accounts WHERE provider = 'x'
  UNION
  SELECT x_user_id, handle FROM followers WHERE provider = 'x'
) AS seen_identities
WHERE x_user_id ~ '^[0-9]{1,25}$' AND handle ~ '^[a-z0-9_]{1,15}$'
ORDER BY x_user_id, handle
ON CONFLICT DO NOTHING;
