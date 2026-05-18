"""Adapter parse_item tests — one fixture per platform, golden-path + edge cases."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mapear_domain.entity_resolution import Target
from mapear_social.adapters import (
    FacebookAdapter,
    InstagramAdapter,
    SchemaDriftError,
    TikTokAdapter,
    XAdapter,
    get_adapter,
)

_TARGETS = [
    Target(
        person_id="mayor_paulinho_freire",
        name="Paulinho Freire",
        role="mayor",
        party="União Brasil",
        city="Natal",
        facebook_page="paulinho.freire",
        instagram_username="paulinho.freire",
        x_handle="paulinhofreire",
        tiktok_handle="paulinhofreire",
    ),
    Target(
        person_id="mayor_allyson_silva",
        name="Allyson Silva",
        role="mayor",
        party="União Brasil",
        city="Mossoró",
        instagram_username="allysonsilva",
        tiktok_handle="allysonsilva",
    ),
    Target(
        person_id="governor_fatima_bezerra",
        name="Fátima Bezerra",
        role="governor",
        party="PT",
        city="",
        x_handle="fatimabezerra",
        tiktok_handle="fatimabezerra",
        is_incumbent=True,
    ),
    Target(  # no social handles — must be filtered out
        person_id="mayor_areia_branca_pendente",
        name="Prefeitura Areia Branca",
        role="mayor",
        party="indefinido",
        city="Areia Branca",
    ),
]


# --- Facebook ----------------------------------------------------------------


def test_facebook_build_input_filters_out_missing_handles():
    adapter = FacebookAdapter()
    payload = adapter.build_input(_TARGETS)
    urls = [s["url"] for s in payload["startUrls"]]
    assert urls == ["https://facebook.com/paulinho.freire"]
    assert payload["resultsLimit"] == 10
    assert "commentsMode" not in payload
    # Without ``since``, no temporal filter is pushed — avoids accidentally
    # excluding posts on a first run where the watermark is unset.
    assert "onlyPostsNewerThan" not in payload


def test_facebook_build_input_pushes_only_posts_newer_than_when_since_given():
    """Apify bills per scraped item — sending ``onlyPostsNewerThan`` is
    the only way to actually reduce cost. Without this field the actor
    returns the full ``resultsLimit`` per profile every run."""
    payload = FacebookAdapter().build_input(
        _TARGETS,
        since=datetime(2026, 4, 23, 12, 0, tzinfo=UTC),
    )
    assert payload["onlyPostsNewerThan"] == "2026-04-23T12:00:00Z"


def test_facebook_build_input_only_posts_newer_than_assumes_utc_if_naive():
    payload = FacebookAdapter().build_input(
        _TARGETS,
        since=datetime(2026, 4, 23, 12, 0),  # naive → treated as UTC
    )
    assert payload["onlyPostsNewerThan"] == "2026-04-23T12:00:00Z"


def test_facebook_build_input_has_proxy_configuration():
    """Actor KoJrdxJCTtpon81KY requires RESIDENTIAL proxy to scrape public pages."""
    payload = FacebookAdapter().build_input(_TARGETS)
    proxy = payload.get("proxyConfiguration", {})
    assert proxy.get("useApifyProxy") is True
    assert "RESIDENTIAL" in proxy.get("apifyProxyGroups", [])


def test_facebook_build_input_excludes_legacy_fields():
    """Old actor fields (query, search_type, max_posts) must not appear."""
    payload = FacebookAdapter().build_input(_TARGETS)
    assert "query" not in payload
    assert "search_type" not in payload
    assert "max_posts" not in payload


def test_facebook_build_input_no_handles_returns_empty_start_urls():
    """When no targets have facebook_page, startUrls is empty —
    pipeline guard catches this."""
    no_fb = [t for t in _TARGETS if not t.facebook_page]
    payload = FacebookAdapter().build_input(no_fb)
    assert payload["startUrls"] == []


def test_facebook_parse_item_iso_timestamp(load_fixture):
    items = load_fixture("facebook_apify_response")
    post = FacebookAdapter().parse_item(
        items[0], actor_run_id="run-fb-1", ingestion_run_id="ingest-1"
    )
    assert post.post_id == "fb:1234567890123456"
    assert post.platform == "facebook"
    assert post.published_at == datetime(2026, 4, 19, 14, 32, 11, tzinfo=UTC)
    assert post.engagement.likes == 1523
    assert post.engagement.comments == 218
    assert post.engagement.shares == 94
    # handle extracted from post URL (facebook.com/<handle>/posts/<id>)
    assert post.account.handle == "paulinho.freire"
    # verified comes from user.verified in new actor format
    assert post.account.verified is True
    assert post.is_repost is False
    assert post.content_hash  # sha256 hex — populated


def test_facebook_parse_item_epoch_millis_and_shared(load_fixture):
    items = load_fixture("facebook_apify_response")
    post = FacebookAdapter().parse_item(
        items[1], actor_run_id="run-fb-1", ingestion_run_id="ingest-1"
    )
    assert post.is_repost is True
    assert post.parent_post_id == "fb:9999999999999"
    # epoch millis 1745067131000 → 2025-04-19T14:12:11Z (sanity: > 2024)
    assert post.published_at.year >= 2024
    assert post.engagement.likes == 412


def test_facebook_schema_drift_missing_post_id():
    adapter = FacebookAdapter()
    bad = {"url": "https://facebook.com/x/posts/1", "time": "2026-04-20T00:00:00Z"}
    with pytest.raises(SchemaDriftError):
        adapter.parse_item(bad, actor_run_id="run-x", ingestion_run_id="ing-x")


def test_facebook_not_available_routed_to_page_unavailable():
    """Deterministic `not_available` errors get their own DLQ reason so
    repeat failures don't alert as schema drift and can be reported back
    to the seed owner.
    """
    adapter = FacebookAdapter()
    sentinel = {
        "url": "https://facebook.com/DepEzequielRN",
        "error": "not_available",
        "errorDescription": "This content isn't available…",
    }
    with pytest.raises(SchemaDriftError) as exc:
        adapter.parse_item(sentinel, actor_run_id="run-fb", ingestion_run_id="ing-fb")
    assert str(exc.value).startswith("page_unavailable:")
    assert "DepEzequielRN" in str(exc.value)


# --- Instagram ---------------------------------------------------------------


def test_instagram_build_input_uses_usernames_list():
    payload = InstagramAdapter().build_input(_TARGETS)
    assert set(payload["usernames"]) == {"paulinho.freire", "allysonsilva"}
    assert payload["maxPosts"] == 10
    assert "resultsType" not in payload
    assert "resultsLimit" not in payload
    assert "onlyPostsNewerThan" not in payload


def test_instagram_build_input_pushes_only_posts_newer_than_when_since_given():
    payload = InstagramAdapter().build_input(
        _TARGETS,
        since=datetime(2026, 4, 23, 12, 0, tzinfo=UTC),
    )
    assert payload["onlyPostsNewerThan"] == "2026-04-23T12:00:00Z"


def test_instagram_parse_item_image_post(load_fixture):
    items = load_fixture("instagram_apify_response")
    post = InstagramAdapter().parse_item(
        items[0], actor_run_id="run-ig-1", ingestion_run_id="ingest-1"
    )
    assert post.post_id == "ig:CxY1a2b3c4d"
    assert post.platform == "instagram"
    assert post.account.handle == "allysonsilva"
    assert post.account.verified is True
    assert post.engagement.likes == 2741
    assert post.engagement.shares is None  # IG does not expose shares


def test_instagram_parse_item_reel_with_views(load_fixture):
    items = load_fixture("instagram_apify_response")
    post = InstagramAdapter().parse_item(
        items[1], actor_run_id="run-ig-1", ingestion_run_id="ingest-1"
    )
    assert post.engagement.views == 15421
    assert post.account.verified is False


def test_instagram_story_is_rejected_as_drift(load_fixture):
    items = load_fixture("instagram_apify_response")
    with pytest.raises(SchemaDriftError):
        InstagramAdapter().parse_item(
            items[2], actor_run_id="run-ig-1", ingestion_run_id="ingest-1"
        )


def test_instagram_parse_item_new_schema_mediatype_and_shortcode():
    """shu8hvrXbJbY3Eb9W emits mediaType/shortCode/takenAt in newer actor versions."""
    item = {
        "shortCode": "DaB1c2d3e4f",
        "mediaType": "IMAGE",
        "postUrl": "https://instagram.com/p/DaB1c2d3e4f/",
        "caption": "Texto do post.",
        "takenAt": "2026-04-20T10:00:00.000Z",
        "ownerUsername": "paulinho.freire",
        "ownerFullName": "Paulinho Freire",
        "ownerVerified": True,
        "likesCount": 500,
        "commentsCount": 30,
    }
    post = InstagramAdapter().parse_item(
        item, actor_run_id="run-ig-2", ingestion_run_id="ingest-2"
    )
    assert post.post_id == "ig:DaB1c2d3e4f"
    assert str(post.url) == "https://instagram.com/p/DaB1c2d3e4f/"
    assert post.published_at.year == 2026
    assert post.engagement.likes == 500
    assert post.engagement.comments == 30
    assert post.account.handle == "paulinho.freire"


def test_instagram_parse_item_nested_owner_and_url_reconstruction():
    """ownerProfile nesting + URL reconstructed from shortCode when url is absent."""
    item = {
        "shortCode": "DaC9x8y7z6",
        "mediaType": "VIDEO",
        "takenAt": 1745096743,
        "ownerProfile": {
            "username": "raimundanilda",
            "full_name": "Raimunda Nilda",
            "is_verified": False,
        },
        "likesCount": 100,
    }
    post = InstagramAdapter().parse_item(
        item, actor_run_id="run-ig-3", ingestion_run_id="ingest-3"
    )
    assert post.account.handle == "raimundanilda"
    assert post.account.display_name == "Raimunda Nilda"
    assert post.account.verified is False
    assert str(post.url) == "https://www.instagram.com/p/DaC9x8y7z6/"


def test_instagram_parse_item_old_schema_aliases():
    """Older shu8hvrXbJbY3Eb9W versions emit postId/permalink/createdAt/username."""
    item = {
        "postId": "CxOLD0001",
        "permalink": "https://instagram.com/p/CxOLD0001/",
        "createdAt": "2026-04-10T08:30:00.000Z",
        "username": "joao.camara",
        "caption": "Post com campos no formato antigo.",
        "type": "Image",
        "likeCount": 350,
        "commentCount": 22,
    }
    post = InstagramAdapter().parse_item(
        item, actor_run_id="run-ig-alias", ingestion_run_id="ingest-alias"
    )
    assert post.post_id == "ig:CxOLD0001"
    assert str(post.url) == "https://instagram.com/p/CxOLD0001/"
    assert post.published_at == datetime(2026, 4, 10, 8, 30, 0, tzinfo=UTC)
    assert post.account.handle == "joao.camara"
    assert post.engagement.likes == 350
    assert post.engagement.comments == 22


def test_instagram_parse_item_pk_alias_and_url_reconstruction():
    """pk is the Instagram primary key used in newer scraper versions;
    url reconstructed."""
    item = {
        "pk": "CxPK00042",
        "takenAt": 1745096743,
        "username": "alcides.belo",
        "mediaType": "VIDEO",
        "likeCount": 88,
    }
    post = InstagramAdapter().parse_item(
        item, actor_run_id="run-ig-pk", ingestion_run_id="ingest-pk"
    )
    assert post.post_id == "ig:CxPK00042"
    assert str(post.url) == "https://www.instagram.com/p/CxPK00042/"
    assert post.account.handle == "alcides.belo"


def test_instagram_parse_item_igid_and_isodate_aliases():
    """igId + isoDate are newer shu8hvrXbJbY3Eb9W field names
    not covered by old aliases."""
    item = {
        "igId": "DxNEW0099",
        "isoDate": "2026-04-21T14:30:00.000Z",
        "url": "https://www.instagram.com/p/DxNEW0099/",
        "ownerUsername": "celso.araujo",
        "mediaType": "IMAGE",
        "likes": 120,
    }
    post = InstagramAdapter().parse_item(
        item, actor_run_id="run-ig-igid", ingestion_run_id="ingest-igid"
    )
    assert post.post_id == "ig:DxNEW0099"
    assert post.published_at.year == 2026
    assert post.account.handle == "celso.araujo"
    assert post.engagement.likes == 120


def test_instagram_parse_item_username_from_url():
    """ownerUsername extracted from post URL when no flat or nested field is present."""
    item = {
        "shortCode": "DxURL0001",
        "url": "https://www.instagram.com/joao.silva/p/DxURL0001/",
        "isoDate": "2026-04-20T09:00:00Z",
        "mediaType": "IMAGE",
    }
    post = InstagramAdapter().parse_item(
        item, actor_run_id="run-ig-url", ingestion_run_id="ingest-url"
    )
    assert post.account.handle == "joao.silva"


def test_instagram_parse_item_queryusername_alias():
    """queryUsername is emitted by some actor versions as the input handle."""
    item = {
        "shortcode": "DxQU0002",
        "postedAt": "2026-04-19T18:00:00Z",
        "url": "https://www.instagram.com/p/DxQU0002/",
        "queryUsername": "luiz.governor",
        "mediaType": "REEL",
        "likeCount": 800,
    }
    post = InstagramAdapter().parse_item(
        item, actor_run_id="run-ig-quser", ingestion_run_id="ingest-quser"
    )
    assert post.post_id == "ig:DxQU0002"
    assert post.account.handle == "luiz.governor"
    assert post.engagement.likes == 800


def test_instagram_parse_item_user_nested_parent():
    """Username nested under 'user' key (another actor variant)."""
    item = {
        "mediaId": "DxUSER0003",
        "timestamp": "2026-04-18T12:00:00Z",
        "url": "https://www.instagram.com/p/DxUSER0003/",
        "user": {"username": "maria.prefeita", "name": "Maria Prefeita"},
        "mediaType": "IMAGE",
    }
    post = InstagramAdapter().parse_item(
        item, actor_run_id="run-ig-user", ingestion_run_id="ingest-user"
    )
    assert post.account.handle == "maria.prefeita"
    assert post.account.display_name == "Maria Prefeita"


_PROFILE_SCRAPER_RESPONSE = [
    {
        "username": "fatimabezerra13",
        "isVerified": True,
        "followersCount": 363849,
        "latestPosts": [
            {
                "id": "3730925229268083880",
                "shortCode": "DPG6LqOESSo",
                "caption": "Reunião com lideranças em Natal.",
                "likesCount": 3720,
                "commentsCount": 1836,
                "timestamp": "2025-09-27T13:49:27.000Z",
                "displayUrl": "https://cdn.instagram.com/img.jpg",
                "type": "Sidecar",
            },
            {
                "id": "3710000000000000001",
                "shortCode": "DPA1b2c3d4",
                "caption": "Evento de saúde.",
                "likesCount": 1200,
                "commentsCount": 88,
                "timestamp": "2025-09-20T10:00:00.000Z",
                "type": "Image",
            },
        ],
    },
    {
        "username": "allysonsilva",
        "isVerified": False,
        "followersCount": 45000,
        "latestPosts": [],
    },
]


def test_instagram_expand_items_flattens_profile_posts():
    adapter = InstagramAdapter()
    expanded = adapter.expand_items(_PROFILE_SCRAPER_RESPONSE)
    assert len(expanded) == 2  # 2 posts from fatimabezerra13, 0 from allysonsilva
    assert expanded[0]["id"] == "3730925229268083880"
    assert expanded[0]["username"] == "fatimabezerra13"
    assert expanded[0]["ownerVerified"] is True
    assert expanded[1]["id"] == "3710000000000000001"
    assert expanded[1]["username"] == "fatimabezerra13"


def test_instagram_expand_items_passes_through_old_format(load_fixture):
    items = load_fixture("instagram_apify_response")
    adapter = InstagramAdapter()
    expanded = adapter.expand_items(items)
    assert expanded == items  # old-format post items unchanged


def test_instagram_expand_items_profile_with_no_posts_yields_nothing():
    profiles = [{"username": "empty_account", "isVerified": False, "latestPosts": []}]
    expanded = InstagramAdapter().expand_items(profiles)
    assert expanded == []


def test_instagram_parse_item_new_actor_post_format():
    """Post item from instagram-profile-scraper after expand_items injection."""
    post_item = {
        "id": "3730925229268083880",
        "shortCode": "DPG6LqOESSo",
        "caption": "Reunião com lideranças em Natal.",
        "likesCount": 3720,
        "commentsCount": 1836,
        "timestamp": "2025-09-27T13:49:27.000Z",
        "displayUrl": "https://cdn.instagram.com/img.jpg",
        "type": "Sidecar",
        # injected by expand_items
        "username": "fatimabezerra13",
        "ownerVerified": True,
    }
    post = InstagramAdapter().parse_item(
        post_item, actor_run_id="run-ig-new", ingestion_run_id="ingest-new"
    )
    assert post.post_id == "ig:3730925229268083880"
    assert str(post.url) == "https://www.instagram.com/p/DPG6LqOESSo/"
    assert post.account.handle == "fatimabezerra13"
    assert post.account.verified is True
    assert post.engagement.likes == 3720
    assert post.engagement.comments == 1836
    assert post.engagement.shares is None
    assert post.published_at.year == 2025


def test_require_keys_error_includes_available_keys():
    """SchemaDriftError message must list the available keys for diagnostics."""
    item = {
        "shortCode": "ABC123",
        "isoDate": "2026-04-22T10:00:00Z",
        "url": "https://www.instagram.com/p/ABC123/",
        # ownerUsername deliberately absent — not covered by any alias
        "unknownField": "value",
    }
    with pytest.raises(SchemaDriftError, match="item has") as exc_info:
        InstagramAdapter().parse_item(
            item, actor_run_id="run-drift", ingestion_run_id="ingest-drift"
        )
    assert "ownerUsername" in str(exc_info.value)
    assert "unknownField" in str(exc_info.value)


def test_instagram_parse_item_no_id_classified_as_non_post_item():
    """Items where no id alias is present must be non_post_item, not schema_drift.

    Without this classification the pipeline exits(4) when an entire batch
    consists of profile-summary or error items returned by the actor (e.g. when
    a monitored account has no posts yet).
    """
    item = {
        # Has a post-signal key (username) so it passes the first guard,
        # but no id field or any known id alias → normalization cannot derive id.
        "username": "paulinho.freire",
        "profileUrl": "https://www.instagram.com/paulinho.freire/",
        "mediaType": "IMAGE",
        # Timestamp field present but no id at all
        "isoDate": "2026-04-22T10:00:00Z",
    }
    with pytest.raises(SchemaDriftError) as exc_info:
        InstagramAdapter().parse_item(
            item, actor_run_id="run-nopost", ingestion_run_id="ingest-nopost"
        )
    assert str(exc_info.value).startswith("non_post_item:")


# --- X -----------------------------------------------------------------------


def test_x_build_input_strips_leading_at():
    payload = XAdapter().build_input(_TARGETS)
    assert set(payload["twitterHandles"]) == {"paulinhofreire", "fatimabezerra"}


def test_x_parse_tweet_golden_path(load_fixture):
    items = load_fixture("x_api_v2_response")
    adapter = XAdapter()
    post = adapter._parse_tweet(
        items[0]["tweet"],
        user_info=items[0]["user"],
        actor_run_id="run-x-1",
        ingestion_run_id="ingest-1",
    )
    assert post.post_id == "x:1783450000000000001"
    assert post.account.handle == "fatimabezerra"
    assert post.account.verified is True
    assert post.engagement.likes == 3412
    assert post.engagement.shares == 892
    assert post.engagement.comments == 215
    assert post.engagement.views == 124318
    assert post.is_repost is False
    assert post.is_reply is False
    assert str(post.url) == "https://x.com/fatimabezerra/status/1783450000000000001"


def test_x_parse_tweet_retweet_marked_and_parent_set(load_fixture):
    items = load_fixture("x_api_v2_response")
    adapter = XAdapter()
    post = adapter._parse_tweet(
        items[1]["tweet"],
        user_info=items[1]["user"],
        actor_run_id="run-x-1",
        ingestion_run_id="ingest-1",
    )
    assert post.is_repost is True
    assert post.parent_post_id == "x:1783449999000000000"
    assert post.published_at.tzinfo is not None


def test_x_parse_tweet_reply_threads_parent_id(load_fixture):
    items = load_fixture("x_api_v2_response")
    adapter = XAdapter()
    post = adapter._parse_tweet(
        items[2]["tweet"],
        user_info=items[2]["user"],
        actor_run_id="run-x-1",
        ingestion_run_id="ingest-1",
    )
    assert post.is_reply is True
    assert post.is_repost is False
    assert post.parent_post_id == "x:1783450000000000002"


# --- X auth-failure regression (#41 / #42) -----------------------------------


def test_x_raise_for_status_401_raises_auth_error():
    """HTTP 401/403 must raise _XAuthError so the pipeline can distinguish
    auth failure from generic API error and abort instead of advancing the
    watermark."""
    import httpx

    from mapear_social.adapters.x import _raise_for_status, _XApiError, _XAuthError

    resp = httpx.Response(401, text="Unauthorized")
    with pytest.raises(_XAuthError) as exc:
        _raise_for_status(resp, "/users/by/username/foo")
    assert exc.value.status_code == 401
    assert isinstance(exc.value, _XApiError)  # subclass — backward compat


def test_x_raise_for_status_403_raises_auth_error():
    import httpx

    from mapear_social.adapters.x import _raise_for_status, _XAuthError

    resp = httpx.Response(403, text="Forbidden")
    with pytest.raises(_XAuthError) as exc:
        _raise_for_status(resp, "/users/by/username/foo")
    assert exc.value.status_code == 403


def test_x_raise_for_status_500_raises_generic_api_error():
    """Non-auth HTTP errors stay on _XApiError (not _XAuthError) so
    they don't trip the auth-failure threshold."""
    import httpx

    from mapear_social.adapters.x import _raise_for_status, _XApiError, _XAuthError

    resp = httpx.Response(500, text="server boom")
    with pytest.raises(_XApiError) as exc:
        _raise_for_status(resp, "/users/by/username/foo")
    assert exc.value.status_code == 500
    assert not isinstance(exc.value, _XAuthError)


