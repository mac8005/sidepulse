# SidePulse for iPhone (and iPhone Duo)

SidePulse watches the coding agents running on a Mac. Everything in the app
exists to answer one question from across a room: **does anything need me?**

## Principles

1. **State first.** The top line is a sober status line — "2 need attention ·
   3 working · 9 sessions" — with the host and the reading time under it.
2. **Colour is information, not decoration.** Neutral by default. Exactly one
   accent, the app tint, for interactive things and the unread dot. Semantic
   colour only where the owner has to act: red for blocked, orange for needs
   input, on a small glyph and the state word — never as a fill.
3. **System list styling.** Inset-grouped lists, hairline separators, standard
   row heights and section headers. No floating cards, no glows, no shadows.
4. **One level of navigation.** The board is the root; the Dot and Settings are
   one push away.
5. **Honest about time.** "Updated 09:41" under the status line; ages in
   monospaced digits; meters say when they reset.
6. **The hardware is part of the design.** The fold, the hinge angle and the
   vertical bar strip are inputs, not obstacles. The strip question is closed:
   Apple's default placement, kept light — no Back button, the attention bell
   only while something needs attention, New session, one system overflow —
   and the grouped background runs underneath it so it never reads as a dead
   column.

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

Three hues on a normal screen: the label greys, the app tint, and — only when
something has stopped for a person — red or orange.

| State | Word | Glyph | Colour |
| --- | --- | --- | --- |
| `blocked_error` | Blocked | `exclamationmark.triangle.fill` | red |
| `waiting_for_input` | Needs input | `questionmark.circle.fill` | orange |
| `working` / `tool_running` / `long_task_progress` | Working / Running / Long task | `circle.dotted`, pulsing | secondary label |
| `completed` | Finished | `checkmark` | secondary label |
| `idle_ready` | Idle | `minus` | secondary label |
| finished, not yet opened | — | a tint dot at the leading edge, as Mail marks unread | app tint |

Usage meters are 3 pt bars in the label colour, orange from 80 %, red from
95 %; percentages are right-aligned monospaced digits.

Session emoji come from the daemon and stay in the data. They are shown in
their own 26 pt neutral tile at the leading edge so titles remain pure text and
the columns line up, and **Settings › Appearance › Show session emoji** turns
them off.

## Type, space, motion

- Dynamic Type throughout; the row glyph is a `@ScaledMetric` so it grows with
  the text. Nothing is laid out at a fixed width except 12–34 pt glyphs.
- One rhythm: 12 pt inside cards, 16 pt corner radius on cards, 10 pt on the
  controls inside them, so the curves stay concentric with the hardware's.
- Motion is state, never decoration: `symbolEffect(.pulse)` on a session that is
  still moving, `.variableColor` on the live connection pill, `numericText` on
  counters. Every one is switched off under Reduce Motion.

## Toolchains

Deployment floor is iOS 27.0 (watchOS 26.0), so ordinary modern API needs no
guard. The iPhone Duo's own API is a different problem: it exists only in the
**iOS 27.1 SDK**, and `#available` guards runtime, not compilation — code that
merely *mentions* `ArrangementView`, `reservedRegions`, `onHingeChange`,
`toolbarVertical*` or `ToolbarOverflowMenu` fails to build against the released
Xcode that TestFlight builds use while 27.1 is in beta.

Every one of those symbols is therefore wrapped twice, and only inside
`DuoLayout.swift`:

```swift
#if canImport(SwiftUI, _version: 8.0.85)   // 8.0.84 = iOS 27.0 SDK, 8.0.85 = 27.1
    if #available(iOS 27.1, *) { … }        // runtime
    else { fallback }
#else
    fallback
#endif
```

Call sites see only `duoFold`, `duoSplit`, `duoCompactTitle`,
`duoPrefersToolbarItems`, `duoHorizontalSheetBar` and `DuoOverflow`, so the app
compiles on Xcode 26.6, 27.0 and 27.1 with no `#if` anywhere else. Fallbacks are
deliberately ordinary — a stacked or side-by-side `HStack`/`VStack`, a plain
`Menu` for the overflow — never a second design. The widget's
`isDynamicIslandLimitedInWidth` is iOS 27.0 API and needs no gate.

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
| 4 | "Follow the system's vertical layout for toolbars, tab bars, and navigation controls." | Settled: Apple's default vertical strip, kept light — no Back button (the board is the root), the attention bell only while something needs attention, New session, and one system overflow. `toolbarVerticalBehavior(.disabled)` survives only on the two single-button sheets, which is Apple's own recommendation. | `01-board__outer__emoji-on.png` |
| 5 | "Adapt your layout when the device folds. Prefer a layout container that adapts automatically … In a grid-style layout, prefer an even number of columns so content divides cleanly." | `ArrangementView(.split)` for both panes; the pattern grid takes its column count from `duoColumnCount`, which rounds down to an even number whenever a division region exists (`[.includeInactive]`). | `01-board__half__A.png`, `02-dot__inner-land__A.png` |
| 6 | "Avoid extreme layout changes as people fold the device. Move only what's necessary." | Folding moves the panes onto the two halves; nothing is rearranged. The only pose-specific layout is tabletop, and it keeps the same content in the same order. | `01-board__inner-land__A.png` vs `01-board__half__A.png` |
| 7 | "Keep navigation outside of arrangement views." | The single `NavigationStack` wraps every `DuoSplit`; no arrangement view contains a navigation container. | `ContentView.swift` |
| 8 | "Reserve the top of the vertical axis for primary navigation controls … followed by prominent actions." | The board is the root, so there is no Back button at all; the pinned trailing slot holds the attention item, then New session, then the system overflow. | `01-board__outer__A.png` |
| 9 | "Prioritize frequently used toolbar items … keep controls that convey important status, like items with badges, visible longer." | The badged attention item is `.topBarPinnedTrailing` with `visibilityPriority(.high)`; New session is high; everything else lives in the overflow. | `01-board__outer__A.png` |
| 10 | "In general, don't override the default bar placement." | Not overridden anywhere; the debug switch that could has been removed. | `04-token__outer__A.png` |
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

### Fold depth

`reservedRegions(kind: .division)` reports the crease as **inactive** until the
phone is folded far enough for the system to divide the display. A shallow
fold therefore keeps the ordinary layout, and that is deliberate: the tabletop
board is for a phone that is actually standing on a desk. What the ordinary
layout must never do is put something unreadable in the crease — it does not,
because the panes are lists that scroll and the board's banner only exists in
the tabletop layout, where the crease is active by definition.

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
