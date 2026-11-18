# Owner preferences and decisions

## Owner preferences and decisions

Before acting at a site, app or task decision point, call `find` with exact `scope` and `rule`; apply the active value without asking and say so briefly; record an `applied` event after it is actually used. Use `tool:<site-or-app>` or `project:<task>` and a stable lowercase decision-point slug; use `fleet` only for cross-cutting choices.
An owner-stated preference, choice or correction always passes the capture gate, even in a simple task: future task is the same decision point, changed choice is the value, unique value is the owner preference, and lifetime is durable unless one-off. Save `user_decision` with scope, rule and entities including the site/app or project and decision point; do not ask. A changed choice supersedes the prior one; the save receipt identifies committed replacements in `superseded_ids`. Never store one-offs, temporary authorizations or secrets.
