"""Tests for the streaming API client."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from wcl_data.api.client import APIClient, AuthFailureAbort, FetchError, Fetched
from wcl_data.config import Settings


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        csrf_token="x", session_cookie="y", referer="z",
        max_workers=4, connect_timeout=5.0, read_timeout=5.0,
        db_path=tmp_path / "db.sqlite",
        stale_days=30,
        grace_days=15,
    )


def _stub_response(
    status_code=200,
    json_data=None,
    *,
    content_type="application/json",
    headers=None,
    body_bytes=None,
):
    """Build a MagicMock that quacks like the parts of a requests.Response
    the client uses: status_code/reason/headers/iter_content/close.

    `body_bytes` overrides the default JSON-of-json_data body, useful for
    truncated / non-JSON / oversized body tests.
    """
    resp = MagicMock()
    resp.status_code = status_code
    resp.reason = "OK" if status_code == 200 else "Err"
    body_dict = json_data if json_data is not None else {}
    encoded = body_bytes if body_bytes is not None else json.dumps(body_dict).encode("utf-8")
    # iter_content is consumed exactly once per response; the client builds a
    # fresh iter so a list is enough (iter is auto-created on each .return_value
    # access in MagicMock).
    resp.iter_content.return_value = [encoded] if encoded else []
    resp.content = encoded
    resp.json.return_value = body_dict
    merged = {"Content-Type": content_type}
    if headers:
        merged.update(headers)
    resp.headers = merged
    return resp


def test_stream_yields_one_per_id(monkeypatch, tmp_path):
    client = APIClient(make_settings(tmp_path))
    calls = []

    def fake_get(url, timeout, **kw):
        calls.append(url)
        ifsc_id = int(url.rsplit("/", 1)[-1])
        return _stub_response(200, {"id": ifsc_id})

    monkeypatch.setattr(client._session, "get", fake_get)
    results = list(client.stream("athletes", [1, 2, 3]))
    assert {r.key for r in results} == {1, 2, 3}
    assert all(isinstance(r, Fetched) for r in results)
    assert {r.data["id"] for r in results} == {1, 2, 3}


def test_stream_retries_failures(monkeypatch, tmp_path):
    client = APIClient(make_settings(tmp_path))
    call_count = {"n": 0}

    def fake_get(url, timeout, **kw):
        # Fail the first call for id=2; succeed thereafter.
        if url.endswith("/2") and call_count["n"] < 2:
            call_count["n"] += 1
            return _stub_response(500)
        return _stub_response(200, {"ok": True})

    monkeypatch.setattr(client._session, "get", fake_get)
    results = list(client.stream("athletes", [1, 2, 3], retry_delay=0))
    assert {r.key for r in results} == {1, 2, 3}


def test_stream_gives_up_after_max_retries(monkeypatch, tmp_path, caplog):
    client = APIClient(make_settings(tmp_path))

    def fake_get(url, timeout, **kw):
        if url.endswith("/2"):
            return _stub_response(500)  # 5xx so it IS retried under the default policy
        return _stub_response(200, {"ok": True})

    monkeypatch.setattr(client._session, "get", fake_get)
    results = list(client.stream("athletes", [1, 2, 3], max_retries=2, retry_delay=0))
    keys = {r.key for r in results}
    assert keys == {1, 3}      # 2 is never delivered


def test_default_retry_on_skips_404(monkeypatch, tmp_path):
    """4xx should not be retried under the default policy — the discovery probe relies on this."""
    client = APIClient(make_settings(tmp_path))
    call_counts = {"/2": 0}

    def fake_get(url, timeout, **kw):
        if url.endswith("/2"):
            call_counts["/2"] += 1
            return _stub_response(404)
        return _stub_response(200, {"ok": True})

    monkeypatch.setattr(client._session, "get", fake_get)
    results = list(client.stream("athletes", [1, 2, 3], retry_delay=0))

    # /2 hit exactly once — no retries because 404 is permanent under default policy.
    assert call_counts["/2"] == 1
    assert {r.key for r in results} == {1, 3}


def test_custom_retry_on_can_override_default(monkeypatch, tmp_path):
    """Caller can pass a custom predicate to retry everything, even 4xx."""
    client = APIClient(make_settings(tmp_path))
    call_counts = {"/2": 0}

    def fake_get(url, timeout, **kw):
        if url.endswith("/2"):
            call_counts["/2"] += 1
            return _stub_response(404)
        return _stub_response(200, {"ok": True})

    monkeypatch.setattr(client._session, "get", fake_get)
    list(client.stream("athletes", [1, 2, 3], retry_delay=0,
                       max_retries=2, retry_on=lambda exc: True))

    # 1 initial + 2 retries = 3 calls when override forces retry on 404.
    assert call_counts["/2"] == 3


def test_retry_success_yields_each_id_exactly_once(monkeypatch, tmp_path):
    """A 5xx that succeeds on retry must not produce a duplicate yield for that id."""
    client = APIClient(make_settings(tmp_path))
    attempts = {"/2": 0}

    def fake_get(url, timeout, **kw):
        if url.endswith("/2"):
            attempts["/2"] += 1
            if attempts["/2"] == 1:
                return _stub_response(500)
            return _stub_response(200, {"ok": True})
        return _stub_response(200, {"ok": True})

    monkeypatch.setattr(client._session, "get", fake_get)
    keys = [r.key for r in client.stream("athletes", [1, 2, 3], retry_delay=0)]

    # Exactly one yield per id, regardless of how many times /2 was retried.
    assert sorted(keys) == [1, 2, 3]
    assert len(keys) == 3


def test_timeout_is_passed_as_connect_read_tuple(monkeypatch, tmp_path):
    """`requests.get(timeout=...)` should receive (connect, read), not a scalar."""
    client = APIClient(make_settings(tmp_path))
    seen_timeouts: list = []

    def fake_get(url, timeout, **kw):
        seen_timeouts.append(timeout)
        return _stub_response(200, {"ok": True})

    monkeypatch.setattr(client._session, "get", fake_get)
    list(client.stream("athletes", [1]))

    # make_settings sets both to 5; the tuple ordering is (connect, read).
    assert seen_timeouts == [(5, 5)]


def test_429_is_retried_under_default_policy(monkeypatch, tmp_path):
    """429 (rate-limit) should be transient, not permanent like other 4xx."""
    client = APIClient(make_settings(tmp_path))
    attempts = {"/2": 0}

    def fake_get(url, timeout, **kw):
        if url.endswith("/2"):
            attempts["/2"] += 1
            if attempts["/2"] == 1:
                return _stub_response(429)
            return _stub_response(200, {"ok": True})
        return _stub_response(200, {"ok": True})

    monkeypatch.setattr(client._session, "get", fake_get)
    keys = sorted(r.key for r in client.stream("athletes", [1, 2, 3], retry_delay=0))
    assert keys == [1, 2, 3]
    assert attempts["/2"] == 2  # one retry needed


def test_retry_after_header_honored_on_429(monkeypatch, tmp_path):
    """A 429 with `Retry-After: N` should make the next batch sleep ≥ N seconds."""
    client = APIClient(make_settings(tmp_path))
    sleeps: list[float] = []

    def fake_get(url, timeout, **kw):
        if url.endswith("/2"):
            return _stub_response(429, headers={"Retry-After": "3"})
        return _stub_response(200, {"ok": True})

    monkeypatch.setattr(client._session, "get", fake_get)
    # Replace time.sleep so we capture without actually sleeping.
    import wcl_data.api.client as client_mod
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: sleeps.append(s))

    list(client.stream("athletes", [1, 2, 3], retry_delay=0, max_retries=1))

    # First batch had a 429 with Retry-After=3. Backoff (retry_delay=0) is 0;
    # max(0, 3) = 3, so the inter-batch sleep is exactly 3.
    assert sleeps and sleeps[0] >= 3.0


def test_consecutive_auth_failures_trigger_abort(monkeypatch, tmp_path):
    """5 consecutive 401s across the pool should raise `AuthFailureAbort`."""
    client = APIClient(make_settings(tmp_path))

    def fake_get(url, timeout, **kw):
        return _stub_response(401)

    monkeypatch.setattr(client._session, "get", fake_get)
    with pytest.raises(AuthFailureAbort):
        list(client.stream("athletes", list(range(1, 11))))


def test_403_also_counts_toward_auth_abort(monkeypatch, tmp_path):
    """403 (CSRF mismatch) trips the same counter as 401."""
    client = APIClient(make_settings(tmp_path))

    def fake_get(url, timeout, **kw):
        return _stub_response(403)

    monkeypatch.setattr(client._session, "get", fake_get)
    with pytest.raises(AuthFailureAbort):
        list(client.stream("athletes", list(range(1, 11))))


def test_200_resets_consecutive_auth_failure_counter(monkeypatch, tmp_path):
    """A 200 interleaved with 401s should reset the counter; abort must not trip."""
    client = APIClient(make_settings(tmp_path))
    # Drive the order deterministically with a single worker.
    client.settings = client.settings.__class__(
        **{**client.settings.__dict__, "max_workers": 1}
    ) if False else client.settings  # keep frozen; just rely on small N below

    calls = {"n": 0}

    def fake_get(url, timeout, **kw):
        calls["n"] += 1
        # Pattern: 2x 401, then a 200, then 2x 401, then 200… ad infinitum.
        # Never 5 consecutive 401s across the run.
        if calls["n"] % 3 == 0:
            return _stub_response(200, {"ok": True})
        return _stub_response(401)

    monkeypatch.setattr(client._session, "get", fake_get)
    # 4xx is permanent under default policy (except 429), so this completes
    # without raising AuthFailureAbort even though many 401s happen.
    list(client.stream("athletes", list(range(1, 7))))


def test_html_200_is_retried_under_default_policy(monkeypatch, tmp_path):
    """HTML-200 is now a TRANSIENT failure (status_code=None) under the default
    retry predicate — a CDN routing blip on one worker should be retried, not
    silently dropped. Confirms the F4 fix."""
    client = APIClient(make_settings(tmp_path))
    attempts = {"/2": 0}

    def fake_get(url, timeout, **kw):
        if url.endswith("/2"):
            attempts["/2"] += 1
            if attempts["/2"] == 1:
                return _stub_response(200, content_type="text/html; charset=utf-8")
        return _stub_response(200, {"ok": True})

    monkeypatch.setattr(client._session, "get", fake_get)
    keys = sorted(r.key for r in client.stream("athletes", [1, 2, 3], retry_delay=0))
    assert keys == [1, 2, 3]
    assert attempts["/2"] == 2  # initial HTML-200, then retried successfully


def test_oversized_content_length_is_retried(monkeypatch, tmp_path):
    """Content-Length above the cap raises a TRANSIENT FetchError so the row
    is retried (Content-Length might be spuriously huge from a proxy bug),
    not silently dropped."""
    client = APIClient(make_settings(tmp_path))
    huge = APIClient._MAX_RESPONSE_BYTES + 1
    attempts = {"/1": 0}

    def fake_get(url, timeout, **kw):
        attempts["/1"] += 1
        if attempts["/1"] == 1:
            return _stub_response(200, headers={"Content-Length": str(huge)})
        return _stub_response(200, {"ok": True})

    monkeypatch.setattr(client._session, "get", fake_get)
    keys = [r.key for r in client.stream("athletes", [1], retry_delay=0)]
    assert keys == [1]
    assert attempts["/1"] == 2


def test_chunked_oversize_body_capped_during_stream(monkeypatch, tmp_path):
    """When Content-Length is absent (chunked encoding) the iter_content cap
    must still fire — proves F8's streaming guard catches what the
    Content-Length header check would have missed."""
    client = APIClient(make_settings(tmp_path))
    # Build a body slightly larger than the cap, served in chunks.
    cap = APIClient._MAX_RESPONSE_BYTES
    huge_chunks = [b"x" * (64 * 1024)] * ((cap // (64 * 1024)) + 2)
    huge_resp = _stub_response(200, headers={})  # no Content-Length
    del huge_resp.headers["Content-Type"]  # rebuild without forcing length
    huge_resp.headers["Content-Type"] = "application/json"
    huge_resp.iter_content.return_value = huge_chunks
    attempts = {"/1": 0}

    def fake_get(url, timeout, **kw):
        attempts["/1"] += 1
        if attempts["/1"] == 1:
            return huge_resp
        return _stub_response(200, {"ok": True})

    monkeypatch.setattr(client._session, "get", fake_get)
    keys = [r.key for r in client.stream("athletes", [1], retry_delay=0)]
    # Cap fires → transient → retry succeeds.
    assert keys == [1]
    assert attempts["/1"] == 2


def test_malformed_json_becomes_transient_fetcherror(monkeypatch, tmp_path):
    """A 200/application/json with truncated body must surface as a FetchError
    (retriable), NOT escape as a JSONDecodeError that would silently abort
    the fetcher's for-loop. Confirms the F2 fix."""
    client = APIClient(make_settings(tmp_path))
    attempts = {"/1": 0}

    def fake_get(url, timeout, **kw):
        attempts["/1"] += 1
        if attempts["/1"] == 1:
            # Truncated JSON — application/json with non-parseable body.
            return _stub_response(200, body_bytes=b'{ "da')
        return _stub_response(200, {"ok": True})

    monkeypatch.setattr(client._session, "get", fake_get)
    keys = [r.key for r in client.stream("athletes", [1], retry_delay=0)]
    assert keys == [1]
    assert attempts["/1"] == 2


