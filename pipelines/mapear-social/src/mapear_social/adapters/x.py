"""X (Twitter) adapter — X API v2 (App-Only Bearer Token).

Replaces the Apify scraper (ghSpYIW3L1RvT57NT) which was blocked and
returning noResults for all targets (2026-04-22).

Endpoints used:
    GET /2/users/by/username/{username}   — resolve handle → user_id + profile
    GET /2/users/{id}/tweets              — fetch recent tweets

Auth: App-Only Bearer Token via X_BEARER_TOKEN env var.

Rate limits (app-only):
    GET /2/users/by/username: 300 req / 15 min
    GET /2/users/:id/tweets:  1 500 tweets / 15 min

Tweet fields requested: id, text, created_at, public_metrics,
    referenced_tweets, lang.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from loguru import logger

from mapear_domain.entity_resolution import Target, validate_handle_format
from mapear_social.adapters.base import PlatformAdapter, SchemaDriftError
from mapear_social.models import Engagement, SocialAccount, SocialPost

_SCHEMA_VERSION = 1
_API_BASE = "https://api.x.com/2"
_DEFAULT_MAX_POSTS = 30  # per-user cap; X API allows up to 100


class _XApiError(Exception):
    """HTTP-level error from the X API v2 (non-recoverable for this run)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class _XAuthError(_XApiError):
    """HTTP 401/403 from X API — token expired/revoked or insufficient scope.

    Pipeline-level handling lifts this above DLQ noise so a 100%-auth-failure
    run aborts visibly instead of advancing the watermark with zero rows.
    """