def _make_x_target(handle: str) -> Target:
    return Target(
        person_id=f"mayor_{handle}",
        name=handle.title(),
        role="mayor",
        party="X",
        city="Natal",
        x_handle=handle,
    )


def test_x_fetch_posts_all_401_populates_auth_failures(monkeypatch):
    """100% HTTP 401 → every handle increments auth_failures and lands in
    DLQ as ``auth_error`` (not ``api_error``). No posts are returned and
    no api_error counter is touched."""
    import httpx

    targets = [_make_x_target("fatimabezerra"), _make_x_target("paulinhofreire")]
    adapter = XAdapter()
    monkeypatch.setattr(adapter, "_token", "expired-bearer-token")

    def _401(*_a, **_kw):
        return httpx.Response(401, text='{"title":"Unauthorized"}')

    monkeypatch.setattr(httpx.Client, "get", lambda self, url, params=None: _401())

    posts, dlq, stats = adapter.fetch_posts_via_api(
        targets,
        ingestion_run_id="ing-x-test",
        actor_run_id="run-x-test",
    )

    assert posts == []
    assert stats["handles_attempted"] == 2
    assert stats["auth_failures"] == 2
    assert stats["api_errors"] == 0
    assert stats["successful_calls"] == 0
    assert len(dlq) == 2
    assert all(e["reason"] == "auth_error" for e in dlq)


