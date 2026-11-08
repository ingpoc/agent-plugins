# Optional tools

Load only when the task needs recommendations, a typed decision, storage or the dashboard.

4. `observability_recommendation` reads the Context Ledger pilot. Explicit
   `operation: propose` may call Decider and persist an investigation; unsupported
   projects fail instead of inventing model recommendations. Their issue details
   still include existing rule-based leads.
5. `observability_decide` requires a current snapshot ID, nonsensitive context,
   a typed choice question and the optional local Decider (`decider_unavailable`
   otherwise). Report service confidence and uncertainty;
   failures remain failures, never a fallback. No action is executed.
6. `observability_storage` (`dry_run` default true) reports disk use of every
   observability path (Samantha log sizes and `*signoz*` VMs included) and the
   prune plan. Collect already prunes after each run: expired plugin-owned files
   go to `~/.Trash/developer-observability-pruned/`; `~/.cache/macos-cua` and
   Samantha logs are never touched. `storage` findings flag paths over 1 GiB.
7. `observability_dashboard` opens the embedded view in MCP Apps hosts.