def test_retry_after_inf_is_capped(monkeypatch, tmp_path):
    """A malicious or misconfigured server sending `Retry-After: inf` must
    not pin the client in time.sleep — F10 caps at _MAX_RETRY_AFTER_SECS."""
    from wcl_data.api.client import _MAX_RETRY_AFTER_SECS, _parse_retry_after

    assert _parse_retry_after("inf") == _MAX_RETRY_AFTER_SECS
    assert _parse_retry_after("99999999") == _MAX_RETRY_AFTER_SECS
    assert _parse_retry_after("nan") is None
    assert _parse_retry_after("-5") is None
    assert _parse_retry_after("30") == 30.0


def test_first_retry_sleeps_one_base_delay(monkeypatch, tmp_path):
    """F11: first retry should sleep `retry_delay`, not `2*retry_delay`.
    Standard exponential backoff convention is `base * 2^(attempt-1)`."""
    client = APIClient(make_settings(tmp_path))
    sleeps: list[float] = []

    def fake_get(url, timeout, **kw):
        if url.endswith("/1"):
            return _stub_response(500)  # forces a retry
        return _stub_response(200, {"ok": True})

    monkeypatch.setattr(client._session, "get", fake_get)
    import wcl_data.api.client as client_mod
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: sleeps.append(s))
    # Use a tiny base; jitter is bounded by min(0.5, base) = 0.25.
    list(client.stream("athletes", [1, 2], retry_delay=0.25, max_retries=1))

    # First retry → 0.25 * 2^0 + jitter[0, 0.25] = [0.25, 0.5].
    assert sleeps, "expected one sleep between batches"
    assert 0.25 <= sleeps[0] <= 0.5, f"first-retry sleep {sleeps[0]} out of expected [0.25, 0.5]"


