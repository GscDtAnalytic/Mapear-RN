"""Async HTTP client for Apify REST API v2.

Why a thin wrapper instead of ``apify-client``: the official package
drags synchronous IO and a heavy dep tree we'd only use for three
endpoints. ``httpx.AsyncClient`` + ``retry_on_network_error`` already
handles everything we need (429/5xx back-off, connection reuse, timeouts).

Endpoints used:
  - POST /v2/acts/{actor_id}/runs         — start an actor run
  - GET  /v2/actor-runs/{run_id}          — poll status
  - GET  /v2/datasets/{dataset_id}/items  — paginated dataset fetch

Error taxonomy (maps 1-1 to the plan's DLQ error_type column):
  - ``ActorRunFailed``     — run reached a terminal FAILED/ABORTED status
  - ``ActorRunTimeout``    — polling exceeded ``poll_timeout_seconds``
  - ``ActorRateLimited``   — Apify returned 429 after retries
  - ``ActorPayloadError``  — 4xx other than 429 (input rejected by actor)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger

from mapear_infra.retry import retry_on_network_error

APIFY_BASE_URL = "https://api.apify.com"


class ApifyError(Exception):
    """Base class for Apify-specific failures.

    `status_code` é populado quando a falha originou de uma resposta HTTP do
    Apify (4xx); fica `None` para falhas de polling, timeout ou rate-limit
    pós-retry. Permite que callers façam logging estruturado e alertas
    filtrarem por código (ex.: A-06 filtra status_code=401).
    """

    def __init__(self, message: str = "", *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ActorRunFailed(ApifyError):  # noqa: N818 - public API name, see module docs
    """Run reached a terminal FAILED / ABORTED / TIMED-OUT status."""


class ActorRunTimeout(ApifyError):  # noqa: N818 - public API name, see module docs
    """Polling exceeded ``poll_timeout_seconds`` without reaching a terminal state."""


class ActorRateLimited(ApifyError):  # noqa: N818 - public API name, see module docs
    """Apify returned 429 after retries exhausted."""


class ActorPayloadError(ApifyError):
    """Actor rejected the input (4xx other than 429)."""


_TERMINAL_SUCCESS = {"SUCCEEDED"}
_TERMINAL_FAILURE = {"FAILED", "ABORTED", "TIMED-OUT", "TIMEOUT"}


@dataclass(frozen=True)
class ActorRun:
    """Materialized view of an Apify run response."""

    run_id: str
    status: str
    dataset_id: str
    started_at: str | None
    finished_at: str | None
    status_message: str | None = None

    @classmethod
    def from_response(cls, data: dict[str, Any]) -> ActorRun:
        return cls(
            run_id=data["id"],
            status=data["status"],
            dataset_id=data["defaultDatasetId"],
            started_at=data.get("startedAt"),
            finished_at=data.get("finishedAt"),
            status_message=data.get("statusMessage"),
        )


class ApifyClient:
    """Minimal async client for Apify API v2.

    Instantiate once per pipeline run — the underlying httpx.AsyncClient
    pools connections across start/poll/fetch calls.
    """

    def __init__(
        self,
        token: str,
        *,
        base_url: str = APIFY_BASE_URL,
        actor_timeout_seconds: int = 900,
        poll_timeout_seconds: int = 1200,
        poll_initial_interval_seconds: float = 3.0,
        dataset_page_size: int = 500,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not token:
            raise ValueError("Apify token is required")
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._actor_timeout = actor_timeout_seconds
        self._poll_timeout = poll_timeout_seconds
        self._poll_interval = poll_initial_interval_seconds
        self._page_size = dataset_page_size
        self._owned_client = http_client is None
        # Apify authenticates via ?token=... or Authorization header; the
        # header form keeps query strings clean in logs.
        self._client = http_client or httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=60.0),
        )

    async def __aenter__(self) -> ApifyClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owned_client:
            await self._client.aclose()

    @retry_on_network_error(max_attempts=3, min_wait=1.0, max_wait=30.0)
    async def _start_run(self, actor_id: str, payload: dict[str, Any]) -> ActorRun:
        """POST /v2/acts/{actor_id}/runs — returns immediately with run metadata."""
        resp = await self._client.post(
            f"/v2/acts/{actor_id}/runs",
            json=payload,
            params={"timeout": self._actor_timeout},
        )
        self._raise_for_apify(resp, context=f"start {actor_id}")
        data = resp.json()["data"]
        return ActorRun.from_response(data)

    @retry_on_network_error(max_attempts=5, min_wait=1.0, max_wait=30.0)
    async def _fetch_run(self, run_id: str) -> ActorRun:
        resp = await self._client.get(f"/v2/actor-runs/{run_id}")
        self._raise_for_apify(resp, context=f"poll {run_id}")
        return ActorRun.from_response(resp.json()["data"])

    async def _poll_until_terminal(self, run_id: str) -> ActorRun:
        """Poll an actor run until it reaches SUCCEEDED or FAILED/ABORTED/TIMED-OUT.

        Uses linear back-off capped at 30s: start small (3s) so short runs
        return quickly, but don't hammer Apify for long-tail FB scrapes
        that can take 5–10 minutes.
        """
        interval = self._poll_interval
        elapsed = 0.0
        while elapsed < self._poll_timeout:
            run = await self._fetch_run(run_id)
            status = run.status.upper()
            if status in _TERMINAL_SUCCESS:
                return run
            if status in _TERMINAL_FAILURE:
                detail = run.status_message or "(no statusMessage returned by Apify)"
                logger.error(
                    "Apify run {run_id} reached terminal failure: "
                    "status={status} finished_at={finished_at} statusMessage={msg}",
                    run_id=run_id,
                    status=run.status,
                    finished_at=run.finished_at or "unknown",
                    msg=detail,
                )
                raise ActorRunFailed(
                    f"run {run_id} ended with status={run.status}: {detail}"
                )
            await asyncio.sleep(interval)
            elapsed += interval
            interval = min(interval + 2.0, 30.0)
        raise ActorRunTimeout(
            f"run {run_id} did not reach a terminal state within "
            f"{self._poll_timeout}s (last status={run.status})"
        )

    @retry_on_network_error(max_attempts=3, min_wait=1.0, max_wait=30.0)
    async def _fetch_page(
        self, dataset_id: str, offset: int, limit: int
    ) -> list[dict[str, Any]]:
        resp = await self._client.get(
            f"/v2/datasets/{dataset_id}/items",
            params={
                "offset": offset,
                "limit": limit,
                "clean": "true",
                "format": "json",
            },
        )
        self._raise_for_apify(
            resp, context=f"fetch {dataset_id}[{offset}:{offset+limit}]"
        )
        payload = resp.json()
        # API may return either a raw array or {"data": {"items": [...]}} depending
        # on the actor's dataset output format. Normalize both to a flat list.
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict) and "data" in payload:
            data = payload["data"]
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and "items" in data:
                return data["items"]
        return []

    async def _fetch_all_items(self, dataset_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = await self._fetch_page(dataset_id, offset, self._page_size)
            if not page:
                break
            items.extend(page)
            if len(page) < self._page_size:
                break
            offset += self._page_size
        return items

    async def run_actor(
        self, actor_id: str, payload: dict[str, Any]
    ) -> tuple[ActorRun, list[dict[str, Any]]]:
        """Start an actor, poll until it terminates, return run + all dataset items.

        Raises one of ``ActorRunFailed``, ``ActorRunTimeout``,
        ``ActorRateLimited``, ``ActorPayloadError`` — the pipeline
        catches these to route failures to ``mapear_ops.dlq_social``.
        """
        logger.bind(actor_id=actor_id).info("Apify run starting")
        run = await self._start_run(actor_id, payload)
        logger.bind(actor_id=actor_id, actor_run_id=run.run_id).info(
            "Apify run started, polling"
        )
        terminal = await self._poll_until_terminal(run.run_id)
        items = await self._fetch_all_items(terminal.dataset_id)
        logger.bind(actor_id=actor_id, actor_run_id=terminal.run_id).info(
            "Apify run finished status={status} items={n}",
            status=terminal.status,
            n=len(items),
        )
        return terminal, items

    @staticmethod
    def _raise_for_apify(resp: httpx.Response, *, context: str) -> None:
        """Translate 4xx responses into the domain error taxonomy.

        5xx and 429 are re-raised as httpx.HTTPStatusError so
        ``retry_on_network_error`` can catch them; other 4xx escalate
        to ActorPayloadError (no retry — input is bad).
        """
        if resp.status_code == 429:
            # Let retry kick in; if retries exhaust, ApifyRateLimited surfaces
            # via the final HTTPStatusError the caller translates.
            resp.raise_for_status()
            return
        if 500 <= resp.status_code < 600:
            resp.raise_for_status()
            return
        if 400 <= resp.status_code < 500:
            try:
                err_body = resp.json().get("error", {})
                err_type = err_body.get("type", "")
                message = err_body.get("message") or resp.text
            except ValueError:
                err_type = ""
                message = resp.text
            detail = f"[{err_type}] {message}" if err_type else message
            raise ActorPayloadError(
                f"Apify {resp.status_code} on {context}: {detail}",
                status_code=resp.status_code,
            )
