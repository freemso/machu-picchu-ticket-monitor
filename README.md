# Machu Picchu Ticket Availability Monitor

Availability-only monitor for the official Peru Ministry of Culture ticket system:

- `https://tuboleto.cultura.pe`
- `https://machupicchu.gob.pe`

It monitors configured dates and route priorities, stores current and historical availability in SQLite, and sends notifications only when availability changes from `0` to `>0` or increases.

This project does not automate purchases, CAPTCHA solving, browser fingerprint evasion, account login, or payments.

## How It Checks Availability

The direct provider mirrors public calls used by the official frontend:

1. `GET /visita/lugar-info?idLugar=llaqta_machupicchu` resolves official route IDs.
2. `POST /visita/consulta-horarios` is the **sole** availability source — it reflects tickets
   actually available to buy online for a given date and route.
3. The encrypted `data` field is decrypted using the AES/PBKDF2 parameters shipped in the public frontend bundle.

> **Note:** `POST /comunes/disponibilidad-actual` (and the `/disponibilidad/…` page Playwright
> scrapes) is the **on-site sales board** shown on the screens at the entrance. It does *not*
> represent online-purchasable inventory, so it is intentionally **not** used — not even as a
> fallback. A `0` from `consulta-horarios` means the tickets are not (yet) on sale online; the
> monitor alerts when that goes above `0`.

## Alert Rules

All alerting is driven by a declarative list of rules in **`rules.json`**. To monitor more,
add an object to that list — no code changes needed. Each rule is one of two types, and both
**alert on every run while the condition currently holds** (a recurring reminder):

| Field | Required | Notes |
|-------|----------|-------|
| `name` | optional | Label shown in logs / dashboard |
| `type` | yes | `available` (alert while >0) or `below_threshold` (alert while < threshold) |
| `date` | yes | Visit date, `YYYY-MM-DD` |
| `route` | yes | e.g. `2A`, `1C` (or `Circuit 2A`) |
| `slot` | optional | Time slot `HH:MM` (e.g. `08:00`); omit to watch the route total |
| `threshold` | `below_threshold` only | Alert while availability is below this number |

```jsonc
[
  // Alert on every run while route 2A has any online availability on 2026-08-19
  { "name": "2A available 08-19", "type": "available", "date": "2026-08-19", "route": "2A" },

  // Alert on every run while the 08:00 slot of route 1C is below 10 tickets, on 2026-08-19
  { "name": "1C 08:00 low stock", "type": "below_threshold",
    "date": "2026-08-19", "route": "1C", "slot": "08:00", "threshold": 10 }
]
```

Notes:
- Alerts are **state-based, not transition-based**: while the condition is true they fire each
  run (hourly), so you keep getting reminded until you act or the condition clears. (`increase`
  is accepted as a backwards-compatible alias for `available`.)
- A transient fetch error for a route is skipped that run; it's simply re-checked next run.
- You can override the file without a redeploy by setting the `ALERT_RULES` env var (or a GitHub
  Actions secret / repo variable) to the same JSON array.
- If `rules.json` is empty/absent, the monitor falls back to generating `available` rules from
  `TARGET_DATES` × `TARGET_ROUTES`.

## Quick Start

```bash
cp .env.example .env
poetry install
poetry run playwright install chromium
poetry run mp-monitor serve
```

Open `http://127.0.0.1:8000`.

Run a single check:

```bash
poetry run mp-monitor check
```

Send a test notification through the configured channel:

```bash
poetry run mp-monitor test-notification
```

Send a test notification through a specific channel:

```bash
poetry run mp-monitor test-notification --channel email
```

Run monitor without the web dashboard:

```bash
poetry run mp-monitor monitor
```

## Personal Config

`.env.example` already includes:

```bash
TARGET_DATES=2026-08-19,2026-08-20
TARGET_ROUTES=2A,2B,3A
PREFERRED_NOTIFICATION=telegram
BACKUP_NOTIFICATIONS=
```

