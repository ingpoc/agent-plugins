# Context Ledger preferences

Load before a preference-sensitive site/app choice, recurring or critical workflow decision, or when the user states or corrects a durable preference.

Local telemetry keeps bounded, credential-redacted Ledger results and error context. The concise observability view shows status/count; opt into details with `include_diagnostics=true`. Preference values, search queries, and transcripts stay out of the report.

Use `ledger_task` automatically; do not wait for the user because an applicable saved preference can change the choice. Before a preference-sensitive site/app choice or recurring/critical workflow decision, search the site or app `scope` without guessing a rule, then apply only an unambiguous active relevant match. For a named site or app use `tool:<site-or-app>`; never substitute `fleet` or browser scope because preferences must stay within their owner. Use `fleet` only for genuinely global preferences. Valid scopes are `fleet`, `tool:<lowercase-id>`, or `project:<lowercase-id>`; rules use lowercase `snake_case`.

A pending application outcome and hint tier describe past use. Apply an unambiguous active relevant user decision as a saved preference, including a self-reported record; do not claim owner attestation or prior successful use because retrieval is not proof of either.

For lookup, provide the site or app `scope` and concrete decision terms in `need`; the helper searches both `need` and `situation`. Recall searches the provided `need` and `situation` plus exact rule terms. An empty exact-rule lookup may search the same scope for relevant candidates; apply only an unambiguous active relevant match. `needs_disambiguation` contains candidates, not a verified preference. If `has_more` is true, ask rather than select from an incomplete set; otherwise use the agreed value when active relevant user decisions match even if rule names differ. Ask if relevant values conflict or applicability is unclear.

After the owner states or corrects a durable preference, save it with `preference`, its exact value, and the exact owner quote. Use `capture` for critical decisions only when the owner decision is explicit, durable, model-safe, has a concrete future trigger, and adds unique value. Skip one-offs, temporary permissions, secrets, status, uncertainty, and duplicates. A preference update with the same scope and rule supersedes its older active `user_decision`; general captures do not auto-supersede, and this tool cannot create an explicit supersession link. Never claim a general decision was replaced because no supersession link exists. Report saved only after a receipt.

`ledger_invalid_argument` is non-retryable: stop and explain; do not retry with reworded arguments unless a later user turn supplies new context. `ledger_unavailable` means stop because there is no reliable Ledger result to apply.
