# Special surfaces (widgets, Notification Center, iPhone Mirroring)

Load only when the target is not an ordinary AX app window. Prefer scriptable
CLIs/APIs first; use macos-cua only for irreducible visual actions.

## Discover without dumping `apps`

Do not call bare `apps` to resolve a known name or bundle. Target it directly,
or filter:

```bash
python3 $SKILL_DIR/scripts/macos-cua.py apps --query ScreenContinuity
python3 $SKILL_DIR/scripts/macos-cua.py apps --running --query Workstream
python3 $SKILL_DIR/scripts/macos-cua.py state com.apple.ScreenContinuity --compact
```

## Desktop widgets / Notification Center

| Fact | Rule |
| --- | --- |
| Process is often `com.apple.notificationcenterui` | Not an ordinary titled AX window |
| `state` / foreground may return `foreground window is not AX-ready` | Do not invent AX readiness or retry `state` loops |
| Widgets sit on a display, often secondary | Prefer app/WidgetKit test APIs and screenshots. Global Quartz is an explicit user-interruptive last resort |

Point actions (no nonexistent `right-click-point`):

```bash
# Window-local (AX-backed app window screenshot)
python3 $SKILL_DIR/scripts/macos-cua.py \
    click-point "Workstream Status" 120 80 --button right --preserve-pointer

# Global Quartz (desktop widget / non-AX surface) — coords from a fresh capture
python3 $SKILL_DIR/scripts/macos-cua.py \
  click-desktop 3480 220 --button right --preserve-pointer
```

AX label/index first when the host app window is AX-ready. Coordinate fallback
only from a fresh screenshot and only when interruption is acceptable; never
reuse stale secondary-display points. Pointer restore reduces visible pointer
displacement but cannot prevent click/focus interference.

## iPhone Mirroring (`com.apple.ScreenContinuity`)

Last-resort GUI proof after scriptable device tooling (`xcrun devicectl`
install/launch/list). When mirroring is required:

1. Confirm the real phone target (not Simulator).
2. Address the Mac app as `com.apple.ScreenContinuity` or `iPhone Mirroring`.
3. Prefer background `state … --compact` once. Use AX/targeted keyboard if
   exposed. Home Screen widgets are often custom/non-AX; defer GUI proof while
   the user is active, or use a fresh coordinate plus `--preserve-pointer` as an
   explicitly interruptive last resort.
4. Keep the GUI sequence minimal; capture proof PNG; do not drive install/launch
   through mirroring clicks when `devicectl` can do it.

## Codex Computer Use pattern (borrow mechanics, not prose)

Bundled `@Computer` / `computer-use` prefers connectors/CLIs and AX
`element_index` actions, which its driver documents as background-safe with no
cursor move or focus steal. Real sessions overwhelmingly use element indices;
coordinate actions cluster on AX-poor surfaces such as iPhone Mirroring.
macos-cua mirrors that hierarchy. Browser work remains isolated in CDP. Native
coordinate work is not isolated and must be explicit.
