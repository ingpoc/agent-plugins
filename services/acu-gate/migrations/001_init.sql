CREATE TABLE IF NOT EXISTS accounts (
  id uuid PRIMARY KEY,
  wall_opt_in boolean NOT NULL DEFAULT false,
  wall_opt_in_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
-- statement-break
CREATE TABLE IF NOT EXISTS identities (
  id uuid PRIMARY KEY,
  account_id uuid NOT NULL REFERENCES accounts(id),
  provider text NOT NULL CHECK (provider IN ('x', 'linkedin')),
  provider_user_id text NOT NULL,
  handle text NOT NULL,
  name text NOT NULL,
  email_hash text,
  avatar_url text,
  profile_url text,
  token_blob text,
  UNIQUE (provider, provider_user_id)
);
-- statement-break
CREATE TABLE IF NOT EXISTS x_profiles (
  identity_id uuid PRIMARY KEY REFERENCES identities(id),
  verified boolean NOT NULL DEFAULT false,
  verified_type text,
  followers_count bigint NOT NULL DEFAULT 0,
  username text NOT NULL
);
-- statement-break
CREATE TABLE IF NOT EXISTS login_states (
  state_hash text PRIMARY KEY,
  provider text NOT NULL,
  verifier text NOT NULL,
  nonce text NOT NULL,
  app_challenge text NOT NULL,
  device_id text NOT NULL,
  device_name text NOT NULL,
  link_account_id uuid REFERENCES accounts(id),
  owner_authorization boolean NOT NULL DEFAULT false,
  expires_at timestamptz NOT NULL
);
-- statement-break
CREATE TABLE IF NOT EXISTS login_codes (
  code_hash text PRIMARY KEY,
  account_id uuid NOT NULL REFERENCES accounts(id),
  identity_id uuid NOT NULL REFERENCES identities(id),
  app_challenge text NOT NULL,
  device_id text NOT NULL,
  device_name text NOT NULL,
  expires_at timestamptz NOT NULL
);
-- statement-break
CREATE TABLE IF NOT EXISTS sessions (
  token_hash text PRIMARY KEY,
  account_id uuid NOT NULL REFERENCES accounts(id),
  identity_id uuid NOT NULL REFERENCES identities(id),
  device_id text NOT NULL,
  device_name text NOT NULL,
  device_pubkey text NOT NULL,
  expires_at timestamptz NOT NULL
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
  name text NOT NULL,
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
CREATE TABLE IF NOT EXISTS pending_claims (
  id uuid PRIMARY KEY,
  account_id uuid NOT NULL REFERENCES accounts(id),
  identity_id uuid NOT NULL REFERENCES identities(id),
  post_key text NOT NULL UNIQUE REFERENCES used_posts(post_key),
  status text NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
  created_at timestamptz NOT NULL DEFAULT now()
);
-- statement-break
CREATE TABLE IF NOT EXISTS owner_tokens (
  provider text PRIMARY KEY,
  ciphertext text NOT NULL,
  nonce text NOT NULL,
  tag text NOT NULL
);
-- statement-break
CREATE TABLE IF NOT EXISTS owner_followers (
  x_user_id text PRIMARY KEY
);
-- statement-break
CREATE TABLE IF NOT EXISTS ref_clicks (
  ref text NOT NULL,
  day date NOT NULL,
  count bigint NOT NULL DEFAULT 0,
  PRIMARY KEY (ref, day)
);
-- statement-break
CREATE TABLE IF NOT EXISTS revocations (
  account_id uuid PRIMARY KEY REFERENCES accounts(id),
  created_at timestamptz NOT NULL DEFAULT now()
);

-- statement-break
CREATE TABLE IF NOT EXISTS download_hits (day date PRIMARY KEY, count bigint NOT NULL DEFAULT 0);
-- statement-break
CREATE TABLE IF NOT EXISTS moved_devices (device_id text PRIMARY KEY, account_id uuid NOT NULL REFERENCES accounts(id));
-- statement-break
CREATE TABLE IF NOT EXISTS device_replays (device_id text NOT NULL, ts bigint NOT NULL, expires_at timestamptz NOT NULL, PRIMARY KEY(device_id,ts));
-- statement-break
CREATE OR REPLACE FUNCTION acu_require(n integer) RETURNS integer LANGUAGE plpgsql AS $$ BEGIN IF n = 0 THEN RAISE EXCEPTION 'concurrent_change'; END IF; RETURN n; END $$;
