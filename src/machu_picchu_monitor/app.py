from __future__ import annotations

import asyncio
import html
import logging
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .config import get_settings
from .monitor import MonitorService
from .observability import AVAILABILITY_GAUGE, setup_logging
from .storage import SQLiteStorage

logger = logging.getLogger(__name__)


def _format_dt(value: str | datetime | None) -> str:
    if value is None:
        return "Never"
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _e(value: Any) -> str:
    return html.escape(str(value))


def render_dashboard_fragment(storage: SQLiteStorage, monitor: MonitorService) -> str:
    current = storage.list_current()
    history = storage.list_history(limit=25)
    slots = storage.list_slot_current()
    thresholds = {
        (rule.visit_date.isoformat(), rule.route, rule.slot): rule.threshold
        for rule in monitor.rules
        if rule.type == "below_threshold"
    }
    latest_seen = storage.latest_seen_at()

    current_rows = "\n".join(
        f"""
        <tr>
          <td>{_e(row["visit_date"])}</td>
          <td><strong>{_e(row["route"])}</strong><span>{_e(row["route_name"])}</span></td>
          <td class="qty">{_e(row["availability"])}</td>
          <td>{_e(row["source"])}</td>
          <td>{_e(row["last_seen_at"])}</td>
        </tr>
        """
        for row in current
    ) or '<tr><td colspan="5" class="empty">No availability checks recorded yet.</td></tr>'

    history_rows = "\n".join(
        f"""
        <tr>
          <td>{_e(row["seen_at"])}</td>
          <td>{_e(row["visit_date"])}</td>
          <td><strong>{_e(row["route"])}</strong><span>{_e(row["route_name"])}</span></td>
          <td>{_e(row["old_availability"])}</td>
          <td class="qty">{_e(row["new_availability"])}</td>
          <td>{_e(row["source"])}</td>
        </tr>
        """
        for row in history
    ) or '<tr><td colspan="6" class="empty">No historical changes yet.</td></tr>'

    slot_rows = "\n".join(
        f"""
        <tr>
          <td>{_e(row["visit_date"])}</td>
          <td><strong>{_e(row["route"])}</strong><span>{_e(row["route_name"])}</span></td>
          <td>{_e(row["slot"])}</td>
          <td class="qty">{_e(row["available"])}</td>
          <td>{_e(row["capacity"])}</td>
          <td>{_e(thresholds.get((row["visit_date"], row["route"], row["slot"]), "—"))}</td>
          <td>{_e(row["last_seen_at"])}</td>
        </tr>
        """
        for row in slots
    ) or '<tr><td colspan="7" class="empty">No watched slots yet.</td></tr>'

    status = monitor.status
    status_class = "ok" if status.last_error is None else "bad"
    running = "Running" if status.running else "Idle"
    last_success = _format_dt(status.last_success_at)
    last_error = status.last_error or "None"

    return f"""
    <section class="status-grid">
      <div class="metric">
        <span>Monitor</span>
        <strong class="{status_class}">{running}</strong>
      </div>
      <div class="metric">
        <span>Last checked</span>
        <strong>{_e(latest_seen or "Never")}</strong>
      </div>
      <div class="metric">
        <span>Last success</span>
        <strong>{_e(last_success)}</strong>
      </div>
      <div class="metric">
        <span>Provider</span>
        <strong>{_e(status.last_provider or "None")}</strong>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2>Current Availability</h2>
        <form method="post" action="/api/run-once">
          <button type="submit">Run check</button>
        </form>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Date</th>
              <th>Route</th>
              <th>Available</th>
              <th>Source</th>
              <th>Last seen</th>
            </tr>
          </thead>
          <tbody>{current_rows}</tbody>
        </table>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2>Watched Slots (low-stock rules)</h2>
        <span class="muted">Alerts when available drops below threshold</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Date</th>
              <th>Route</th>
              <th>Slot</th>
              <th>Available</th>
              <th>Capacity</th>
              <th>Threshold</th>
              <th>Last seen</th>
            </tr>
          </thead>
          <tbody>{slot_rows}</tbody>
        </table>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2>Historical Changes</h2>
        <span class="muted">Last 25 changes</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Seen at</th>
              <th>Date</th>
              <th>Route</th>
              <th>Previous</th>
              <th>New</th>
              <th>Source</th>
            </tr>
          </thead>
          <tbody>{history_rows}</tbody>
        </table>
      </div>
    </section>

    <section class="panel thin">
      <h2>Last Error</h2>
      <pre>{_e(last_error)}</pre>
    </section>
    """


