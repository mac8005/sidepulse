# SidePulse for iPhone (and iPhone Duo)

SidePulse watches the coding agents running on a Mac. Everything in the app
exists to answer one question from across a room: **does anything need me?**

## Principles

1. **State first.** The first line of the app is a sentence — "4 need you",
   "3 working", "All quiet" — not a list you have to read.
2. **Never colour alone.** Every state carries a word, a glyph and a colour.
   Take the colour away and the screen still works.
3. **One level of navigation.** The board is the root. The Dot and Settings
   are one push away. Nothing sits in front of the thing you opened the app for.
4. **Calm surfaces.** Content lives on plain grouped backgrounds. Liquid Glass
   is the system's layer — bars, sheets, controls — and the app does not imitate
   it or stack its own glass on top of it.
5. **Honest about time.** A snapshot older than ninety seconds says when it is
   from. A meter says when it resets. Nothing pretends to be live that is not.
6. **The hardware is part of the design.** The fold, the hinge angle and the
   vertical bar strip are inputs, not obstacles.

## Information architecture

```
Board (root)                     the sessions, grouped by urgency
├── SidePulse Dot                preview, behaviour, hand-sent patterns
└── Settings                     monitored Mac, notifications, proxy, diagnostics
    └── Push token (sheet)
```

There is no landing screen. The old Home screen's panels moved to where they
belong: the Dot setup and the pattern grid are the Dot screen, the connection
state is the board's header, the push token is a row in Settings.

On a display with room for two panes the board keeps the primary pane and the
usage meters and the Dot move beside it — the same content, not more.

## Components

| Component | Where | Notes |
| --- | --- | --- |
| `SessionRow` | board, dashboard | title / quiet line / state. Dense variant gives the title the whole first line. |
| `ConnectionPill` | board header | live · connecting · offline · paused |
| `BoardMessage` | board | every empty and error state |
| `SetupChecklist` | board | first run: three numbered steps, not a blank list |
| `UsageMeter` | usage section | label, countdown, percent, threshold bar |
| `DotPreview` | Dot screen, dashboard | two LEDs in the colours the Dot would show |
| `AgentsStatusBoard` | tabletop pose | distance-readable board, whole rows only |

## State palette

System colours, because they already carry the light, dark, Increased Contrast
and colour-blind-friendly variants Apple ships. Defined once in
`AgentModeStyle.tint(_:)` and shared with the Live Activity.

| State | Word | Glyph | Colour | Group |
| --- | --- | --- | --- | --- |
| `blocked_error` | Blocked | `exclamationmark.triangle.fill` | red | Needs you |
| `waiting_for_input` | Asking | `questionmark.bubble.fill` | orange | Needs you |
| finished, not yet opened | New | `checkmark.circle.fill` | green | Needs you |
| `working` | Working | `bolt.fill` | blue | Working |
| `tool_running` | Running | `wrench.and.screwdriver.fill` | indigo | Working |
| `long_task_progress` | Long task | `hourglass` | purple | Working |
| `completed` | Finished | `checkmark.circle.fill` | green | Finished & idle |
| `idle_ready` | Idle | `moon.fill` | secondary | Finished & idle |

Meters use thresholds, not a gradient: green under 60 %, orange under 85 %, red
above — and the percentage is printed next to the bar.

## Type, space, motion

- Dynamic Type throughout; the row glyph is a `@ScaledMetric` so it grows with
  the text. Nothing is laid out at a fixed width except 12–34 pt glyphs.
- One rhythm: 12 pt inside cards, 16 pt corner radius on cards, 10 pt on the
  controls inside them, so the curves stay concentric with the hardware's.
- Motion is state, never decoration: `symbolEffect(.pulse)` on a session that is
  still moving, `.variableColor` on the live connection pill, `numericText` on
  counters. Every one is switched off under Reduce Motion.

## Non-happy states

| Situation | What the app shows |
| --- | --- |
| No server configured | numbered setup checklist + Open Settings |
| Connecting | "Connecting to <host>" with the reason |
| Unreachable | "Can't reach <host>", the error, Open Settings |
| Stale snapshot (> 90 s) | "· as of 09:41" in the subtitle |
| No sessions | "All quiet — no agent is running on <host> right now." |
| Usage unavailable | the daemon's message, inline, not an empty section |

## Accessibility

- Rows are one element: label = title, value = "state, in project, detail",
  hint = "Opens this session in Claude".
- Meters: label = window, value = "41 percent used, resets in 1 hour".
- Board banner and section headers are combined elements with counts spelled out.
- Tap targets ≥ 44 pt (pattern tiles are `minHeight: 44`).
- Reduce Motion stops every effect; Reduce Transparency is respected because the
  app draws on system grouped backgrounds rather than custom materials.

---

# iPhone Duo conformance

Every guideline below is quoted from Apple's *Designing for iPhone Duo* (HIG,
9 September 2026). Screenshots are in
`/Volumes/MacMiniData/Developer/duo-shots/sidepulse/`.

