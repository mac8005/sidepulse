# AGENTS.md — orientation for agents working on sidepulse

`sidepulse` is an LED status indicator for macOS that displays agent (Codex, Claude, Grok) status, battery level, and other system signals. The project includes:
- **LED firmware + protocol** (`LEDS_FORMAT.md`)
- **macOS menu-bar app** (`status_bar.py`, `live_activity.py`)
- **CLI tools** (`cli.py`)
- **Agent monitoring system** (collector, providers)
- **Admin UI backend** (business analytics, export pipeline)

## Project Structure

```
src/sidepulse/
  collector.py          — collect agent status from logs and APIs
  providers.py          — detect + parse Codex/Claude/Grok configs
  models.py             — data models (AgentStatus, HookEvent, etc.)
  live_activity.py      — iOS/macOS Live Activity push integration
  status_bar.py         — menu-bar app UI and state management
  cli.py                — command-line interface
  settings.py           — user settings (device path, preferences)
  battery.py            — battery status polling
  install.py            — installation/setup
  ...                   — device control, session actions, etc.

tests/                  — test suite (no pytest; run test_*.py directly)
ios/SidePulse/          — iOS companion app (SwiftUI)
```

## Admin UI & Business Features

The collector and a new marketing daemon fetch data for an admin dashboard showing:
- Agent session metrics (counts, duration, tool usage)
- System health (battery, CPU, uptime)
- Performance analytics
- Export data for business reporting

Access paths are documented below so future sessions can retrieve credentials and configure the APIs.

## Service Access & Credentials

All services use **macOS Keychain** for secrets. Environment variables are preferred for non-sensitive config (file paths, API endpoints).

### Supabase (Database + Analytics)

**Purpose:** Store agent metrics, session data, and business analytics.

**Credentials in Keychain:**
```bash
security find-generic-password -a sidepulse -s supabase-url -w
security find-generic-password -a sidepulse -s supabase-anon-key -w
security find-generic-password -a sidepulse -s supabase-service-key -w
```

**Environment Setup** (in collector/daemon):
```bash
export SUPABASE_URL=$(security find-generic-password -a sidepulse -s supabase-url -w)
export SUPABASE_ANON_KEY=$(security find-generic-password -a sidepulse -s supabase-anon-key -w)
export SUPABASE_SERVICE_KEY=$(security find-generic-password -a sidepulse -s supabase-service-key -w)
```

**Usage in Code:**
```python
from supabase import create_client, Client

url = os.environ["SUPABASE_URL"]
key = os.environ["SUPABASE_ANON_KEY"]
supabase: Client = create_client(url, key)

# Example: Insert session metrics
supabase.table("session_metrics").insert({
    "session_id": "...",
    "provider": "codex",
    "duration_seconds": 1234,
    "tool_count": 5,
    "timestamp": datetime.utcnow().isoformat(),
}).execute()
```

**Database Tables** (for reference):
- `session_metrics` — aggregated session data (session_id, provider, duration, tool_count, timestamp)
- `agent_events` — detailed hook events (provider, event_name, session_id, timestamp, payload)
- `system_health` — battery, CPU, memory snapshots

### Apple Ads API

**Purpose:** Fetch marketing performance data (impressions, clicks, conversions) for admin dashboard.

**Credentials in Keychain:**
```bash
security find-generic-password -a sidepulse -s apple-ads-api-key -w
security find-generic-password -a sidepulse -s apple-ads-api-secret -w
security find-generic-password -a sidepulse -s apple-ads-org-id -w
```

**Environment Setup:**
```bash
export APPLE_ADS_API_KEY=$(security find-generic-password -a sidepulse -s apple-ads-api-key -w)
export APPLE_ADS_API_SECRET=$(security find-generic-password -a sidepulse -s apple-ads-api-secret -w)
export APPLE_ADS_ORG_ID=$(security find-generic-password -a sidepulse -s apple-ads-org-id -w)
```

**Usage** (via generated client):
```python
from sidepulse.clients.apple_ads import AppleAdsClient

client = AppleAdsClient(
    api_key=os.environ["APPLE_ADS_API_KEY"],
    api_secret=os.environ["APPLE_ADS_API_SECRET"],
    org_id=os.environ["APPLE_ADS_ORG_ID"],
)

# Fetch campaign data
campaigns = client.get_campaigns()
for campaign in campaigns:
    print(f"{campaign.name}: {campaign.impressions} impressions")
```

**Client Location:** `src/sidepulse/clients/apple_ads.py` (auto-generated from OpenAPI spec)

### Additional Services (Placeholder)