def render_page(monitor: MonitorService, fragment: str) -> str:
    targets = " · ".join(rule.name for rule in monitor.rules) or "No rules configured"
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Machu Picchu Availability Monitor</title>
        <style>
          :root {{
            color-scheme: light;
            --bg: #f6f7f9;
            --text: #17202a;
            --muted: #667085;
            --line: #d9dee7;
            --panel: #ffffff;
            --accent: #0f766e;
            --accent-dark: #115e59;
            --warn: #b42318;
            --gold: #b7791f;
          }}
          * {{ box-sizing: border-box; }}
          body {{
            margin: 0;
            background: var(--bg);
            color: var(--text);
            font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
              "Segoe UI", sans-serif;
            letter-spacing: 0;
          }}
          header {{
            background: #17202a;
            color: #fff;
            padding: 22px clamp(18px, 4vw, 42px);
          }}
          header h1 {{
            margin: 0 0 8px;
            font-size: clamp(1.4rem, 2vw, 2rem);
            line-height: 1.1;
          }}
          header p {{ margin: 0; color: #cbd5e1; font-size: .95rem; }}
          main {{
            width: min(1180px, calc(100vw - 28px));
            margin: 22px auto 44px;
          }}
          .status-grid {{
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 12px;
            margin-bottom: 16px;
          }}
          .metric, .panel {{
            background: var(--panel);
            border: 1px solid var(--line);
            border-radius: 8px;
          }}
          .metric {{ padding: 16px; min-width: 0; }}
          .metric span, .muted {{
            display: block;
            color: var(--muted);
            font-size: .8rem;
            margin-bottom: 6px;
          }}
          .metric strong {{
            display: block;
            font-size: 1rem;
            overflow-wrap: anywhere;
          }}
          .ok {{ color: var(--accent-dark); }}
          .bad {{ color: var(--warn); }}
          .panel {{ margin-top: 16px; overflow: hidden; }}
          .panel.thin {{ padding: 18px; }}
          .panel-head {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            padding: 16px 18px;
            border-bottom: 1px solid var(--line);
          }}
          h2 {{ margin: 0; font-size: 1rem; }}
          button {{
            border: 0;
            border-radius: 6px;
            background: var(--accent);
            color: #fff;
            font-weight: 700;
            padding: 9px 13px;
            cursor: pointer;
          }}
          button:hover {{ background: var(--accent-dark); }}
          .table-wrap {{ overflow-x: auto; }}
          table {{ width: 100%; border-collapse: collapse; min-width: 760px; }}
          th, td {{
            padding: 12px 14px;
            border-bottom: 1px solid var(--line);
            text-align: left;
            vertical-align: top;
            font-size: .9rem;
          }}
          th {{ color: #475467; background: #f9fafb; font-weight: 700; }}
          td span {{ display: block; color: var(--muted); margin-top: 3px; }}
          .qty {{ color: var(--gold); font-weight: 800; font-variant-numeric: tabular-nums; }}
          .empty {{ color: var(--muted); text-align: center; padding: 26px; }}
          pre {{
            margin: 10px 0 0;
            white-space: pre-wrap;
            overflow-wrap: anywhere;
            color: var(--muted);
          }}
          @media (max-width: 760px) {{
            .status-grid {{ grid-template-columns: 1fr 1fr; }}
            header {{ padding: 18px; }}
          }}
        </style>
      </head>
      <body>
        <header>
          <h1>Machu Picchu Availability Monitor</h1>
          <p>{_e(targets)}</p>
        </header>
        <main id="dashboard">{fragment}</main>
        <script>
          async function refreshDashboard() {{
            const response = await fetch('/partials/dashboard', {{ cache: 'no-store' }});
            if (response.ok) {{
              document.getElementById('dashboard').innerHTML = await response.text();
            }}
          }}
          setInterval(refreshDashboard, 60000);
        </script>
      </body>
    </html>
    """


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level)
    storage = SQLiteStorage(settings.sqlite_path)
    storage.init()
    for row in storage.list_current():
        AVAILABILITY_GAUGE.labels(
            visit_date=row["visit_date"],
            route=row["route"],
        ).set(row["availability"])
    monitor = MonitorService(settings, storage)
    task: asyncio.Task[None] | None = None

    app.state.settings = settings
    app.state.storage = storage
    app.state.monitor = monitor

    if settings.run_monitor_in_web:
        task = asyncio.create_task(monitor.run_forever())
        app.state.monitor_task = task

    try:
        yield
    finally:
        await monitor.stop()
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        storage.close()


def create_app() -> FastAPI:
    app = FastAPI(title="Machu Picchu Ticket Availability Monitor", lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        fragment = render_dashboard_fragment(request.app.state.storage, request.app.state.monitor)
        return HTMLResponse(render_page(request.app.state.monitor, fragment))

    @app.get("/partials/dashboard", response_class=HTMLResponse)
    async def dashboard_partial(request: Request) -> HTMLResponse:
        return HTMLResponse(
            render_dashboard_fragment(request.app.state.storage, request.app.state.monitor)
        )

    @app.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        storage: SQLiteStorage = request.app.state.storage
        monitor: MonitorService = request.app.state.monitor
        ok = storage.database_ok() and monitor.status.last_error is None
        return JSONResponse(
            status_code=200 if ok else 503,
            content={
                "ok": ok,
                "database": storage.database_ok(),
                "running": monitor.status.running,
                "last_success_at": _format_dt(monitor.status.last_success_at),
                "last_error": monitor.status.last_error,
                "consecutive_failures": monitor.status.consecutive_failures,
            },
        )

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/api/availability")
    async def availability(request: Request) -> dict[str, Any]:
        storage: SQLiteStorage = request.app.state.storage
        return {"routes": storage.list_current(), "slots": storage.list_slot_current()}

    @app.get("/api/history")
    async def history(request: Request, limit: int = 100) -> list[dict[str, Any]]:
        return request.app.state.storage.list_history(limit=max(1, min(limit, 500)))

    @app.post("/api/run-once")
    async def run_once(request: Request) -> JSONResponse:
        monitor: MonitorService = request.app.state.monitor
        alerts = await monitor.run_once()
        return JSONResponse({"ok": True, "alerts": alerts})

    return app


app = create_app()
