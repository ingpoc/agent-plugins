# Product wrapper skills (on comet-control)

Load only when **authoring or reviewing** a **thin product skill** (like `smallcase-manager`) that drives a site through comet-control. Do not re-teach leases or CDP. Point at comet-control; put product defaults + verified recipes in the wrapper.

## Shape

```
my-product/
  SKILL.md              # when-to-use, defaults, campaign, bans, pointers
  references/<flow>.md  # verified action batches only
```

**SKILL.md** = when · execution · defaults · campaign · do-not.  
**references/** = start URL · JSON actions · real `expect` · bans.  
No second lease layer, no Browser Use, no evaluate-click.

## Platform contract (inherit; do not copy)

Wrappers **must load comet-control + fast-path**. Keep only product-specific restrictions, defaults, and approval gates in the wrapper. Do not paste lease/CDP/observe rules into every product skill.

## Campaign

1. Probe → one `durable_lease_controller.py start` (stable label).
2. Run `references/` recipes.
3. Closeout once; `verified_absent: true`.

## Writing a recipe (live-prove first)

1. Prove once on Mini with comet ≥0.1.13 before writing. Do not invent expects from docs alone.
2. **Record** proof date, installed runtime version, and a redacted evidence reference. Proposed steps stay **unverified** until live-proven; approval ≠ verified recipe.
3. **Start URL must include required query/route state.** Missing params can make a CTA “succeed” (URL bit flips) while UI stays on the prior step (`smallcaseKind=smallcase` vs bare `/create`).
4. **`expect` what actually changes** (URL or new module/CTA set). Not body copy that stays on the old screen (`Name`, `not_text: Review`).
5. **Dismiss overlays before primary CTAs.** Open search/combobox can swallow the first click — Escape or clear, then click.
6. **Preconditions before the CTA** (e.g. ≥2 valid rows, real ₹ amount, button enabled).
7. **Locators (≥0.1.13):** `click_selector` + `text`, or `click_text`. Unique aria/placeholder for fill. Never bare `button` / bare `input[type=text]`. Never evaluate-click.
8. **Combobox:** `fill_selector` → `click_selector` `[role=option]` + text → Escape.
9. **Stop gates** in the recipe (before Invest/Confirm). Wrapper owns approval.

## Skeletons

### SKILL.md

```markdown
---
name: my-product
description: >-
  Use when <product UI task> via comet-control on Mini — never Chrome browser-use.
---

# my-product

Via **comet-control only**. Never Chrome browser-use.

## Execution
- Load **comet-control** + **fast-path**; inherit platform contract.
- (product-specific restrictions / gates only)

## Defaults
- (account, names, stop-before-X)

## Campaign
1. Probe → one durable start (label `my-product`).
2. Recipes in **references/<flow>.md**.
3. Closeout; `verified_absent: true`.

## Do not
Chrome · evaluate mutations · retry loops · silent sidestep · unapproved money actions
```

### references/.md

```markdown
# <Flow> (comet-control ≥0.1.13)

## Start URL (required)
https://example.com/path?required=param

## Steps (verified JSON)
{"type":"goto","url":"..."}
{"type":"…","expect":{"url_contains":"…"}}

## After success you should see
- (concrete UI — not the previous screen)

## Bans
bare button · wrong expects · overlay-open CTA · silent sidestep
```

## Break table

| Symptom | Likely cause | Action |
| --- | --- | --- |
| Click ok, UI unchanged | Missing required query; overlay stole click; wrong expect | Fix recipe; re-prove |
| `ACTIONABILITY_TARGET_COUNT` | Ambiguous/disabled locator | Unique selector+text; wake ACU if plugin |
| `EXPECT_UNVERIFIED` | Expect needle may be right; UI did not reach claimed state | Inspect evidence; do not assume the expectation is wrong |
| Recipe right, still broken | Plugin/locator defect | Wake ACU (lease id + flight-recorder) |

## Recovery (platform vs product)

An error is not a diagnosis. Stop mutations. At most one compact read (`page_context` / `snapshot`). Never weaken `expect` just to pass. Platform defects → **ACU** with session id, failed action, expected vs observed, runtime version, and a redacted flight-recorder ref. Product bots must not patch or restart the platform.

ACU owns comet-control. Product wrappers own product recipes only.