**[Service Name TBD]**
- **Credentials:** `security find-generic-password -a sidepulse -s [service-name] -w`
- **Purpose:** [Description]
- **Usage:** [Code example]

## Collector & Marketing Daemon

### Collector (`src/sidepulse/collector.py`)

Polls agent status from:
- Hook event logs (Codex, Claude, Grok)
- iOS Live Activity heartbeat
- System stats (battery, CPU, network)

Exports data to Supabase for business analytics.

**Running Collector (Manual):**
```bash
python3 -c "from sidepulse.collector import MonitorSnapshot; ..."
```

**Collector in Daemon (Background):**
The `marketing_daemon.py` (see below) runs the collector on a schedule.

### Marketing Daemon (`src/sidepulse/marketing_daemon.py`)

**Purpose:** Autonomous background process that:
1. Runs the collector every 5 minutes
2. Fetches Apple Ads data hourly
3. Aggregates metrics and pushes to Supabase
4. Exports reports for the admin UI
5. Handles errors with retry logic and logging

**Installation & Running:**
```bash
# Set up launchd service (macOS)
launchctl load ~/Library/LaunchAgents/ch.cerqui.sidepulse-marketing-daemon.plist

# View logs
tail -f ~/Library/Logs/sidepulse-marketing-daemon.log

# Manually trigger export
python3 -m sidepulse.marketing_daemon --export-now
```

**Config** (`~/.sidepulse/marketing-daemon.json`):
```json
{
  "enabled": true,
  "collector_interval_minutes": 5,
  "apple_ads_interval_minutes": 60,
  "supabase_batch_size": 100,
  "log_file": "~/Library/Logs/sidepulse-marketing-daemon.log",
  "export_path": "~/sidepulse-exports"
}
```

## Setting Up Credentials

**First-time setup:**
```bash
# Store Supabase credentials
security add-generic-password -a sidepulse -s supabase-url -w "https://your-project.supabase.co"
security add-generic-password -a sidepulse -s supabase-anon-key -w "your-anon-key"
security add-generic-password -a sidepulse -s supabase-service-key -w "your-service-key"

# Store Apple Ads credentials
security add-generic-password -a sidepulse -s apple-ads-api-key -w "your-api-key"
security add-generic-password -a sidepulse -s apple-ads-api-secret -w "your-api-secret"
security add-generic-password -a sidepulse -s apple-ads-org-id -w "your-org-id"
```

**Verify credentials are stored:**
```bash
security find-generic-password -a sidepulse
```

## Data Export & Admin UI

**Export Format:**
The daemon exports collected data as JSON to `~/sidepulse-exports/`:
```
~/sidepulse-exports/
  sessions-2026-08-27.json       — session metrics
  events-2026-08-27.json         — detailed hook events
  system-health-2026-08-27.json  — battery/system stats
  ads-2026-08-27.json            — Apple Ads campaign data
```

**Admin UI Integration:**
The admin UI ingests these exports and renders:
- Session activity timeline
- Provider performance breakdown (Codex vs Claude vs Grok)
- System health trends
- Ad campaign ROI metrics

**Triggering Export Manually:**
```bash
python3 -m sidepulse.marketing_daemon --export-now
```

## Conventions

- **No AI attribution:** Commits, PRs, and code must appear solely under the user's name.
- **Commit style:** `collector: …`, `daemon: …`, `admin-ui: …` etc.
- **Secrets:** NEVER commit `.env` files or hardcoded keys. Use macOS Keychain + environment variables only.
- **Testing:** Run `python3 tests/test_*.py` directly. No pytest setup required.
- **Code compilation:** After edits, run `python3 -m py_compile src/sidepulse/<file>.py` to check syntax.

## Troubleshooting

**Collector fails to connect to Supabase:**
- Verify credentials: `security find-generic-password -a sidepulse -s supabase-url -w`
- Check network connectivity: `curl $SUPABASE_URL`
- Review collector logs in daemon output

**Apple Ads API returns 401:**
- Verify API credentials in Keychain
- Ensure `APPLE_ADS_ORG_ID` matches the organization you're querying
- Check API key expiration with Apple

**Daemon won't start (launchd error):**
- Check log file: `~/Library/Logs/sidepulse-marketing-daemon.log`
- Verify Python path: `which python3`
- Ensure launchd plist has correct permissions (644)

## References

- [Supabase Python Client](https://github.com/supabase/supabase-py)
- [Apple Ads API Documentation](https://developers.apple.com/app-store-connect/apple-ads/)
- [macOS Keychain CLI](https://linux.die.net/man/1/security)
- Live Activity integration: `src/sidepulse/live_activity.py`