def test_x_fetch_posts_500_populates_api_errors(monkeypatch):
    """Non-auth 5xx errors keep the existing ``api_error`` reason and do
    not inflate the auth-failure count."""
    import httpx

    targets = [_make_x_target("fatimabezerra")]
    adapter = XAdapter()
    monkeypatch.setattr(adapter, "_token", "valid-bearer-token")
    monkeypatch.setattr(
        httpx.Client,
        "get",
        lambda self, url, params=None: httpx.Response(500, text="boom"),
    )

    posts, dlq, stats = adapter.fetch_posts_via_api(
        targets,
        ingestion_run_id="ing-x-500",
        actor_run_id="run-x-500",
    )

    assert posts == []
    assert stats["auth_failures"] == 0
    assert stats["api_errors"] == 1
    assert dlq[0]["reason"] == "api_error"


def test_x_fetch_posts_legitimate_empty_marks_success(monkeypatch):
    """User found + 0 tweets returned → counts as a successful call, NOT
    as an error. The pipeline relies on this to decide whether the
    watermark may advance on a zero-post run."""
    import httpx

    targets = [_make_x_target("fatimabezerra")]
    adapter = XAdapter()
    monkeypatch.setattr(adapter, "_token", "valid-bearer-token")

    def _ok_get(self, url, params=None):
        if "users/by/username" in url:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": "12345",
                        "username": "fatimabezerra",
                        "name": "Fátima",
                        "verified": True,
                        "public_metrics": {},
                    }
                },
            )
        # /users/<id>/tweets — empty payload (no new tweets in window)
        return httpx.Response(200, json={"meta": {"result_count": 0}})

    monkeypatch.setattr(httpx.Client, "get", _ok_get)

    posts, dlq, stats = adapter.fetch_posts_via_api(
        targets,
        ingestion_run_id="ing-x-empty",
        actor_run_id="run-x-empty",
    )

    assert posts == []
    assert dlq == []
    assert stats["successful_calls"] == 1
    assert stats["auth_failures"] == 0
    assert stats["api_errors"] == 0