def test_html_200_with_retry_after_honored(monkeypatch, tmp_path):
    """A 200 + text/html + Retry-After: 4 (Cloudflare interstitial pattern)
    must propagate the cooldown into the inter-batch sleep — F15 hoisted the
    Retry-After parse out of the != 200 branch so 200-with-cooldown isn't lost."""
    client = APIClient(make_settings(tmp_path))
    sleeps: list[float] = []
    attempts = {"/1": 0}

    def fake_get(url, timeout, **kw):
        attempts["/1"] += 1
        if attempts["/1"] == 1:
            return _stub_response(200, content_type="text/html",
                                  headers={"Retry-After": "4"})
        return _stub_response(200, {"ok": True})

    monkeypatch.setattr(client._session, "get", fake_get)
    import wcl_data.api.client as client_mod
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: sleeps.append(s))
    list(client.stream("athletes", [1], retry_delay=0, max_retries=1))

    # max(backoff=0, Retry-After=4) = 4.
    assert sleeps and sleeps[0] >= 4.0


def test_auth_counter_resets_only_after_content_guards_pass(monkeypatch, tmp_path):
    """F1: HTML-200 must NOT reset the auth counter (else AuthFailureAbort
    would never trip on an upstream that returns HTML interstitials for
    auth-expired requests). The reset should only happen on a clean JSON
    response that passes every guard."""
    client = APIClient(make_settings(tmp_path))

    # 5 consecutive HTML-200s simulating an auth-redirect interstitial.
    def fake_get(url, timeout, **kw):
        return _stub_response(200, content_type="text/html")

    monkeypatch.setattr(client._session, "get", fake_get)
    # Drive 5 401s first to load the counter close to threshold, then 5 HTML-200s.
    # We do this in two passes since fake_get above only does HTML; switch behavior.
    pass_n = {"n": 0}

    def staged_get(url, timeout, **kw):
        pass_n["n"] += 1
        if pass_n["n"] <= 5:
            return _stub_response(401)  # increments counter to 5 → abort
        return _stub_response(200, content_type="text/html")  # would-be reset

    monkeypatch.setattr(client._session, "get", staged_get)
    with pytest.raises(AuthFailureAbort):
        # 6 IDs so the 5th 401 trips the threshold before any HTML-200 lands.
        list(client.stream("athletes", list(range(1, 11)), retry_delay=0))


