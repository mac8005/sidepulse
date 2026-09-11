# Status
Updated: 2026-09-11

## In flight
- Nothing mid-change. de891a6 pushed and deployed to Mini, installed source hash verified; 816 tests + 519 subtests passed. TestFlight 202609111122 is VALID and assigned to existing Personal Testing. Actual Island visibility and LED behavior await phone confirmation after installation/opening.

## Decisions
- Ignore the specific internal title-generation prompt without a transcript, retaining the classification for later helper events. Do not kill the app or filter ordinary sessions by model or age.
- Usage meters read Claude (Keychain OAuth) and Codex (`~/.codex/auth.json`, wham endpoints) directly; CodexBar CLI is fallback only — it hung for hours behind a Gatekeeper prompt after a cask upgrade (2026-09-06).
- Usage alerts: one warning per window at 90%, one reset alert when an armed window comes back; a window is "new" only when its reset time moves >10 min (Codex `reset_at` jitters ±1 s).
- Paseo "closed"/"initializing" are quiet states (daemon restart lists every agent as closed); idle history from a directory listing is never announced.
- Dynamic Island recovery: stale window 30 min, priority-10 keep-alive every ≤10 min while active, and current-activity stale reports trigger replacement (≥2 h apart). iOS now reports observed stale transitions immediately rather than waiting for foreground/reconcile. APNs acceptance and active reports do not prove Island visibility.
- Visible Dot completion/resume alerts require a changed LED program, not just a new completed session. The phone reports bounded hashes of its rendered states (including Show finished/custom appearance); unchanged attention/disabled overlays/already-ACKed output need no banner. Live Activity updates remain independent.
- Completed LED notifications wait 10 seconds; hold the matching silent push during this window so brief auto-continuations do not consume a visible completion/resume pair. Normal Live Activity delivery stays immediate.
- Push-to-start bursts stop after one retry (MAX_UNANSWERED_START_PUSHES=2): unanswered starts stacked three cards on 2026-09-06.
- Hook logs self-trim to 64 MB at 96 MB; tool_response/tool_input compacted to head 1 KB + tail 2 KB at write time (was 2.2 GB, 100 MB/day).
- Upstream policy (Massimo): cherry-pick small upstream fixes only; skip the animation framework, keep idle→off.

## Next
1. Install/open TestFlight 202609111122 once so the server receives the phone's rendered LED-state hashes; until then it uses logical-state fallback and immediate stale reporting is not yet on the phone. The server-side 10-second grace window already applies to old clients.
2. Watch that usage warnings fire once per window (first live cycle: Codex weekly reset 2026-09-07 05:41).
3. Air: `~/.local/state/sidepulse/agent-monitor/status-history.jsonl` is 292 MB (status-bar history is not covered by log_trim); decide whether to trim it like the hook logs.

## Gotchas
- TestFlight signing repaired with approval Sep 11: reused IOS_DISTRIBUTION 4STRQLBTSW, six ACTIVE SidePulse profiles, no certificate creation/revocation. Metadata lives in ~/.local/share/sidepulse/signing/signing.json; .testflight/deploy.sh reuses it. Dedicated keychain sidepulse-distribution-20260911.keychain-db uses login Keychain service io.sidepulse.distribution-signing-20260911/account release. Old keychain preserved (its saved password failed). Release 202609111122/source 2424bc3 artifacts: /private/tmp/sidepulse-release.JZ9m8j. All user keychain search-list changes were restored.
- Air has no `~/Git/sidepulse` checkout: build a wheel on Mini, copy it over SSH, force-reinstall it into `~/.local/share/sidepulse/venv`, then kickstart agentstatus/remotehosts. Installed from the same 041333d wheel as Mini/M1 on 2026-09-10, including the earlier unread-click fix. The Air's hooks are separate: they run from the editable pipx venv → `~/Git/sidepulse-feature` (remote `origin`, same branch), so hook-side changes (log trimming/compaction) need `git pull --ff-only origin mac8005/remote-host-monitoring` there; done 2026-09-10 20:05 (f428de4→d4ee412).
- M1 menu-bar app also runs a COPIED venv (`~/.local/share/sidepulse/venv`, launcher execs its python): `git pull` there is inert until `venv/bin/pip install --no-deps --force-reinstall ~/Git/sidepulse` + kickstart agentstatus/remotehosts. Cost a day on the unread-click fix.
- Mini venv is a COPIED install: deploy = `~/.local/share/sidepulse/venv/bin/pip install --no-deps --force-reinstall .` then `launchctl kickstart -k gui/501/io.sidepulse.live-activity` (and `io.sidepulse.paseo-monitor` when paseo_monitor.py changed). Hooks run from the source tree by path.
- A retained Lock Screen card with a missing Island can follow staleness; inspect phone state reports rather than inferring it from healthy `/health`. Sep 11 afternoon reports were active despite the user's missing-Island report, so its exact cause remains unverified. Replacement is a recovery action, not proof of cause.
- `AgentStatus.updated_at` keeps microseconds only; never compare it by equality to a daemon `finishedAt` float (that bug left clicked sessions unread in the Mac app).
- `ignoring retired activity token <current id>` in the log is benign: the app re-sent an older observation of the same activity.
- Full suite: `.venv/bin/python -m pytest -q tests` (not the repo root: `ios/SidePulse/tools/tests` needs fastapi). ruff is not installed; `uvx ruff check` works, the tree has pre-existing findings.
- Log files: `~/.local/state/sidepulse/agent-monitor/live-activity.{out,err}.log`; err log is full of benign `ConnectionResetError` from SSE clients.

