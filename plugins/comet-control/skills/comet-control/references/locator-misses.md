# Comet Control — Locators, frames, and misses



- `click_text` skips `position:sticky` / `position:fixed` header chips when an in-page match exists. A **unique** sticky name still matches; the click point then uses the nearest same-column card rect, not the stuck inset (FPL names were y≈439 with the real card X). Do not fall back to coordinates.
- Checkbox / radio clicks must activate the native input (`HTMLElement.click()`). Synthetic `MouseEvent` (`isTrusted: false`) reports success and does not toggle CSS-custom checkboxes (FPL Vice Captain).
- `click_selector` searches open shadow roots. A 16×16 icon is on-target if `elementFromPoint` hits a descendant or a `button` / `[role=button]` ancestor.
- `page_context` / `getStatus` are pinned to the top frame. Ad/Twitter/DoubleClick iframes must not win. Do not treat an ad title as the product page.
- Screenshot `captureVisibleTab` is bounded (~8s). A hang returns `SCREENSHOT_TIMEOUT` and must not occupy the worker FIFO for minutes (that is what knocked Grok Bot local-exec offline). Do not mint a second session; retry later on the same lease or skip the opening screenshot.
- After `viewport_set`, the live worker (gen 2, sha `5a06352…`) skips `captureVisibleTab` and uses CDP `Page.captureScreenshot` (`stale_capture_skipped`) until `viewport_reset`. Locators still win. On an older worker, treat identical hashes as stale.
- Mini "offline" / empty machines is Grok Bot local-exec (`desktop ownership lost`, ConnectError, DeadlineExceeded), not sleep and not Comet pairing. Probe can stay `extension_connected`. CUA cannot recover it (same pipe). Known Grok Bot bug. Workaround: fully quit Grok Bot (`Cmd+Q`), kill leftover `local-exec-daemon` processes, relaunch once. Two Grok Bot desktops flap ownership. Do not mint a second Comet session for this.
- Locator `press` / `type` skip the box-center mouse click when `document.activeElement` is already inside the target. Clicking a tall contenteditable's geometric center relocates the caret mid-document and splits the wrong block.
- Locator `click` is CDP mouse only and does not `element.focus()`. Locator `type` mouse-clicks then `Input.insertText` only when the field is not already focused, so it types into whatever is focused (often a title field). `fill` on contenteditable calls `element.focus()` and sets `textContent`, now through bounded `executeScriptOnTab` (~8s, `locatorFill`). A long contenteditable fill times out instead of wedging the worker. Prefer `cdp_send Input.insertText` after the editor is focused; do not locator `type` into the wrong field; do not Meta+V as a paste strategy.
- **Contenteditable + embeds (app-agnostic):** Meta+A / select-all + Backspace and `fill ""` often clear only text runs. Embedded/media/atomic blocks commonly survive. Do not treat “clear then rebuild” as a recovery path; compose forward on a fresh empty editor, or use the product’s per-block remove controls. Domain skills own product-specific recovery.
- **Sticky block styles:** quote / heading / list toolbars can leave the caret in that block type so later `Input.insertText` inherits it. After applying a block style, explicitly reset to the default paragraph/body control before the next insert.
- **Leftover choosers:** an invisible file/media/dialog layer swallows the next locator. Assert the chooser closed (or close via the app’s dismiss control) before the next paste/upload.
- **Multi-file inputs:** some products coalesce multiple paths into one gallery/attachment. When the product needs distinct blocks, upload one path per operation.
- `upload_files` is top-document `DOM.querySelector` and does not see file inputs inside iframes (iCIMS `#icims_content_iframe`). `cdp_send` cannot call `DOM.setFileInputFiles`. Until patched: locator click the iframe upload control with `frameSelector: "#icims_content_iframe"`, then CUA the native Open sheet. Stay on the same lease.
- A name locator that is a substring of another option wins the wrong one (`India` matches `British Indian Ocean Territory`). Click only `textContent` equality.
- A missing locator fails fast (`ACTIONABILITY_*` / `ELEMENT_NOT_FOUND`) and must not reload the leased tab. Do not mint a second session or host-reload to "fix" a miss.
- `cursor_scroll` walks custom overflow ancestors (including shadow hosts). If a pitch/list still ignores it, click the row by locator.

