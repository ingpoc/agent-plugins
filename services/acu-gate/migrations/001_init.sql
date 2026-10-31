CREATE TABLE IF NOT EXISTS accounts (
  id uuid PRIMARY KEY,
  provider text NOT NULL CHECK (provider IN ('x', 'linkedin')),
  handle text NOT NULL,
  x_user_id text,
  verified boolean NOT NULL DEFAULT false,
  verified_type text,
  followers_count bigint,
  avatar_url text,
  profile_url text,
  wall_opt_in boolean NOT NULL DEFAULT false,
  wall_opt_in_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (provider, handle)
);
-- statement-break
CREATE TABLE IF NOT EXISTS claims (
  id uuid PRIMARY KEY,
  account_id uuid NOT NULL REFERENCES accounts(id),
  device_id text NOT NULL,
  device_pubkey text NOT NULL,
  device_name text NOT NULL,
  post_url text,
  post_key text,
  follow_check text NOT NULL CHECK (follow_check IN ('pending', 'confirmed', 'n/a')),
  post_check text NOT NULL CHECK (post_check IN ('pending', 'confirmed', 'failed')),
  status text NOT NULL CHECK (status IN ('pending_verification', 'unlocked', 'rejected', 'expired')),
  reject_reason text,
  show_on_wall boolean NOT NULL DEFAULT false,
  post_source text,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL
);
-- statement-break
CREATE INDEX IF NOT EXISTS claims_status_idx ON claims (status, created_at);
-- statement-break
CREATE INDEX IF NOT EXISTS claims_device_account_idx ON claims (device_id, account_id, status);
-- statement-break
CREATE TABLE IF NOT EXISTS followers (
  provider text NOT NULL CHECK (provider = 'x'),
  handle text NOT NULL,
  x_user_id text,
  PRIMARY KEY (provider, handle)
);
-- statement-break
CREATE INDEX IF NOT EXISTS followers_x_user_id_idx ON followers (x_user_id) WHERE x_user_id IS NOT NULL;
-- statement-break
CREATE TABLE IF NOT EXISTS follower_meta (
  id text PRIMARY KEY DEFAULT 'x',
  source text,
  complete boolean NOT NULL DEFAULT false,
  updated_at timestamptz NOT NULL DEFAULT now()
);
-- statement-break
CREATE TABLE IF NOT EXISTS bindings (
  account_id uuid PRIMARY KEY REFERENCES accounts(id),
  device_id text NOT NULL UNIQUE,
  device_pubkey text NOT NULL,
  device_name text NOT NULL,
  bound_at timestamptz NOT NULL DEFAULT now(),
  last_rebind_at timestamptz
);
-- statement-break
CREATE TABLE IF NOT EXISTS unlocks (
  account_id uuid PRIMARY KEY REFERENCES accounts(id),
  provider text NOT NULL CHECK (provider IN ('x', 'linkedin')),
  post_ref text NOT NULL,
  handle text NOT NULL,
  display_name text NOT NULL DEFAULT '',
  avatar_url text,
  profile_url text,
  created_at timestamptz NOT NULL DEFAULT now()
);
-- statement-break
CREATE TABLE IF NOT EXISTS used_posts (
  post_key text PRIMARY KEY,
  account_id uuid NOT NULL REFERENCES accounts(id),
  created_at timestamptz NOT NULL DEFAULT now()
);
-- statement-break
CREATE TABLE IF NOT EXISTS claim_device_limits (
  device_id text NOT NULL,
  day date NOT NULL,
  count bigint NOT NULL DEFAULT 0,
  PRIMARY KEY (device_id, day)
);
-- statement-break
CREATE TABLE IF NOT EXISTS ref_clicks (
  ref text NOT NULL,
  day date NOT NULL,
  count bigint NOT NULL DEFAULT 0,
  PRIMARY KEY (ref, day)
);
-- statement-break
CREATE TABLE IF NOT EXISTS download_hits (day date PRIMARY KEY, count bigint NOT NULL DEFAULT 0);
-- statement-break
CREATE TABLE IF NOT EXISTS revocations (
  account_id uuid PRIMARY KEY REFERENCES accounts(id),
  created_at timestamptz NOT NULL DEFAULT now()
);
-- statement-break
CREATE TABLE IF NOT EXISTS moved_devices (device_id text PRIMARY KEY, account_id uuid NOT NULL REFERENCES accounts(id));
-- statement-break
CREATE TABLE IF NOT EXISTS device_replays (device_id text NOT NULL, ts bigint NOT NULL, expires_at timestamptz NOT NULL, PRIMARY KEY(device_id,ts));
-- statement-break
CREATE OR REPLACE FUNCTION acu_require(n integer) RETURNS integer LANGUAGE plpgsql AS $$ BEGIN IF n = 0 THEN RAISE EXCEPTION 'concurrent_change'; END IF; RETURN n; END $$;
