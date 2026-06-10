# Machu Picchu Ticket Availability Monitor

Availability-only monitor for the official Peru Ministry of Culture ticket system:

- `https://tuboleto.cultura.pe`
- `https://machupicchu.gob.pe`

It monitors configured dates and route priorities, stores current and historical availability in SQLite, and sends notifications only when availability changes from `0` to `>0` or increases.

This project does not automate purchases, CAPTCHA solving, browser fingerprint evasion, account login, or payments.

## How It Checks Availability

The direct provider mirrors public calls used by the official frontend:

1. `GET /visita/lugar-info?idLugar=llaqta_machupicchu` resolves official route IDs.
2. `POST /visita/consulta-horarios` checks exact date and route availability.
3. The encrypted `data` field is decrypted using the AES/PBKDF2 parameters shipped in the public frontend bundle.
4. `POST /comunes/disponibilidad-actual` is used as a public day-board fallback.
5. Playwright navigates to `/disponibilidad/llaqta_machupicchu` only if direct HTTP fails.

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
- The monitor attempts the Playwright fallback in `PROVIDER_MODE=auto`.
- Failed runs are recorded in SQLite.
- The process keeps polling with exponential backoff.

Set `PROVIDER_MODE=api` to disable browser fallback or `PROVIDER_MODE=playwright` to force browser extraction.