# --- TikTok ------------------------------------------------------------------


def test_tiktok_build_input_uses_profiles():
    payload = TikTokAdapter().build_input(_TARGETS)
    profiles = set(payload["profiles"])
    assert "paulinhofreire" in profiles
    assert "allysonsilva" in profiles
    assert "fatimabezerra" in profiles
    assert payload["resultsPerPage"] == 10
    assert payload["shouldDownloadVideos"] is False
    assert "oldestPostDateUnified" not in payload


def test_tiktok_build_input_pushes_oldest_post_date_unified_when_since_given():
    """clockworks/tiktok-scraper uses date-only (YYYY-MM-DD), not ISO 8601."""
    payload = TikTokAdapter().build_input(
        _TARGETS,
        since=datetime(2026, 4, 23, 12, 0, tzinfo=UTC),
    )
    assert payload["oldestPostDateUnified"] == "2026-04-23"


def test_tiktok_build_input_payload_contract():
    """Actor GdWCkxBtKWOsKjdch uses profiles (bare handles) — startUrls causes 400."""
    payload = TikTokAdapter().build_input(_TARGETS)
    assert "profiles" in payload
    assert "startUrls" not in payload
    assert payload["profileScrapeSections"] == ["videos"]
    for handle in payload["profiles"]:
        assert isinstance(handle, str)
        assert not handle.startswith("@")


