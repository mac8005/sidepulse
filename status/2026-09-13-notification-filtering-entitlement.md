# Notification filtering entitlement — request

**2026-09-17 — Apple answered the case with boilerplate: "submit the form".**
The reply ignores the point made on 09-13 and lists the form's fields as "sample
answers". Form re-checked the same day while logged in as the Account Holder
(DOM dump, not a guess): unchanged. `request_reason` is still a mandatory radio
with exactly four statements (E2E encrypted messaging / earthquake critical
alerts / retract urgent alerts in an education platform / retract time-sensitive
alerts in a healthcare workflow) and `app_store_url` is still mandatory; SidePulse
is TestFlight-only. Submitting therefore still means asserting a category that is
not true, under the Account Holder identity of the team that also ships Kleido
and SwimInsights. Put to Massimo, who chose: submit with a plain disclaimer.

**Submitted 2026-09-17 ~17:50 — Request ID HY8SFJ3KR3** ("We'll review your
request and contact you soon"). First option ticked because the form forces one;
the first text field opens with "None of the four categories above applies …
selected only to be able to submit … not a messaging app and uses no encryption
… TestFlight only, so the App Store URL is not live … if the entitlement is
limited to the four listed categories, please simply decline." Text below is what
was sent. Confirmation screenshot (not in git, repo is public):
`~/.local/share/sidepulse/apple/2026-09-17-entitlement-request-HY8SFJ3KR3.png`.
Expect 2–3+ weeks and most likely a refusal.

Same day the need shrank: the daemon now sends the visible push only while a Dot
is known to be plugged into the phone (commit 1c7d7c9). Without a Dot there is
no notification at all; with one it is a passive, list-only entry.

### Answers as submitted
- `app_name`: SidePulse Monitor
- `app_store_url`: https://apps.apple.com/app/id6804387957 (not live; said so in the text)
- `app_id`: 6804387957
- `bundle_id`: com.massimo.sidepulse
- `request_reason`: first option, with the disclaimer above
- `reason_why_not_adequate`: SidePulse mirrors the status of coding agents
  running on my Mac onto a small USB-C LED accessory (SidePulse Dot) plugged into
  the iPhone. Only code running on the phone can rewrite the LED. In the
  background there are two ways to run that code: background pushes
  (content-available), which iOS throttles to a few per hour and often does not
  deliver, so the LED falls out of sync; and an alert push with mutable-content
  handled by a Notification Service Extension, which is delivered reliably but
  must then display a notification. Live Activity pushes are reliable but give
  no code execution. So the app has to show a notification whose only purpose
  was to wake the extension.
- `why_no_visible_notification`: The notification carries no information. The
  same event (a session finished or resumed) is already shown by the app's Live
  Activity on the Lock Screen and in the Dynamic Island, updated by its own
  ActivityKit push at the same moment. The push only wakes the extension so it
  can write the new LED state to the accessory. It is delivered at the passive
  interruption level today, but still adds a Notification Center entry for
  every LED change. With the entitlement the extension would write the LED and
  deliver empty content. The server sends these pushes only while the accessory
  is plugged into the phone.
- `resources_needed`: One GET to the user's own Mac (local network or the
  user's VPN) for the current LED command (<1 KB JSON, 5 s timeout), one file
  write of a few hundred bytes to the accessory's USB volume through a
  security-scoped bookmark granted in the app, one small POST to acknowledge.
  Typically 1–3 s, hard cap 20 s. No location, media, third-party servers,
  user-generated or encrypted content.
- `how_often_runs`: Only when the state shown on the LED changes because an
  agent session finished or resumed: about 20–60 times a day on one device
  (measured: ~420 in the first 10 days), at most one per state change, with a
  60-second settle before a "finished" push. Nothing is sent while the app is
  in the foreground, during Do Not Disturb or Focus, or with no accessory
  attached.

**2026-09-13 20:11 — request sent as a reply in Developer Support case 102956095213**
(from massimo@cerqui.ch, the mailbox apple@cerqui.ch forwards to; copy in Gmail Sent).
The official form at developer.apple.com/contact/request/notification-service could
NOT be used: its mandatory "My app needs the entitlement because it" radio offers only
the four listed categories (E2E messaging, earthquake, education, healthcare) with no
"other", and its "App Store URL" field is mandatory too. Submitting it would have
required a false category. The reply asks Apple to route the request or say no.
Next: wait for Apple's answer in that thread.


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