class XAdapter(PlatformAdapter):
    """X API v2 adapter — direct API, no Apify intermediary.

    ``fetch_posts_via_api`` is the primary entry point; ``build_input``
    and ``parse_item`` are stubs kept for interface compliance but are
    never called when platform == "x" (pipeline branches before Apify).
    """

    actor_id = "x-api-v2"  # sentinel — never sent to Apify
    platform = "x"

    def __init__(self) -> None:
        self._token: str = os.environ.get("X_BEARER_TOKEN", "")

    @property
    def has_bearer_token(self) -> bool:
        return bool(self._token)

    def expected_schema_version(self) -> int:
        return _SCHEMA_VERSION

    def targets_with_handle(self, targets: list[Target]) -> list[Target]:
        """Keep only targets with an X handle that passes API format rules.

        X API rejects handles > 15 chars or with non-[A-Za-z0-9_] chars with
        HTTP 400, which would otherwise clog the DLQ on every run (see
        2026-04-24 DLQ audit). Pre-flight filter emits a loud warning so the
        bad row in ``rn_targets.csv`` is visible in pipeline logs.
        """
        kept: list[Target] = []
        for t in targets:
            if not t.x_handle:
                continue
            reason = validate_handle_format("x", t.x_handle)
            if reason is not None:
                logger.warning(
                    "Skipping invalid X handle for {pid}: {handle!r} ({reason}) "
                    "— fix rn_targets.csv",
                    pid=t.person_id,
                    handle=t.x_handle,
                    reason=reason,
                )
                continue
            kept.append(t)
        return kept

    def build_input(
        self,
        targets: list[Target],
        *,
        since: datetime | None = None,
    ) -> dict[str, Any]:
        # Stub — X API path bypasses Apify build_input entirely.
        # `since` is handled via ``fetch_posts_via_api(start_time=...)``.
        with_handle = self.targets_with_handle(targets)
        return {
            "twitterHandles": [t.x_handle.strip().lstrip("@") for t in with_handle],
        }

    def parse_item(
        self,
        raw: dict[str, Any],
        actor_run_id: str,
        ingestion_run_id: str,
    ) -> SocialPost:
        # Not called in the X API path — pipeline branches to fetch_posts_via_api.
        raise NotImplementedError(
            "XAdapter.parse_item is a stub — use fetch_posts_via_api"
        )

    def fetch_posts_via_api(
        self,
        targets: list[Target],
        ingestion_run_id: str,
        actor_run_id: str,
        max_posts: int = _DEFAULT_MAX_POSTS,
        start_time: datetime | None = None,
    ) -> tuple[list[SocialPost], list[dict], dict[str, int]]:
        """Fetch recent tweets for all targets via X API v2.

        Returns ``(posts, dlq_entries, stats)``. API errors for individual
        handles are caught and DLQ'd — a single suspended account does not
        abort the run for the others. ``stats`` lets the pipeline distinguish
        "API ok, zero new posts" (legitimate empty → watermark may advance)
        from "API broken" (auth/rate/5xx → watermark must NOT advance).

        ``stats`` keys:
            handles_attempted: total handles iterated.
            auth_failures: handles that errored with HTTP 401/403.
            api_errors: handles that errored with other HTTP non-2xx.
            users_not_found: handles where ``/users/by/username`` returned 404.
            successful_calls: handles whose user+tweets endpoints both
                returned HTTP 2xx (regardless of how many tweets came back).
                A run with ``successful_calls == handles_attempted`` and
                zero posts is a *legitimate empty* — safe to advance.
        """
        posts: list[SocialPost] = []
        dlq: list[dict] = []
        stats = {
            "handles_attempted": 0,
            "auth_failures": 0,
            "api_errors": 0,
            "users_not_found": 0,
            "successful_calls": 0,
        }

        with httpx.Client(
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=30.0,
        ) as client:
            for target in targets:
                stats["handles_attempted"] += 1
                handle = target.x_handle.strip().lstrip("@")
                try:
                    user_info = self._get_user(client, handle)
                    if user_info is None:
                        stats["users_not_found"] += 1
                        logger.warning(
                            "X API: user not found for @{handle} — skipping",
                            handle=handle,
                        )
                        continue
                    tweets = self._get_tweets(
                        client, user_info["id"], max_posts, start_time=start_time
                    )
                    stats["successful_calls"] += 1
                    for tweet in tweets:
                        try:
                            posts.append(
                                self._parse_tweet(
                                    tweet,
                                    user_info=user_info,
                                    actor_run_id=actor_run_id,
                                    ingestion_run_id=ingestion_run_id,
                                )
                            )
                        except SchemaDriftError as exc:
                            dlq.append(
                                self._dlq_entry(
                                    tweet,
                                    actor_run_id,
                                    ingestion_run_id,
                                    reason="schema_drift",
                                    error=str(exc),
                                )
                            )
                except _XAuthError as exc:
                    stats["auth_failures"] += 1
                    err = str(exc)
                    logger.error(
                        "X API auth failure for @{handle} (HTTP {code}): {err}",
                        handle=handle,
                        code=exc.status_code,
                        err=err,
                    )
                    dlq.append(
                        self._dlq_entry(
                            {"handle": handle},
                            actor_run_id,
                            ingestion_run_id,
                            reason="auth_error",
                            error=err,
                        )
                    )
                except _XApiError as exc:
                    stats["api_errors"] += 1
                    err = str(exc)
                    logger.error(
                        "X API error for @{handle}: {err}",
                        handle=handle,
                        err=err,
                    )
                    dlq.append(
                        self._dlq_entry(
                            {"handle": handle},
                            actor_run_id,
                            ingestion_run_id,
                            reason="api_error",
                            error=err,
                        )
                    )

        return posts, dlq, stats

    # --- Private helpers ---

    def _get_user(self, client: httpx.Client, username: str) -> dict[str, Any] | None:
        resp = client.get(
            f"{_API_BASE}/users/by/username/{username}",
            params={"user.fields": "name,username,verified,public_metrics"},
        )
        if resp.status_code == 404:
            return None
        _raise_for_status(resp, f"/users/by/username/{username}")
        body = resp.json()
        return body.get("data")  # None when API returns errors-only body

    def _get_tweets(
        self,
        client: httpx.Client,
        user_id: str,
        max_results: int,
        start_time: datetime | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "max_results": min(max_results, 100),
            "tweet.fields": "created_at,text,public_metrics,referenced_tweets,lang",
        }
        if start_time is not None:
            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=UTC)
            params["start_time"] = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        resp = client.get(f"{_API_BASE}/users/{user_id}/tweets", params=params)
        if resp.status_code == 429:
            reset = int(resp.headers.get("x-rate-limit-reset", time.time() + 60))
            wait = max(reset - time.time(), 1.0)
            logger.warning(
                "X API rate limited on /users/{id}/tweets — waiting {wait:.0f}s",
                id=user_id,
                wait=wait,
            )
            time.sleep(wait)
            resp = client.get(f"{_API_BASE}/users/{user_id}/tweets", params=params)
        _raise_for_status(resp, f"/users/{user_id}/tweets")
        return resp.json().get("data") or []

    def _parse_tweet(
        self,
        raw: dict[str, Any],
        user_info: dict[str, Any],
        actor_run_id: str,
        ingestion_run_id: str,
    ) -> SocialPost:
        missing = [k for k in ("id", "text", "created_at") if k not in raw]
        if missing:
            raise SchemaDriftError(
                f"schema drift: X API tweet missing keys {missing!r} — "
                f"item has keys: {sorted(raw.keys())!r}"
            )

        post_id_raw = str(raw["id"])
        text = (raw.get("text") or "").strip()
        metrics = raw.get("public_metrics") or {}
        refs: list[dict[str, Any]] = raw.get("referenced_tweets") or []

        handle = str(user_info.get("username") or "")
        display = str(user_info.get("name") or handle)
        verified = bool(user_info.get("verified") or False)

        is_repost = any(r.get("type") == "retweeted" for r in refs)
        is_reply = any(r.get("type") == "replied_to" for r in refs)
        parent_ref = next(
            (r for r in refs if r.get("type") in ("retweeted", "replied_to")), None
        )
        parent_raw_id: str | None = parent_ref["id"] if parent_ref else None

        return SocialPost(
            post_id=self.prefix_post_id(self.platform, post_id_raw),
            platform=self.platform,
            url=f"https://x.com/{handle}/status/{post_id_raw}",
            account=SocialAccount(
                platform=self.platform,
                handle=handle,
                display_name=display,
                verified=verified,
            ),
            author_display_name=display,
            text=text,
            language=raw.get("lang"),
            published_at=datetime.fromisoformat(
                raw["created_at"].replace("Z", "+00:00")
            ),
            engagement=Engagement(
                likes=_safe_int(metrics.get("like_count")),
                comments=_safe_int(metrics.get("reply_count")),
                shares=_safe_int(metrics.get("retweet_count")),
                views=_safe_int(metrics.get("impression_count")),
            ),
            is_repost=is_repost,
            is_reply=is_reply,
            parent_post_id=(
                self.prefix_post_id(self.platform, parent_raw_id)
                if parent_raw_id
                else None
            ),
            content_hash=self.compute_content_hash(self.platform, post_id_raw, text),
            actor_run_id=actor_run_id,
            ingestion_run_id=ingestion_run_id,
            schema_version=_SCHEMA_VERSION,
        )

    @staticmethod
    def _dlq_entry(
        raw: dict[str, Any],
        actor_run_id: str,
        ingestion_run_id: str,
        reason: str,
        error: str,
    ) -> dict[str, Any]:
        return {
            "platform": "x",
            "actor_run_id": actor_run_id,
            "ingestion_run_id": ingestion_run_id,
            "reason": reason,
            "error": error,
            "raw_keys": sorted(raw.keys()),
            "raw": raw,
            "captured_at": datetime.now(UTC).isoformat(),
        }


def _safe_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _raise_for_status(resp: httpx.Response, endpoint: str) -> None:
    if not resp.is_error:
        return
    msg = f"X API {endpoint} returned HTTP {resp.status_code}: {resp.text[:200]}"
    if resp.status_code in (401, 403):
        raise _XAuthError(msg, status_code=resp.status_code)
    raise _XApiError(msg, status_code=resp.status_code)