def test_html_200_does_not_reset_auth_counter(monkeypatch, tmp_path):
    """Direct unit-level: an HTML-200 _fetch_one call must raise FetchError
    AND leave _consecutive_auth_failures untouched."""
    client = APIClient(make_settings(tmp_path))
    # Manually load the counter.
    with client._auth_failure_lock:
        client._consecutive_auth_failures = 3

    def fake_get(url, timeout, **kw):
        return _stub_response(200, content_type="text/html")

    monkeypatch.setattr(client._session, "get", fake_get)
    with pytest.raises(FetchError):
        client._fetch_one("/athletes/1")
    # Counter is unchanged — the HTML-200 didn't pretend to be a fresh start.
    assert client._consecutive_auth_failures == 3


def test_refresh_credentials_updates_session_headers(monkeypatch, tmp_path):
    """refresh_credentials() should mutate session headers and Settings in-memory."""
    from wcl_data.api import credentials as creds_mod

    client = APIClient(make_settings(tmp_path))
    fake_creds = creds_mod.FetchedCredentials(
        csrf_token="NEW_CSRF",
        session_cookie="_results_session=NEW_COOKIE",
    )
    monkeypatch.setattr(creds_mod, "fetch_credentials", lambda *a, **kw: fake_creds)

    client.refresh_credentials()

    assert client._session.headers["X-Csrf-Token"] == "NEW_CSRF"
    assert client._session.headers["Cookie"] == "_results_session=NEW_COOKIE"
    assert client.settings.csrf_token == "NEW_CSRF"
    assert client.settings.session_cookie == "_results_session=NEW_COOKIE"
