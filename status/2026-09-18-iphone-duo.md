# iPhone Duo support — SidePulse iOS (2026-09-18)

Request (Massimo, 2026-09-18): best-experience iPhone Duo support, tested in the Duo simulator, screenshots of design VARIANTS per screen sent for approval before anything is finalised. Nothing in this repo has been changed yet.

## State
- Xcode 27.1 beta (27A9269) is installed on the Mini's external volume (`/Volumes/MacMiniData/Developer/Xcode-27.1-beta/Xcode-27.1.0-beta.app`). `xcode-select` still points at Xcode 26.6 on purpose; use the beta via `DEVELOPER_DIR=<app>/Contents/Developer`, DerivedData under `/Volumes/MacMiniData/Developer/duo-dd/sidepulse`.
- Compile check 2026-09-18: `xcodebuild … -destination 'generic/platform=iOS Simulator' CODE_SIGNING_ALLOWED=NO build` with the beta → BUILD SUCCEEDED against iPhoneSimulator27.1.sdk, 7 warnings, none Duo-related. Building works without the simulator runtime, so implementation with compiler feedback is not blocked — only screenshots are.
- UNBLOCKED 2026-09-19: Massimo had the mini cleaned up and lifted all restrictions on the apps ("free to change everything, best iPhone Duo experience"). iOS 27.1 runtime installed, `iPhone Duo` simulator works in all poses (closed / open / half-folded / rotated). Shared brief + helper scripts (simulator lock, pose, per-display screenshots): `/Volumes/MacMiniData/Developer/duo-tools/` (start with BRIEF.md). Screenshots land in `/Volumes/MacMiniData/Developer/duo-shots/<app>/`. Implementation runs on a branch in a worktree under `/Volumes/MacMiniData/Developer/duo-worktrees/`; nothing ships before Massimo approved the screenshots.
- `.testflight/deploy.sh` builds locally: a Duo-ready TestFlight build means running it with the 27.1 toolchain (Apple: apps built with older Xcode do not extend under the status bar and camera on Duo).

## Duo facts (from Apple's simulator profile)
Outer display 466×678 pt, compact width, wider and much shorter than an iPhone 17 Pro (402×874); its front camera is always visible and expands into the Dynamic Island for Live Activities. Inner display 669×951 pt, regular width, native orientation landscape. On the outer display and on the inner display in landscape the system moves toolbar and navigation controls to a vertical strip on the side — only for system bars and toolbar items that have an icon.

## Audit (read-only agent pass, line numbers not individually re-checked)
Good: one system `NavigationStack` (`ContentView.swift:22`), no custom bars, no `safeAreaInset`, no `UIScreen` / idiom / orientation checks, no `GeometryReader`, nothing wider than 52 pt fixed, `TARGETED_DEVICE_FAMILY = "1,2"`, portrait + landscape allowed, no `UIRequiresFullScreen`. The cheapest of the four apps.

Ranked changes:
1. Root is a push stack pre-seeded with `[.agents]` (`ContentView.swift:11,22-49`): on the inner display the agents list would stretch over 669–951 pt. `NavigationSplitView` (Home / Agents / Settings) or `ArrangementView(.split)` with the list as primary and usage / Dot settings as secondary.
2. The three toolbar items do not qualify for the vertical strip: gear is an `Image` with only an accessibility label (`ContentView.swift:51-62`), the two sheets have text-only `Button("Done")` (`:338-342`, `:380-384`). Use `Label` with title + icon.
3. There is no session detail: rows deep-link out (`AgentsLiveView.swift:187-205`). A secondary pane (state, usage, links per session) is what would make the inner display worth opening — new scope, needs Massimo's yes.
4. Live Activity: row caps are phone-shaped constants — island `prefix(4)` with a "caps at 160pt" comment (`AgentLiveActivity.swift:88-103`), lock screen `prefix(5)` (`:411-445`), fixed `.system(size:)` fonts. Check both on the wider outer display; keep the leading glyph clear of the outer camera region.
5. One grid, `GridItem(.adaptive(minimum: 150))` (`ContentView.swift:253-262`): 3 columns at 466 pt. HIG prefers even counts across the fold.
6. No screenshot path: no launch arguments, no previews, no fixture data; every screen shows "Waiting for data…" without a live daemon. A DEBUG launch argument that loads a canned `/snapshot` is a prerequisite for Duo screenshots of the list, and the Live Activity needs a foreground `Activity.request` with fixture content.

## Design variants to screenshot
- Agents (outer): A = current list with system vertical bar; B = denser rows (the display is short: 678 pt).
- Agents (inner, open flat): A = list stretched; B = list + usage/Dot pane side by side; C = list + new session-detail pane.
- Half-folded tabletop: list on the upper half, "New session" + usage meters on the lower half (`ArrangementView(.overlay)` or `.split` limited to the vertical axis).
- Live Activity / Dynamic Island on the outer display: compact, expanded, lock screen with 4 vs 6 rows.

## Rules that apply
Public fork: no private details in code or status. Upstream policy unchanged (small cherry-picks only).
