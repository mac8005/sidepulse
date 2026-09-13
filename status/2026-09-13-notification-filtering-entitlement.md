# Notification filtering entitlement — request draft

Apple Developer Support case 102956095213 (2026-09-13): the entitlement
`com.apple.developer.usernotifications.filtering` lets a notification service
extension receive a remote notification without displaying it. Apply through the
"Request Notification Service Entitlement" form from the primary developer
account (Apple Team ID UUN62LPZT6). Review is manual, 2–3 weeks or more, and
Apple lists only messaging, earthquake warning, education and enterprise
healthcare as approved categories, so expect a refusal; the form costs nothing.

Do NOT add the entitlement key to `SidePulse.entitlements` before it is granted:
the App Store provisioning profile would no longer match and the TestFlight
release would fail to sign.

## Form answers (draft, honest, no marketing)

**App name / bundle ID**
SidePulse — `com.massimo.sidepulse` (iOS). Personal-use utility distributed
through TestFlight only.

**What the app does**
SidePulse shows the live status of coding agents (Claude Code, Codex, Paseo)
running on a Mac: a Live Activity in the Dynamic Island and, when the SidePulse
Dot LED accessory is plugged into the iPhone, the same status as a colour on
that LED. A small daemon on the Mac sends the updates as push notifications.

**Why the entitlement is needed**
The LED can only be rewritten by code running on the phone. In the background
the only reliable way to run that code is a remote notification handled by a
notification service extension (`mutable-content`). Silent (`content-available`)
pushes are throttled and frequently not delivered, so the accessory falls out of
sync. Today every LED update therefore has to be delivered as a visible
notification ("Session finished", "Session resumed") that carries no information
for the user beyond what the Live Activity already shows. The extension writes
the LED and then has to display a notification nobody needs to read.

**How filtering would be used**
The extension would process the push (update the LED colour) and suppress the
notification entirely. No notification content is ever shown, stored or
analysed; the payload contains only an aggregate state ("working", "waiting",
"finished") and a command id. Nothing is encrypted or user-generated. The
entitlement would be used for this one accessory-update purpose only.

**Volume**
Roughly 20–100 pushes per day per device, at most one per state change, with a
one-minute settle before a "finished" state and a two-hour minimum between
alerts of the same kind.

**Fallback if not granted**
Notifications are delivered at the `passive` interruption level: no banner, no
sound, Notification Center list only (shipped 2026-09-13).

## When granted
1. Add `<key>com.apple.developer.usernotifications.filtering</key><true/>` to
   `ios/SidePulse/SidePulse/SidePulse.entitlements` and to the extension's
   entitlements, regenerate the provisioning profiles (`.testflight/repair-profiles.py`).
2. In `DotNotificationService/NotificationService.swift`, after the LED write,
   call `contentHandler(UNNotificationContent())` for Dot pushes instead of
   handing back the mutated content.
3. Drop the `alert` dictionary from the daemon's Dot payload (`_maybe_send_dot_completion_alert`);
   keep `mutable-content`.
