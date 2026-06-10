from __future__ import annotations

import asyncio
import html
import logging
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .config import get_settings
from .monitor import MonitorService
from .observability import AVAILABILITY_GAUGE, setup_logging
from .route_matching import display_route
from .storage import SQLiteStorage

logger = logging.getLogger(__name__)


def _format_dt(value: str | datetime | None) -> str:
    if value is None:
        return "Never"
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _fmt_ts(value: str | datetime | None) -> str:
    """Short local-time stamp for display, e.g. 'Jun 10 14:30'."""
    if value is None:
        return "never"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    return value.astimezone().strftime("%b %d %H:%M")


def _e(value: Any) -> str:
    return html.escape(str(value))


def render_dashboard_fragment(storage: SQLiteStorage, monitor: MonitorService) -> str:
    rules = monitor.rules
    watched_keys = {(rule.visit_date.isoformat(), rule.route) for rule in rules}
    dates = sorted({rule.visit_date.isoformat() for rule in rules})
    current = {(row["visit_date"], row["route"]): row for row in storage.list_current()}
    slots: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in storage.list_slot_current():
        slots.setdefault((row["visit_date"], row["route"]), []).append(row)

    alert_desc: dict[tuple[str, str], list[str]] = {}
    for rule in rules:
        key = (rule.visit_date.isoformat(), rule.route)
        if rule.type == "increase":
            alert_desc.setdefault(key, []).append("on increase")
        elif rule.type == "below_threshold":
            where = f" @ {rule.slot[:5]}" if rule.slot else " total"
            alert_desc.setdefault(key, []).append(f"< {rule.threshold}{where}")

    date_sections: list[str] = []
    for visit_date in dates:
        routes = sorted(
            {rule.route for rule in rules if rule.visit_date.isoformat() == visit_date}
        )
        weekday = date.fromisoformat(visit_date).strftime("%A")
        body_rows: list[str] = []
        for route in routes:
            key = (visit_date, route)
            row = current.get(key)
            qty = None if row is None else int(row["availability"])
            name = row["route_name"] if row else display_route(route)
            seen = _fmt_ts(row["last_seen_at"]) if row else "never"

            if qty is None:
                qty_cell = '<td class="zero">—</td>'
            elif qty > 0:
                qty_cell = f'<td class="qty">{qty}</td>'
            else:
                qty_cell = '<td class="zero">0</td>'

            slot_bits = []
            for slot_row in slots.get(key, []):
                cap = f"/{slot_row['capacity']}" if slot_row["capacity"] else ""
                slot_bits.append(
                    f"{str(slot_row['slot'])[:5]} → {slot_row['available']}{cap}"
                )

            body_rows.append(
                f"""
                <tr>
                  <td><strong>{_e(route)}</strong><span>{_e(name)}</span></td>
                  {qty_cell}
                  <td>{_e(", ".join(slot_bits) or "—")}</td>
                  <td>{_e("; ".join(alert_desc.get(key, [])) or "—")}</td>
                  <td>{_e(seen)}</td>
                </tr>
                """
            )

        date_sections.append(
            f"""
            <section class="panel">
              <div class="panel-head">
                <h2>{_e(visit_date)} <span class="muted-inline">{_e(weekday)}</span></h2>
              </div>
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Route</th>
                      <th>Available online</th>
                      <th>Watched slots</th>
                      <th>Alert rules</th>
                      <th>Last seen</th>
                    </tr>
                  </thead>
                  <tbody>{"".join(body_rows)}</tbody>
                </table>
              </div>
            </section>
            """
        )

    history = [
        row
        for row in storage.list_history(limit=100)
        if (row["visit_date"], row["route"]) in watched_keys
    ][:15]
    history_rows = "\n".join(
        f"""
        <tr>
          <td>{_e(_fmt_ts(row["seen_at"]))}</td>
          <td>{_e(row["visit_date"])}</td>
          <td><strong>{_e(row["route"])}</strong></td>
          <td>{_e("—" if row["old_availability"] is None else row["old_availability"])}
              → <strong>{_e(row["new_availability"])}</strong></td>
        </tr>
        """
        for row in history
    ) or '<tr><td colspan="4" class="empty">No changes recorded yet.</td></tr>'

    status = monitor.status
    status_class = "ok" if status.last_error is None else "bad"
    running = "Running" if status.running else "Idle"
    latest_seen = storage.latest_seen_at()
    last_error = status.last_error or "None"
    no_rules = (
        '<section class="panel thin"><p class="empty">No alert rules configured.</p></section>'
    )
    dates_html = "".join(date_sections) or no_rules

    return f"""
    <section class="status-grid">
      <div class="metric">
        <span>Monitor</span>
        <strong class="{status_class}">{running}</strong>
      </div>
      <div class="metric">
        <span>Last checked</span>
        <strong>{_e(_fmt_ts(latest_seen))}</strong>
      </div>
      <div class="metric">
        <span>Last success</span>
        <strong>{_e(_fmt_ts(status.last_success_at))}</strong>
      </div>
      <div class="metric">
        <span>Provider</span>
        <strong>{_e(status.last_provider or "None")}</strong>
      </div>
    </section>

    <div class="toolbar">
      <form method="post" action="/api/run-once" id="run-check-form">
        <span id="run-check-status" class="check-status"></span>
        <button type="submit" id="run-check-btn">Run check now</button>
      </form>
    </div>

    {dates_html}

    <section class="panel">
      <div class="panel-head">
        <h2>Recent Changes</h2>
        <span class="muted">Last 15 for monitored dates</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>When</th>
              <th>Date</th>
              <th>Route</th>
              <th>Change</th>
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
          .zero {{ color: var(--muted); font-variant-numeric: tabular-nums; }}
          .muted-inline {{ color: var(--muted); font-weight: 400; font-size: .85rem; }}
          .toolbar {{ display: flex; justify-content: flex-end; align-items: center;
            gap: 12px; margin-bottom: 4px; }}
          .toolbar form {{ display: flex; align-items: center; gap: 10px; }}
          button:disabled {{ opacity: .6; cursor: progress; }}
          .check-status {{ font-size: .85rem; }}
          .check-status.err {{ color: var(--warn); max-width: 60ch; overflow-wrap: anywhere; }}
          .check-status.ok {{ color: var(--accent-dark); }}
          .spinner {{ display: inline-block; width: 13px; height: 13px; vertical-align: -2px;
            margin-right: 6px; border: 2px solid var(--line); border-top-color: var(--accent);
            border-radius: 50%; animation: spin .7s linear infinite; }}
          @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
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
          let checking = false;
          async function refreshDashboard() {{
            if (checking) return;  // don't clobber the in-progress status line
            const response = await fetch('/partials/dashboard', {{ cache: 'no-store' }});
            if (response.ok) {{
              document.getElementById('dashboard').innerHTML = await response.text();
            }}
          }}
          // Delegated so it keeps working after the fragment is re-rendered.
          document.addEventListener('submit', async (event) => {{
            if (event.target.id !== 'run-check-form') return;
            event.preventDefault();
            const btn = document.getElementById('run-check-btn');
            const status = document.getElementById('run-check-status');
            checking = true;
            btn.disabled = true;
            status.className = 'check-status';
            status.innerHTML = '<span class="spinner"></span>Checking the official site…';
            try {{
              const res = await fetch('/api/run-once', {{
                method: 'POST',
                headers: {{ 'Accept': 'application/json' }},
              }});
              const data = await res.json().catch(() => ({{}}));
              if (res.ok && data.ok) {{
                status.className = 'check-status ok';
                const n = data.alerts ? ' · ' + data.alerts + ' alert(s)' : '';
                status.textContent = '✓ Updated' + n;
                checking = false;
                await refreshDashboard();
                setTimeout(() => {{
                  status.textContent = '';
                  status.className = 'check-status';
                }}, 4000);
              }} else {{
                status.className = 'check-status err';
                status.textContent = '✗ ' + (data.error || ('HTTP ' + res.status));
                checking = false;
              }}
            }} catch (err) {{
              status.className = 'check-status err';
              status.textContent = '✗ ' + err;
              checking = false;
            }} finally {{
              btn.disabled = false;
            }}
          }});
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
    async def run_once(request: Request) -> Response:
        monitor: MonitorService = request.app.state.monitor
        try:
            alerts = await monitor.run_once()
            ok, error = True, None
        except Exception as exc:
            # run_once already logged and recorded the failure; surface it gracefully
            ok, alerts, error = False, 0, str(exc)

        # The dashboard button is a plain HTML form: redirect back so the page
        # re-renders (errors appear in the "Last Error" panel) instead of a 5xx.
        if "text/html" in request.headers.get("accept", ""):
            return RedirectResponse(url="/", status_code=303)
        payload = {"ok": ok, "alerts": alerts}
        if error:
            payload["error"] = error
        return JSONResponse(payload, status_code=200 if ok else 502)

    return app


app = create_app()
