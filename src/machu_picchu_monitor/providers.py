from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
from collections.abc import Iterable, Sequence
from datetime import date
from typing import Any, Protocol

import httpx
from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .config import Settings
from .models import AvailabilityRecord, RouteMetadata, utcnow
from .observability import PROVIDER_FAILURES
from .route_matching import display_route, normalize_route_code, route_code_from_text

logger = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    pass


class AvailabilityProvider(Protocol):
    name: str

    async def fetch_availability(
        self,
        visit_dates: Sequence[date],
        routes: Sequence[str],
    ) -> list[AvailabilityRecord]:
        ...


class OfficialApiProvider:
    name = "official_api"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self._client = client
        self._owns_client = client is None
        self._route_catalog: dict[str, RouteMetadata] | None = None

    async def __aenter__(self) -> OfficialApiProvider:
        await self._ensure_client()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client and self._owns_client:
            await self._client.aclose()
        self._client = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.request_timeout_seconds),
                headers=self._headers(self.settings.place_url),
                follow_redirects=True,
            )
        return self._client

    def _headers(self, referer: str) -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": self.settings.official_site_base_url,
            "Referer": referer,
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
            ),
        }

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        referer: str,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        client = await self._ensure_client()
        url = f"{self.settings.official_api_base_url}{path}"
        last_error: Exception | None = None

        for attempt in range(1, self.settings.retry_attempts + 1):
            try:
                response = await client.request(
                    method,
                    url,
                    headers=self._headers(referer),
                    json=json_body,
                )
                if response.status_code >= 500:
                    raise ProviderError(
                        f"Official API returned {response.status_code}: {response.text}"
                    )
                response.raise_for_status()
                data = response.json()
                if isinstance(data, dict) and data.get("message") == "Forbidden":
                    raise ProviderError("Official API rejected the request as forbidden")
                return data
            except (httpx.HTTPError, ProviderError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt >= self.settings.retry_attempts:
                    break
                delay = min(
                    self.settings.retry_max_seconds,
                    self.settings.retry_base_seconds * (2 ** (attempt - 1)),
                )
                logger.warning(
                    "official_api_request_retry",
                    extra={"attempt": attempt, "path": path, "error": str(exc), "delay": delay},
                )
                await asyncio.sleep(delay)

        raise ProviderError(f"Official API request failed for {path}: {last_error}")

    async def _server_timestamp(self) -> str:
        payload = await self._request_json(
            "GET",
            "/comunes/tiempo-servidor",
            referer=self.settings.place_url,
        )
        try:
            return str(payload["tiempoServidor"])
        except (KeyError, TypeError) as exc:
            raise ProviderError(f"Unexpected server time payload: {payload!r}") from exc

    def _sign(self, timestamp: str) -> str:
        secret = self.settings.official_api_secret
        digest = hmac.new(
            secret.encode(),
            f"{secret}:{timestamp}".encode(),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("ascii")

    async def _signed_body(self, body: dict[str, Any]) -> dict[str, Any]:
        timestamp = await self._server_timestamp()
        return {**body, "code": self._sign(timestamp), "timestamp": timestamp}

    def _decrypt_data(self, encrypted_base64: str) -> Any:
        raw = base64.b64decode(encrypted_base64)
        iv, ciphertext = raw[:16], raw[16:]
        key = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=self.settings.encryption_key_length_words * 4,
            salt=self.settings.decrypt_security_salt.encode("utf-8"),
            iterations=self.settings.encryption_iterations,
        ).derive(self.settings.encryption_password.encode("utf-8"))

        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded_plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        plaintext = unpadder.update(padded_plaintext) + unpadder.finalize()
        return json.loads(plaintext.decode("utf-8"))

    async def _fetch_route_catalog(self) -> dict[str, RouteMetadata]:
        if self._route_catalog is not None:
            return self._route_catalog

        payload = await self._request_json(
            "GET",
            f"/visita/lugar-info?idLugar={self.settings.official_place_slug}",
            referer=self.settings.place_url,
        )
        try:
            circuitos = json.loads(payload["circuitos"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ProviderError("Could not parse official route catalog") from exc

        catalog: dict[str, RouteMetadata] = {}
        for circuit in circuitos:
            for route in circuit.get("rutas", []):
                name = route.get("snombre_corto") or route.get("nombre_ruta") or ""
                code = route_code_from_text(name)
                if not code:
                    continue
                catalog[code] = RouteMetadata(
                    code=code,
                    name=name,
                    circuit_id=int(circuit["nidcircuito"]),
                    route_id=int(route["nidruta"]),
                )
        self._route_catalog = catalog
        return catalog

    async def _fetch_day_board(self, visit_date: date) -> dict[str, AvailabilityRecord]:
        body = await self._signed_body(
            {
                "lugar": self.settings.official_place_slug,
                "fecha": visit_date.isoformat(),
                "punto": self.settings.official_point_of_sale,
            }
        )
        payload = await self._request_json(
            "POST",
            "/comunes/disponibilidad-actual",
            referer=self.settings.availability_url,
            json_body=body,
        )
        if isinstance(payload, dict) and payload.get("error"):
            raise ProviderError(f"Day-board endpoint error: {payload}")
        if not isinstance(payload, list):
            raise ProviderError(f"Unexpected day-board payload: {payload!r}")

        records: dict[str, AvailabilityRecord] = {}
        checked_at = utcnow()
        for row in payload:
            route_name = str(row.get("ruta") or "")
            code = route_code_from_text(route_name)
            if not code:
                continue
            quantity = int(row.get("ncupoActual") or row.get("ncupo_actual") or 0)
            records[code] = AvailabilityRecord(
                visit_date=visit_date,
                route=code,
                route_name=route_name,
                quantity=quantity,
                source="official_day_board_api",
                checked_at=checked_at,
                raw=row,
            )
        return records

    async def _fetch_online_route(
        self,
        visit_date: date,
        route: RouteMetadata,
    ) -> AvailabilityRecord:
        body = await self._signed_body(
            {
                "nidruta": route.route_id,
                "nidcircuito": route.circuit_id,
                "nidlugar": self.settings.official_place_slug,
                "df_inicio": visit_date.isoformat(),
                "valorPunto": 0,
                "token": "",
            }
        )
        payload = await self._request_json(
            "POST",
            "/visita/consulta-horarios",
            referer=self.settings.place_url,
            json_body=body,
        )
        if not isinstance(payload, dict):
            raise ProviderError(f"Unexpected horarios payload for {route.code}: {payload!r}")
        if payload.get("estado") is False:
            raise ProviderError(
                f"Horarios endpoint returned estado=false for {route.code}: {payload}"
            )

        encrypted = payload.get("data")
        rows: list[dict[str, Any]] = self._decrypt_data(str(encrypted)) if encrypted else []
        if not isinstance(rows, list):
            raise ProviderError(f"Unexpected decrypted horarios data for {route.code}: {rows!r}")

        quantity = sum(
            int(row.get("ncupo_actual") or row.get("ncupoActual") or 0)
            for row in rows
            if int(row.get("activa", 1)) == 1
        )
        return AvailabilityRecord(
            visit_date=visit_date,
            route=route.code,
            route_name=route.name,
            quantity=quantity,
            source="official_online_api",
            checked_at=utcnow(),
            raw={"horarios": rows},
        )

    async def fetch_availability(
        self,
        visit_dates: Sequence[date],
        routes: Sequence[str],
    ) -> list[AvailabilityRecord]:
        catalog = await self._fetch_route_catalog()
        wanted_routes = [normalize_route_code(route) for route in routes]
        day_board_cache: dict[date, dict[str, AvailabilityRecord]] = {}
        records: list[AvailabilityRecord] = []

        for visit_date in visit_dates:
            for route_code in wanted_routes:
                route_meta = catalog.get(route_code)
                if route_meta is None:
                    logger.warning("route_not_found_in_catalog", extra={"route": route_code})
                    records.append(
                        AvailabilityRecord(
                            visit_date=visit_date,
                            route=route_code,
                            route_name=display_route(route_code),
                            quantity=0,
                            source="official_catalog_missing",
                            checked_at=utcnow(),
                            raw={},
                        )
                    )
                    continue

                try:
                    records.append(await self._fetch_online_route(visit_date, route_meta))
                    continue
                except Exception as exc:
                    logger.warning(
                        "official_online_api_failed",
                        extra={
                            "visit_date": visit_date.isoformat(),
                            "route": route_code,
                            "error": str(exc),
                        },
                    )

                try:
                    if visit_date not in day_board_cache:
                        day_board_cache[visit_date] = await self._fetch_day_board(visit_date)
                    fallback = day_board_cache[visit_date].get(route_code)
                    if fallback is not None:
                        records.append(fallback)
                    else:
                        records.append(
                            AvailabilityRecord(
                                visit_date=visit_date,
                                route=route_code,
                                route_name=route_meta.name,
                                quantity=0,
                                source="official_day_board_missing",
                                checked_at=utcnow(),
                                raw={},
                            )
                        )
                except Exception as exc:
                    PROVIDER_FAILURES.labels(provider="official_api").inc()
                    raise ProviderError(
                        f"Official API failed for {visit_date.isoformat()} {route_code}: {exc}"
                    ) from exc

        return records


class PlaywrightProvider:
    name = "playwright"

    def __init__(self, settings: Settings):
        self.settings = settings

    async def fetch_availability(
        self,
        visit_dates: Sequence[date],
        routes: Sequence[str],
    ) -> list[AvailabilityRecord]:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ProviderError("Playwright is not installed") from exc

        wanted_routes = [normalize_route_code(route) for route in routes]
        records: list[AvailabilityRecord] = []
        captured_payloads: list[Any] = []

        async def capture_response(response: Any) -> None:
            if "/comunes/disponibilidad-actual" not in response.url:
                return
            try:
                captured_payloads.append(await response.json())
            except Exception as exc:
                logger.warning("playwright_response_parse_failed", extra={"error": str(exc)})

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=self.settings.playwright_headless)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
                )
            )
            page = await context.new_page()
            page.on("response", lambda response: asyncio.create_task(capture_response(response)))
            await page.goto(
                self.settings.availability_url,
                wait_until="networkidle",
                timeout=self.settings.playwright_timeout_ms,
            )

            for visit_date in visit_dates:
                captured_payloads.clear()
                date_input = page.locator("input#fecha")
                if await date_input.count():
                    await date_input.evaluate(
                        """
                        (element, value) => {
                          element.value = value;
                          element.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                        """,
                        visit_date.isoformat(),
                    )
                    await page.wait_for_timeout(2500)
                else:
                    await page.wait_for_timeout(1500)

                board_records = self._records_from_payloads(visit_date, captured_payloads)
                if not board_records:
                    board_records = await self._records_from_dom(page, visit_date)

                for route_code in wanted_routes:
                    records.append(
                        board_records.get(
                            route_code,
                            AvailabilityRecord(
                                visit_date=visit_date,
                                route=route_code,
                                route_name=display_route(route_code),
                                quantity=0,
                                source="playwright_missing",
                                checked_at=utcnow(),
                                raw={},
                            ),
                        )
                    )
            await context.close()
            await browser.close()

        return records

    def _records_from_payloads(
        self,
        visit_date: date,
        payloads: Iterable[Any],
    ) -> dict[str, AvailabilityRecord]:
        records: dict[str, AvailabilityRecord] = {}
        for payload in payloads:
            if not isinstance(payload, list):
                continue
            for row in payload:
                if not isinstance(row, dict):
                    continue
                route_name = str(row.get("ruta") or "")
                code = route_code_from_text(route_name)
                if not code:
                    continue
                records[code] = AvailabilityRecord(
                    visit_date=visit_date,
                    route=code,
                    route_name=route_name,
                    quantity=int(row.get("ncupoActual") or row.get("ncupo_actual") or 0),
                    source="playwright_network",
                    checked_at=utcnow(),
                    raw=row,
                )
        return records

    async def _records_from_dom(self, page: Any, visit_date: date) -> dict[str, AvailabilityRecord]:
        rows = await page.evaluate(
            """
            () => Array.from(document.querySelectorAll('body *'))
              .map((node) => node.innerText || '')
              .filter((text) => /Ruta\\s+[1-3]-[A-D]/i.test(text) && /DISPONIBLES/i.test(text))
            """
        )
        records: dict[str, AvailabilityRecord] = {}
        for text in rows:
            code = route_code_from_text(text)
            if not code:
                continue
            match = None
            for candidate in (
                r"DISPONIBLES\s+(\d+)",
                r"Disponibles:\s*(\d+)",
                r"DISPONIBLES\s*:\s*(\d+)",
            ):
                import re

                match = re.search(candidate, text, re.IGNORECASE)
                if match:
                    break
            quantity = int(match.group(1)) if match else 0
            records[code] = AvailabilityRecord(
                visit_date=visit_date,
                route=code,
                route_name=display_route(code),
                quantity=quantity,
                source="playwright_dom",
                checked_at=utcnow(),
                raw={"text": text},
            )
        return records


class AutoProvider:
    name = "auto"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.api = OfficialApiProvider(settings)
        self.browser = PlaywrightProvider(settings)
        self.last_provider: str | None = None

    async def fetch_availability(
        self,
        visit_dates: Sequence[date],
        routes: Sequence[str],
    ) -> list[AvailabilityRecord]:
        mode = self.settings.provider_mode.lower()
        if mode == "api":
            self.last_provider = self.api.name
            return await self.api.fetch_availability(visit_dates, routes)
        if mode == "playwright":
            self.last_provider = self.browser.name
            return await self.browser.fetch_availability(visit_dates, routes)

        try:
            records = await self.api.fetch_availability(visit_dates, routes)
            self.last_provider = self.api.name
            return records
        except Exception as exc:
            PROVIDER_FAILURES.labels(provider="official_api").inc()
            logger.warning("official_api_failed_using_playwright", extra={"error": str(exc)})
            records = await self.browser.fetch_availability(visit_dates, routes)
            self.last_provider = self.browser.name
            return records

    async def aclose(self) -> None:
        await self.api.aclose()