def test_tiktok_parse_item_iso_timestamp_new_actor(load_fixture):
    """New actor (GdWCkxBtKWOsKjdch) uses createTimeISO and authorMeta.name."""
    items = load_fixture("tiktok_apify_response")
    post = TikTokAdapter().parse_item(
        items[0], actor_run_id="run-tt-1", ingestion_run_id="ingest-1"
    )
    assert post.post_id == "tt:7234567890123456001"
    assert post.platform == "tiktok"
    # handle comes from authorMeta.name in new actor format
    assert post.account.handle == "fatimabezerra.13"
    assert post.account.display_name == "Fátima Bezerra"
    assert post.account.verified is True
    assert post.engagement.views == 45821
    assert post.engagement.likes == 3210
    assert post.engagement.shares == 92
    assert post.published_at.tzinfo is not None


def test_tiktok_parse_item_second_profile(load_fixture):
    items = load_fixture("tiktok_apify_response")
    post = TikTokAdapter().parse_item(
        items[1], actor_run_id="run-tt-1", ingestion_run_id="ingest-1"
    )
    assert post.account.handle == "allysonsilva"
    assert post.account.verified is False
    assert post.published_at.tzinfo is not None


def test_tiktok_sponsored_is_rejected_as_drift(load_fixture):
    """isSponsored=true (new actor) must be routed to DLQ, not persisted."""
    items = load_fixture("tiktok_apify_response")
    with pytest.raises(SchemaDriftError):
        TikTokAdapter().parse_item(
            items[2], actor_run_id="run-tt-1", ingestion_run_id="ingest-1"
        )