## Log
- 2026-09-11: de891a6 pushed/deployed to Mini; installed hash + healthy daemon verified, completion grace 10 seconds. 816 tests + 519 subtests passed. TestFlight 202609111122 already VALID in Personal Testing; server-only grace required no rebuild.
- 2026-09-11: TestFlight 202609111122 verified VALID/Personal Testing. Added completion grace plus held silent push after a real five-second completion/resume pair; 51 focused regressions passed, final verification/deploy next.
- 2026-09-11 13:25: Signed build 202609111122 uploaded without errors; awaiting processing. Continued investigation of unchanged-Working notices; new phone appearance metadata not received yet.
- 2026-09-11: Authorized signing repair complete: reused 4STRQLBTSW, six verified replacement profiles, isolated SidePulse keychain, original keychain/search list preserved. No certificates revoked or created. TestFlight release proceeding.
- 2026-09-11 13:16: 88b1297 server deployed, 811 tests + 519 subtests and unsigned iOS Release passed. Replaced current activity; phone registered active. TestFlight preflight blocked by removed retained certificate, no signing assets changed or upload made. Release artifacts /private/tmp/sidepulse-release.Tmbg0A; build check /Volumes/MacMiniData/sidepulse-build-check.ZAixFk.
- 2026-09-11: Reviewed overlapping stopped-worker changes; reproduced three unnecessary-banner cases, implemented actual LED-program gating and immediate stale-state reports. Focused original regressions passed; full verification/release in progress.
- 2026-09-11: Island vanished again at 3.5 h; root cause = phone reported the activity stale at 06:27 (18 such reports in the log history). Shipped stale guard + stale-report replacement, deployed to the mini, replaced the live activity by hand.
- 2026-09-10 20:05: Air verified for the unread-click fix (launcher venv hashes match d4ee412, agents up since 19:57); hook checkout ~/Git/sidepulse-feature fast-forwarded, hook smoke test exit 0.
- 2026-09-10: 041333d pushed and deployed to Mini/M1/Air; phantom absent in live snapshot, all installed collector hashes match. Full suite: 804 tests + 519 subtests passed (33 existing warnings); no iOS rebuild required.
- 2026-09-10: Added title-helper filter and eight regression cases; focused tests and real log replay passed, rollout in progress.
- 2026-09-10: Traced "Repair corrupted Kleido marketing session" false Working row to an unfiltered ephemeral title helper; diagnosis only, runtime unchanged.
- 2026-09-10 16:48: M1 venv reinstalled — the 05:40 git pull had not reached the running app; fix now live there.
- 2026-09-10: Mac app "clicked finished session stays unread" fixed: exact-generation match now tolerates the datetime round trip (remote_state.match_status). Deployed to M1.
- 2026-09-10: Island-vanished report checked: daemon+phone healthy (activity 52967973 since 22:23 after the mini's 22:22 reboot); auto-replace due 05:53. Created this file.
- 2026-09-07: Usage-alert repeat storm fixed (reset_at jitter → 10-min tolerance), 1566d70.
