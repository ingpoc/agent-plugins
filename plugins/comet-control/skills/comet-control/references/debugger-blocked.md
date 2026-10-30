# Debugger blocked by policy (Comet / Chrome 155+)

Symptom: `chrome.debugger.attach` → "Host access is restricted by policy" on every tab
(Comet ExtensionManagement blocks perplexity hosts; Chrome 155 then disables the debugger
for the extension everywhere). Do not work around it (no remote-debugging port, no
Local State/Preferences edits). Capabilities are probed once per lease and logged to
`run/cache/flight-recorder/input-paths.jsonl` (lengths/modes only, never text).

In-page only (0.1.23): comet-control never moves the Mac's real mouse or sends real keys.
Every input below is a DOM event in the leased tab, drawn with comet-control's own cursor
overlay. Agent Computer Use (`$macos-cua`) is only for Mac surfaces outside the browser
(OS sheets, other apps). `real_mouse` / `trusted:"real"` are retired and ignored.

| Need | Action | Path when debugger is off |
|---|---|---|
| Type | `{"type":"type","selector":S,"text":T,"clear":true,"submit":false}` | scripting focus + caret → in-page insert (MAIN world) → verify |
| Key / shortcut | `{"type":"press","selector":S,"key":"Enter","modifiers":["Meta"]}` | synthetic `KeyboardEvent` (keyCode/which set) + default action (Enter/Backspace/Delete/Tab/Meta+a) |
| Click | `click_selector` / `click_text` / `click_at_xy` | cursor overlay glides + pulses, then pointer/mouse events + click at the point |
| Screenshot | `screenshot` (or `"source":"cua_window"`) | `tabs.captureVisibleTab`, else CUA window capture (read-only) |
| JS | `js`/`evaluate` | `chrome.userScripts.execute` (USER_SCRIPT world) → MAIN-world eval → `USER_SCRIPTS_DISABLED` + hint on strict CSP |

In-page text methods (`mode`/`method`; default `auto`). Inputs and textareas: `execcommand`,
then the native value setter + `input` event. Contenteditable editors, tried in order and
only while the target is unchanged (so two methods never stack):

1. `beforeinput`: `InputEvent("beforeinput", insertText)`. Lexical, ProseMirror, Slate.
2. `paste`: `ClipboardEvent("paste")` with text/plain (+ text/html via `html`). Draft.js.
3. `textinput`: `TextEvent("textInput")`. React `onBeforeInput`, and Chrome inserts it itself.
4. `execcommand`: `document.execCommand("insertText" | "insertHTML")`.

The result reports `text_mode` (the method that landed) and `tried`. Text that never lands
fails `TYPE_NOT_VERIFIED` with `tried`, and no Enter is sent.

The broker refuses real-input `steps` with `IN_PAGE_ONLY`.

One-time user step for `js` on CSP sites: comet://extensions → Comet Control → Details →
turn on "Allow User Scripts". Adding the `userScripts` permission (0.1.18) did not
disable the unpacked extension or require re-approval.
Debugger-free since 0.1.22: bring the leased window to front via Chrome window focus and
`upload_file` via a userScripts DataTransfer.
Unsupported without debugger (0.1.23): `DOM.*` CDP, and pages that accept only `isTrusted`
events (report these; do not switch to the Mac's mouse).
