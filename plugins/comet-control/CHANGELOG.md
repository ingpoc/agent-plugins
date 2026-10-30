# Changelog: comet-control

Built from commit history (`git log -- plugins/comet-control`). Dates are commit dates.

## 0.1.23 (2026-10-30)

- In-page input only: comet-control never moves the Mac's real mouse or sends real keys. Without the debugger, type, key and click run as DOM events in the leased tab, and the in-page cursor overlay draws each one.
- `real_mouse` and `trusted:"real"` are retired (ignored, in-page click instead). The broker refuses real-input `steps` with `IN_PAGE_ONLY`.
- Contenteditable typing tries `beforeinput`, `paste`, `textInput` and `execCommand` in order, stops at the first that lands, and reports `text_mode` and `tried`.
- Synthetic keys set `keyCode`/`which` (Draft.js reads them) and apply the default action for Enter, Backspace, Delete, Tab and Meta+A.

## 0.1.22 (2026-10-30)

- Debugger-free bring-to-front for a hidden leased window through Chrome window focus (PR #24).
- Debugger-free `upload_file` through a userScripts DataTransfer; files are verified before dispatch, and a page reset after the change counts as success (PR #25).
- Stage Manager-safe raise: the leased window is matched even when clamped (origin, size slack and title), raise waits up to 1.5 s for the thumbnail to settle, real-mouse clicks are mapped with the raised window's real frame, and release restores the previously frontmost app (PR #26).
- Screen checks go to the local mini-brain before cloud vision.

## 0.1.21 (2026-10-24)

- Real input checks the CUAService runtime contract before the first call.

## 0.1.18 – 0.1.20 (2026-10-22)

- Real-input fallback when the debugger is blocked by policy (Chrome 155 "Host access is restricted by policy"): DOM click, tab back/forward, Runtime.evaluate through chrome.scripting.
- Native writes are verified and command failures propagate; native `prepare_raise` flow.

## 0.1.14 – 0.1.17 (2026-10-10 – 2026-10-12)

- One-pass form plans, per-run call ledger, opt-in scoped AX fallback, event-based navigation settle, actions pinned to their window, closeout waits for cursor hide.

## 0.1.1 – 0.1.13 (2026-08-23 – 2026-09-22)

- Shared portable runtime, lease tab scoping, action-scoped inject, watchable cursor, compact semantic `page_context`, stable click rects and persisted closeout proof.

## 0.1.0 (2026-08-16)

- First portable plugin: skill, broker and Comet extension.