| # | HIG guideline (quoted) | How SidePulse complies | Proof |
| --- | --- | --- | --- |
| 1 | "Build your app to resize. … Use size classes, layout margins, and safe area insets … Avoid fixed widths and display-specific dependencies." | Only `horizontalSizeClass`, `verticalSizeClass` and measured container size are read. No `UIScreen`, idiom or orientation check anywhere; the only fixed frames are 12–34 pt glyphs. | every screenshot |
| 2 | "Create a consistent experience across displays. Keep functionality and the state of elements the same between displays … show an additional level of hierarchy on the larger inner display if it makes sense." | Same board, same rows, same actions on both. The inner display adds one level — the usage meters and Dot beside the sessions instead of below them. | `01-board__outer__A.png`, `01-board__inner-land__A.png` |
| 3 | "Maintain the same functionality across device poses." | Tabletop replaces the list with a distance-readable board but keeps every control on the lower half: new session, usage, Dot, and the toolbar. | `07-tabletop__tabletop__A.png` |
| 4 | "Follow the system's vertical layout for toolbars, tab bars, and navigation controls." | Only system bars; `toolbarVerticalBehavior` is never disabled in the shipping configuration. The comparison pair exists only as a DEBUG flag for this decision. | `11-strip-compare__outer__vertical.png` |
| 5 | "Adapt your layout when the device folds. Prefer a layout container that adapts automatically … In a grid-style layout, prefer an even number of columns so content divides cleanly." | `ArrangementView(.split)` for both panes; the pattern grid takes its column count from `duoColumnCount`, which rounds down to an even number whenever a division region exists (`[.includeInactive]`). | `01-board__half__A.png`, `02-dot__inner-land__A.png` |
| 6 | "Avoid extreme layout changes as people fold the device. Move only what's necessary." | Folding moves the panes onto the two halves; nothing is rearranged. The only pose-specific layout is tabletop, and it keeps the same content in the same order. | `01-board__inner-land__A.png` vs `01-board__half__A.png` |
| 7 | "Keep navigation outside of arrangement views." | The single `NavigationStack` wraps every `DuoSplit`; no arrangement view contains a navigation container. | `ContentView.swift` |
| 8 | "Reserve the top of the vertical axis for primary navigation controls … followed by prominent actions." | The board is the root, so there is no Back button at all; the pinned trailing slot holds the attention item, then New session, then the system overflow. | `01-board__outer__A.png` |
| 9 | "Prioritize frequently used toolbar items … keep controls that convey important status, like items with badges, visible longer." | The badged attention item is `.topBarPinnedTrailing` with `visibilityPriority(.high)`; New session is high; everything else lives in the overflow. | `01-board__outer__A.png` |
| 10 | "In general, don't override the default bar placement." | Not overridden. `toolbarVerticalBehavior(.disabled)` is used only on the two single-button sheets, which the same guidance endorses. | `04-token__outer__A.png` |
| 11 | "Consider using the full display width for interfaces where bars aren't necessary … letting a background image or header span the full width while scrollable content stays inset." | The grouped background runs the full width under the strip; the scrolling list stays inside the safe area. | `01-board__outer__A.png` |
| 12 | "Group related toolbar items instead of spacing them manually." | `ToolbarItemGroup` / single items only; no spacers. | `AgentsLiveView.swift` |
| 13 | "Provide both a title and a symbol for each toolbar item that isn't text-only." | Every item is a `Label(title, systemImage:)`. | — |
| 14 | "Keep text-based buttons to a minimum." | The only text-only buttons in the app are the two sheet "Done" buttons, which sit in horizontal bars by design. | `04-token__outer__A.png` |
| 15 | "In task-oriented experiences, minimize the tab bar to preserve the toolbar actions." | The board is a monitoring surface: `toolbarVerticalCompressionBehavior(.prefersToolbarItems)`. | `AgentsLiveView.swift` |
| 16 | "Use the system overflow menu … Reserve the ellipsis symbol for overflow." | One `ToolbarOverflowMenu` (Dot, Settings, Refresh). No hand-rolled ellipsis anywhere. | `01-board__outer__A.png` |
| 17 | "The outer front-facing camera … is always visible, vertically aligned with controls on the side." | Nothing is drawn into the reserved region; the system inset is respected through the safe area. | `01-board__outer__A.png` |
| 18 | "the inner display in portrait … has enough vertical space to keep standard horizontal bars." | Checked and shipped as such — the same board with horizontal bars. | `01-board__inner-port__A.png` |
| 19 | "Locate controls near the content they affect." | Usage and Dot controls live in the pane that shows them, not in the bar. | `01-board__inner-land__A.png` |
| 19b | "Create a consistent experience" (first run) | A phone with no Mac configured shows a numbered setup checklist instead of an empty list. | `08-setup__outer__A.png` |
| 20 | "People interact with the outer display when the device is closed" (glance surface) | A short container switches the board to its dense form: one-line titles, state on the quiet line, tighter rows — all nine sessions at once. | `01-board__outer__A.png` |

### Gaps

- **Dynamic Island on the outer display.** The Live Activity's Lock Screen card
  renders (`10-liveactivity-lock__outer__A.png`), but the simulator never shows
  the island in the side strip, so the `isDynamicIslandLimitedInWidth` layouts
  are written to Apple's description and unverified until real hardware.
- **Split View multitasking.** Not scriptable here. The app falls to its compact
  layout at half width, which is the same layout proven at 466 pt on the outer
  display.
- **Multiple windows.** Deliberately off: SidePulse watches one Mac through
  app-wide singletons, so a second scene would duplicate the stream and the USB
  writes without showing anything new.