def test_tiktok_parse_item_legacy_epoch_seconds():
    """Old actor (createTime epoch + uniqueId) still parses correctly."""
    item = {
        "id": "7000000000000000001",
        "text": "Legado compatível",
        "createTime": 1745323200,
        "webVideoUrl": "https://www.tiktok.com/@legacyuser/video/7000000000000000001",
        "playCount": 100,
        "diggCount": 10,
        "commentCount": 2,
        "shareCount": 1,
        "authorMeta": {
            "uniqueId": "legacyuser",
            "name": "Legacy User",
            "verified": False,
        },
        "isAd": False,
    }
    post = TikTokAdapter().parse_item(
        item, actor_run_id="run-tt-legacy", ingestion_run_id="ingest-legacy"
    )
    assert post.account.handle == "legacyuser"
    assert post.published_at.year >= 2025


def test_tiktok_note_only_item_classified_as_non_post():
    """Items with 'note' but no 'id' are actor housekeeping rows (profile unavailable).
    They must be classified as non_post_item so 100% DLQ batches exit(0) not exit(4).
    """
    item = {
        "authorMeta": {"name": "someuser", "nickName": "Some User"},
        "input": "someuser",
        "note": "Profile is private or does not exist.",
    }
    with pytest.raises(SchemaDriftError) as exc_info:
        TikTokAdapter().parse_item(item, actor_run_id="run-x", ingestion_run_id="ing-x")
    assert str(exc_info.value).startswith("non_post_item:")


