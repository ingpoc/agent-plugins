# Debugger blocked by policy (Comet / Chrome 155+)

Symptom: `chrome.debugger.attach` → "Host access is restricted by policy" on every tab
(Comet ExtensionManagement blocks perplexity hosts; Chrome 155 then disables the debugger
for the extension everywhere). Do not work around it (no remote-debugging port, no
Local State/Preferences edits). Capabilities are probed once per lease and logged to
`run/cache/flight-recorder/input-paths.jsonl` (lengths/modes only, never text).

| Need | Action | Path when debugger is off |
|---|---|---|
| Type | `{"type":"type","selector":S,"text":T,"clear":true,"submit":false}` | scripting focus + caret → raise lease window/tab → CUAService keys → scripting verify |
| Key / shortcut | `{"type":"press","selector":S,"key":"Enter","modifiers":["Meta"]}` | same, `press_key` |
| Trusted click (isTrusted gate) | `click_selector`/`click_text` with `"real_mouse":true` | AX press of the element under the point (trusted) |
| Screenshot | `screenshot` (or `"source":"cua_window"`) | `tabs.captureVisibleTab`, else CUA window capture |
| JS | `js`/`evaluate` | `chrome.userScripts.execute` (USER_SCRIPT world) → MAIN-world eval → `USER_SCRIPTS_DISABLED` + hint on strict CSP |

Text mode: printable ASCII ≤40 chars on U.S./ABC layout → per-key `press_key`;
anything else (unicode, emoji, IME) → clipboard paste with transient/concealed markers,
clipboard restored. If keystrokes drop chars (SPA re-render, e.g. x.com search), the
flow retypes once via paste when `clear` or the field was empty (`recovered_via:"paste"`).

Failed → fixed:
- CUAService AX `type_text` on Chromium web inputs is refused → `execute_plan` of `press_key` or Cmd+V.
- pid-posted x/y click is ignored by web content → `click` with `element_index` (AX press).
- Bounds-only window match collides with another same-size Comet window → also match AX title == tab title.
- Type flow moved caret before a shortcut (`Meta+a` then text) → focus keeps an existing selection.
- `goto comet://extensions` from a lease → `NAVIGATION_TIMEOUT`; the user must open it.

One-time user step for `js` on CSP sites: comet://extensions → Comet Control → Details →
turn on "Allow User Scripts". Adding the `userScripts` permission (0.1.18) did not
disable the unpacked extension or require re-approval.
Debugger-free since 0.1.22: bring the leased window to front via Chrome window focus, `upload_file`
via a userScripts DataTransfer, and real-mouse clicks mapped through the raised window's real frame
(Stage Manager-safe, restores the previous front app on release).
Unsupported without debugger (0.1.22): `DOM.*` CDP, real mouse inside iframes, forward-Delete.
