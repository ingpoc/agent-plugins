CREATE TABLE IF NOT EXISTS trials (
  id uuid PRIMARY KEY,
  account_id uuid NOT NULL UNIQUE REFERENCES accounts(id),
  device_id text NOT NULL UNIQUE,
  provider text NOT NULL CHECK (provider IN ('x', 'linkedin')),
  handle text NOT NULL,
  claim_id uuid NOT NULL REFERENCES claims(id),
  started_at timestamptz NOT NULL,
  ends_at timestamptz NOT NULL,
  converted_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
-- statement-break
CREATE TABLE IF NOT EXISTS gate_settings (
  key text PRIMARY KEY,
  value text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
