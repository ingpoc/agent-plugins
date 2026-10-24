# Verification contract

Source checks: `python3 -m unittest discover -s tests`; the collection runner
selects the pinned local runtime installed by `runtime/install.py`. Tests cover
modern discovery, legacy initialization, six tools, typed errors, Apps resource,
generated JS syntax, reproducible bundle and delayed project-response races.
Backend tests belong to the Kindle project, `tests/test_observability_api.py`.

Live tool probe: `tests/probe_installed.py --launcher /absolute/installed/launcher
--output /private/proof.json [--decision]`. Ordinary tests never create production
findings or call Decider. The explicit probe asserts recommendation state is
unchanged. Prototype/installation/real backend/host rendering remain separate
proof levels. Store user-specific output outside this public repository.

UI acceptance uses the official AppBridge in a loopback-only test host. Require
visible initialization, selectors, details, bounded evidence pagination, truthful
recommendation states and wrapped long identifiers. Close the single browser
lease with absence verified. Stop the temporary host afterward. This does not
prove native embedding in every MCP host.

A paired three-run local retrieval check found CLI median 39.72 ms versus warm
MCP 6.88 ms. Cold startup plus discovery was 774.34 ms. Response encoding was
12,778 versus 1,592–1,594 cl100k_base token equivalents, counting the full MCP
result conservatively. It compared the same known finding's headline, status
and redacted evidence, not full-record parity. These are response encodings,
not billed model tokens or total agent-session savings; benchmark scores remain
unmeasured pending a representative workload.

A Comet test found and fixed an actual embedded-script corruption: JavaScript
`String.replace` replacement-string dollar sequences were interpreting bundled
SDK text. The builder now uses a replacement callback; parsing the final embedded
script is a regression gate. Another test caught long identifier overflow;
issue titles now wrap. Project-switch races are exercised for both UI selectors
and host-delivered results.
