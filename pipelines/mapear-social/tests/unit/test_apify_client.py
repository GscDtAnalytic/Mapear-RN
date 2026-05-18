"""ApifyClient tests — use respx to stub the 3 endpoints we call."""

from __future__ import annotations

import httpx
import pytest
import respx

from mapear_social.apify_client import (
    ActorPayloadError,
    ActorRunFailed,
    ActorRunTimeout,
    ApifyClient,
)


def _client(**overrides) -> ApifyClient:
    defaults = {
        "token": "test-token",
        "poll_timeout_seconds": 5,
        "poll_initial_interval_seconds": 0.01,  # fast polling in tests
        "dataset_page_size": 2,
    }
    defaults.update(overrides)
    return ApifyClient(**defaults)


@pytest.mark.asyncio
@respx.mock
async def test_run_actor_happy_path_paginates_dataset():
    respx.post("https://api.apify.com/v2/acts/apify/facebook-posts-scraper/runs").mock(
        return_value=httpx.Response(
            201,
            json={
                "data": {
                    "id": "run-abc",
                    "status": "RUNNING",
                    "defaultDatasetId": "ds-1",
                    "startedAt": "2026-04-20T00:00:00Z",
                    "finishedAt": None,
                }
            },
        )
    )

    poll_route = respx.get("https://api.apify.com/v2/actor-runs/run-abc").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "data": {
                        "id": "run-abc",
                        "status": "RUNNING",
                        "defaultDatasetId": "ds-1",
                        "startedAt": "2026-04-20T00:00:00Z",
                        "finishedAt": None,
                    }
                },
            ),
            httpx.Response(
                200,
                json={
                    "data": {
                        "id": "run-abc",
                        "status": "SUCCEEDED",
                        "defaultDatasetId": "ds-1",
                        "startedAt": "2026-04-20T00:00:00Z",
                        "finishedAt": "2026-04-20T00:00:05Z",
                    }
                },
            ),
        ]
    )

    respx.get("https://api.apify.com/v2/datasets/ds-1/items").mock(
        side_effect=[
            httpx.Response(200, json=[{"postId": "1"}, {"postId": "2"}]),
            httpx.Response(200, json=[{"postId": "3"}]),
        ]
    )

    async with _client() as client:
        run, items = await client.run_actor(
            "apify/facebook-posts-scraper", {"startUrls": []}
        )

    assert run.status == "SUCCEEDED"
    assert [i["postId"] for i in items] == ["1", "2", "3"]
    assert poll_route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_run_actor_raises_on_terminal_failure():
    respx.post("https://api.apify.com/v2/acts/apify/broken/runs").mock(
        return_value=httpx.Response(
            201,
            json={
                "data": {
                    "id": "run-fail",
                    "status": "RUNNING",
                    "defaultDatasetId": "ds-fail",
                    "startedAt": "2026-04-20T00:00:00Z",
                    "finishedAt": None,
                }
            },
        )
    )
    respx.get("https://api.apify.com/v2/actor-runs/run-fail").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "id": "run-fail",
                    "status": "FAILED",
                    "defaultDatasetId": "ds-fail",
                    "startedAt": "2026-04-20T00:00:00Z",
                    "finishedAt": "2026-04-20T00:00:05Z",
                }
            },
        )
    )

    async with _client() as client:
        with pytest.raises(ActorRunFailed):
            await client.run_actor("apify/broken", {})


@pytest.mark.asyncio
@respx.mock
async def test_run_actor_times_out_when_never_terminal():
    respx.post("https://api.apify.com/v2/acts/apify/slow/runs").mock(
        return_value=httpx.Response(
            201,
            json={
                "data": {
                    "id": "run-slow",
                    "status": "RUNNING",
                    "defaultDatasetId": "ds-slow",
                    "startedAt": "2026-04-20T00:00:00Z",
                    "finishedAt": None,
                }
            },
        )
    )
    respx.get("https://api.apify.com/v2/actor-runs/run-slow").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "id": "run-slow",
                    "status": "RUNNING",
                    "defaultDatasetId": "ds-slow",
                    "startedAt": "2026-04-20T00:00:00Z",
                    "finishedAt": None,
                }
            },
        )
    )

    async with _client(poll_timeout_seconds=0.05) as client:
        with pytest.raises(ActorRunTimeout):
            await client.run_actor("apify/slow", {})


@pytest.mark.asyncio
@respx.mock
async def test_400_on_start_becomes_payload_error():
    respx.post("https://api.apify.com/v2/acts/apify/bad-input/runs").mock(
        return_value=httpx.Response(
            400,
            json={"error": {"message": "startUrls is required"}},
        )
    )
    async with _client() as client:
        with pytest.raises(
            ActorPayloadError, match="startUrls is required"
        ) as exc_info:
            await client.run_actor("apify/bad-input", {})
    # TDT-SOCIAL-PIPELINE-STRUCTURED-LOGGING: status_code é populado
    # para que o caller faça logging estruturado sem parsing de string.
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
@respx.mock
async def test_401_payload_error_carries_status_code():
    """A-06 (alert mapear_social_401_errors) depende de status_code=401
    estar em jsonPayload.record.extra — TDT-SOCIAL-PIPELINE-STRUCTURED-LOGGING."""
    respx.post("https://api.apify.com/v2/acts/apify/auth-fail/runs").mock(
        return_value=httpx.Response(
            401,
            json={"error": {"type": "user-or-token-not-found", "message": "expired"}},
        )
    )
    async with _client() as client:
        with pytest.raises(ActorPayloadError) as exc_info:
            await client.run_actor("apify/auth-fail", {})
    assert exc_info.value.status_code == 401


def test_empty_token_rejected():
    with pytest.raises(ValueError):
        ApifyClient(token="")


@pytest.mark.asyncio
@respx.mock
async def test_run_actor_failure_message_includes_status_message():
    """ActorRunFailed message must include the Apify statusMessage for triage."""
    respx.post("https://api.apify.com/v2/acts/apify/tiktok-scraper/runs").mock(
        return_value=httpx.Response(
            201,
            json={
                "data": {
                    "id": "run-tt-fail",
                    "status": "RUNNING",
                    "defaultDatasetId": "ds-tt",
                    "startedAt": "2026-04-20T00:00:00Z",
                    "finishedAt": None,
                }
            },
        )
    )
    respx.get("https://api.apify.com/v2/actor-runs/run-tt-fail").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "id": "run-tt-fail",
                    "status": "FAILED",
                    "defaultDatasetId": "ds-tt",
                    "startedAt": "2026-04-20T00:00:00Z",
                    "finishedAt": "2026-04-20T00:01:00Z",
                    "statusMessage": "Out of memory: container exceeded 4096 MB",
                }
            },
        )
    )

    async with _client() as client:
        with pytest.raises(
            ActorRunFailed,
            match="Out of memory: container exceeded 4096 MB",
        ):
            await client.run_actor("apify/tiktok-scraper", {})
