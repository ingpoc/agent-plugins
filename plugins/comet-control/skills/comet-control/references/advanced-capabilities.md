# Advanced browser capabilities

Load this reference only when a task needs semantic/frame locators, raw CDP,
dialogs, clipboard, files, page assets, responsive viewport testing, or user
tab/history access. Routine navigation should stay on `page_context` and the
basic actions in [`operate.md`](operate.md).

## Contents

- [Semantic and frame locators](#semantic-and-frame-locators)
- [Runtime-safe evaluation and CDP](#runtime-safe-evaluation-and-cdp)
- [JavaScript dialogs](#javascript-dialogs)
- [Clipboard](#clipboard)
- [Uploads and downloads](#uploads-and-downloads)
- [Page assets](#page-assets)
- [Viewport](#viewport)
- [User tabs and history](#user-tabs-and-history)

## Semantic and frame locators

Use one `locator` action with a locator specification and an operation:

```python
{"type": "locator",
 "locator": {"by": "role", "role": "button", "name": "Save", "exact": True},
 "operation": "click"}

{"type": "locator",
 "frameSelector": "iframe#checkout",
 "locator": {"by": "label", "label": "Cardholder"},
 "operation": "fill", "value": "Example User"}

{"type": "locator",
 "locator": {"by": "testid", "testId": "samantha-orb-text"},
 "operation": "fill", "value": "Show me my recent orders"}
```

Locator kinds: `css`, `text`, `role`, `label`, `placeholder`, and `testid`.
The value key is kind-specific: `selector`, `text`, `role` + `name`, `label`,
`placeholder`, or `testId`. In particular, `by: "testid"` requires the
camel-cased `testId` key; a lowercase `testid` value key is not the contract.
Optional constraints: `exact`, `visible`, `hasText`, `notHasText`, `within`,
`first`, `last`, and `nth`. Use `frameSelectors: ["#outer", "#inner"]` for
nested frames.

Operations:

- Read: `inspect`, `count`, `all_text`, `inner_text`, `text_content`, `value`,
  `get_attribute`, `is_visible`, `is_enabled`, `is_checked`, `wait`.
- Interact: `click`, `dblclick`, `fill`, `type`, `press`, `check`, `uncheck`,
  `set_checked`, `select_option`.

Interactive locator actions move the labeled Comet Control cursor to the resolved
target before acting. Start with `inspect` only when the semantic match is
ambiguous; do not pull all matches by default.

## Runtime-safe evaluation and CDP

`evaluate` is read-only at runtime through CDP `throwOnSideEffect`; mutation
attempts fail and leave page state unchanged.

Use raw CDP only for developer diagnostics that lack a higher-level action:

```python
{"type": "cdp_send", "method": "Network.enable"}
{"type": "cdp_events"}  # mark current cursor
# perform the action in a later run call
{"type": "cdp_events", "afterSequence": 123,
 "methods": ["Network.responseReceived", "Network.loadingFailed"], "limit": 50}
```

The command surface is tab-scoped and allowlisted. `Runtime.evaluate` is routed
through the same read-only guard. `cdp_events` returns no event payloads when
used as a cursor mark; details load only after `afterSequence` is supplied.

## JavaScript dialogs

```python
{"type": "dialog_get"}
{"type": "dialog_handle", "accept": True}
{"type": "dialog_handle", "accept": True, "promptText": "value"}
{"type": "dialog_handle", "dismiss": True}
```

Alert, confirm, prompt, and beforeunload dialogs share this surface. Locator
clicks, download triggers, and `click_text` / `click_selector` return when a
dialog opens (`dialog_opened`) instead of blocking the lease queue. Handle the
prompt in the **next** action (or the next action in the same batch):

```python
{"actions": [
  {"type": "click_text", "text": "Dispatch order"},
  {"type": "dialog_handle", "accept": True, "promptText": "TRACK-DEMO-1"},
]}
```

Seller ONDC Dispatch uses `window.prompt` for the tracking ID — Comet Control owns
that prompt (`dialog_handle` + `promptText`). Do not reload while a JS dialog
is open; a frozen content-script reply is not “content script missing.”

OS-native surfaces (file chooser sheets, print/system permission UI, Comet
shell administration, and non-Comet apps) are **not** this action surface — hand
the slice to `$macos-cua` per [`multi-agent.md`](multi-agent.md), then resume
this lease. Closeout dismisses an open JS dialog before releasing the session.

## Clipboard

```python
{"type": "clipboard_read_text"}
{"type": "clipboard_write_text", "text": "value"}
{"type": "clipboard_read", "includeData": True}
{"type": "clipboard_write", "items": [{"type": "image/png", "base64": "…"}]}
```

Binary clipboard data is base64 and limited to 750 KB per item across the
native bridge. Without `includeData`, reads return metadata and bounded text,
which is the token-efficient default.

## Uploads and downloads

Set a local file chooser through CDP without copying file bytes into the agent
response:

```python
{"type": "upload_files", "selector": "input[type=file]",
 "paths": ["/absolute/path/report.pdf"]}
```

Uploading transmits a local file to a webpage; apply the harness confirmation
policy before this action.

```python
{"type": "download_click",
 "locator": {"by": "role", "role": "link", "name": "Export"},
 "timeout": 30000}
{"type": "download_media", "locator": {"by": "css", "selector": "video"}}
```

Download results include the materialized absolute filename. A click that
opens a dialog returns a diagnostic instead of wedging the session.

## Page assets

Inventory first, then bundle only the required slice:

```python
{"type": "page_assets_list", "limit": 200}
{"type": "page_assets_bundle", "inventoryId": "…", "kinds": ["image", "stylesheet"]}
```

Inventory observes DOM attributes, computed background images, resource timing,
and optional inline SVG. Bundles land under `~/Downloads/Comet Control Page Assets/`
with a JSON manifest and per-file failures.

## Viewport

```python
{"type": "viewport_set", "width": 390, "height": 844,
 "deviceScaleFactor": 2, "mobile": True}
{"type": "viewport_reset"}
```

Reset temporary viewport overrides before closeout unless the user asked to
keep the responsive state.

## User tabs and history

Use top-level socket requests, not `run.actions` entries:

```python
{"type": "user_tabs", "filter": "checkout", "limit": 20}
{"type": "history", "query": "focused term", "startTime": 0, "limit": 20}
```

User-tab inventory and history are read only. Controlled work always starts a
new window lease; existing operator or peer-agent tabs cannot be claimed.