def test_tiktok_error_item_classified_as_non_post():
    """Items with 'error' but no 'id' are video-level fetch errors, not schema drift."""
    item = {
        "error": "Video not found",
        "input": "https://www.tiktok.com/@user/video/123",
        "url": "https://www.tiktok.com/@user/video/123",
    }
    with pytest.raises(SchemaDriftError) as exc_info:
        TikTokAdapter().parse_item(item, actor_run_id="run-x", ingestion_run_id="ing-x")
    assert str(exc_info.value).startswith("non_post_item:")


# --- Registry ----------------------------------------------------------------


def test_registry_returns_correct_adapter():
    assert isinstance(get_adapter("facebook"), FacebookAdapter)
    assert isinstance(get_adapter("instagram"), InstagramAdapter)
    assert isinstance(get_adapter("x"), XAdapter)
    assert isinstance(get_adapter("tiktok"), TikTokAdapter)


def test_registry_rejects_unknown_platform():
    with pytest.raises(ValueError, match="Unknown platform"):
        get_adapter("snapchat")


def test_schema_version_is_set_per_adapter():
    assert FacebookAdapter().expected_schema_version() >= 1
    assert InstagramAdapter().expected_schema_version() >= 1
    assert XAdapter().expected_schema_version() >= 1
    assert TikTokAdapter().expected_schema_version() >= 1


def test_actor_ids_match_apify_console():
    """Guard against accidental revert to old/broken actor slugs."""
    assert (
        FacebookAdapter().actor_id == "KoJrdxJCTtpon81KY"
    )  # apify/facebook-posts-scraper
    assert (
        InstagramAdapter().actor_id == "dSCLg0C3YEZ83HzYX"
    )  # apify/instagram-profile-scraper
    assert XAdapter().actor_id == "x-api-v2"  # native API v2, no Apify
    assert TikTokAdapter().actor_id == "GdWCkxBtKWOsKjdch"  # clockworks/tiktok-scraper