Routes accept compact values such as `2A` or labels such as `Circuit 2A`.

## Notifications

Telegram:

```bash
TELEGRAM_BOT_TOKEN=123456:abc
TELEGRAM_CHAT_ID=123456789
PREFERRED_NOTIFICATION=telegram
```

Email:

```bash
PREFERRED_NOTIFICATION=email
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=you@example.com
SMTP_PASSWORD=secret
SMTP_FROM=you@example.com
SMTP_TO=you@example.com
SMTP_USE_TLS=true
```

Slack:

```bash
PREFERRED_NOTIFICATION=slack
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

Use `PREFERRED_NOTIFICATION=all` to send through every configured channel.
Use `BACKUP_NOTIFICATIONS=email` to keep Telegram primary and use email only if Telegram fails.

## Polling

Defaults:

- `POLL_INTERVAL_SECONDS=1800`
- `JITTER_SECONDS=300`
- Exponential backoff on transient failures
- Structured JSON logs to stdout

## Dashboard And APIs

- Dashboard: `/`
- Health: `/healthz`
- Metrics: `/metrics`
- Current availability JSON: `/api/availability`
- Historical changes JSON: `/api/history`
- Manual run: `POST /api/run-once`

## Docker

```bash
cp .env.example .env
docker compose up --build -d
```

The SQLite database is persisted in `./data`.

## Railway Cron Deployment

Railway is a good fit for the scheduled monitor because the service can run one check, exit,
and then be started again by Railway's cron scheduler.

The included `railway.toml` configures:

```toml
[build]
builder = "DOCKERFILE"
dockerfilePath = "Dockerfile"

[deploy]
startCommand = "mp-monitor check"
cronSchedule = "*/30 * * * *"
restartPolicyType = "NEVER"
```

Create one Railway service from this repository, then add these service variables:

```bash
TARGET_DATES=2026-08-19,2026-08-20
TARGET_ROUTES=2A,2B,3A
PREFERRED_NOTIFICATION=telegram
BACKUP_NOTIFICATIONS=
SQLITE_PATH=/app/data/availability.sqlite3
PROVIDER_MODE=api
LOG_LEVEL=INFO
RAILWAY_RUN_UID=0
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

Attach a Railway volume to the service with mount path:

```text
/app/data
```

The volume keeps SQLite state between runs so alerts only send when availability appears or
increases. `RAILWAY_RUN_UID=0` lets the process write to the root-owned Railway volume. For the
first cloud deploy, run the cron service manually once from Railway and check the logs for six
target checks across `2026-08-19` and `2026-08-20`.

## GitHub Actions

The included workflow runs:

- `ruff check`
- `pytest`
- Docker image build

For a deployment workflow, add repository secrets matching your `.env` values, then push the built image to your registry or deploy the compose file to your host. Keep notification credentials in GitHub Secrets, not in the repository.

## Project Structure

```text
.
├── .github/workflows/ci.yml
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── src/machu_picchu_monitor/
│   ├── app.py
│   ├── cli.py
│   ├── config.py
│   ├── models.py
│   ├── monitor.py
│   ├── notifications.py
│   ├── observability.py
│   ├── providers.py
│   ├── route_matching.py
│   └── storage.py
└── tests/
    ├── test_monitor.py
    ├── test_provider.py
    ├── test_route_matching.py
    └── test_storage.py
```

## Reliability Notes

If the official site changes:

- Direct API parsing errors are logged with structured fields.
- A failed route is skipped for that run (its last stored value is kept), so a transient
  error is never misread as a `0 -> >0` change. A run only fails if *every* query fails.
- Failed runs are recorded in SQLite, and the process keeps polling with exponential backoff.

`PROVIDER_MODE=auto` (default) and `PROVIDER_MODE=api` both use the official online API with no
automatic browser fallback. `PROVIDER_MODE=playwright` forces the (on-site board) browser
extractor — see the note under "How It Checks Availability"; it is not online availability.
