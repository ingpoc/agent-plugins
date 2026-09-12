# Secure Copy → Keychain (masked dashboard secrets)

Proven 2026-09-12: Render Environment `ONDC_SELLER_UNIQUE_KEY_ID` via Comet **Copy value** (masked) → Mini Keychain. Prefer this over chat or secure-paste fields.

## When

Dashboard shows a secret with **Copy value** while the value stays masked (Render, Auth0, similar). Human should not paste into chat.

## Recipe

1. Lease Comet on the env/secrets page. **Do not close** until captain says done.
2. Ensure Comet is frontmost (empty clipboard if not).
3. Click **Copy value** for the target key. Do not Reveal into screenshots/chat.
4. On Mini (never print clipboard):

```bash
pbpaste | security add-generic-password -U \
  -a "<account>" \
  -s "<group>" \
  -l "<group>.<account>" \
  -j "group=<group>" \
  -w
pbcopy </dev/null
```

5. Verify without `-w` (or length-only). Report labels + `fetch_len` only.
6. Write **labels only** to shared user memory.

Canonical Keychain rules: skill `mac-keychain-secrets`.
