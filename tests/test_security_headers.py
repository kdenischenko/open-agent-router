"""Security headers on dashboard pages, fragments and API responses; static assets keep caching."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from test_web_dashboard import login
from test_web_jobs import WebRig

SECURITY_HEADERS = {
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
}


@pytest.fixture
def rig(home, config, store, jobstore, clock, tokens) -> WebRig:
    return WebRig(home, config, store, jobstore, clock, tokens)


@pytest.fixture
def web(rig) -> TestClient:
    with TestClient(rig.app, base_url="http://127.0.0.1:8787") as client:
        assert login(client, rig.tokens.admin).status_code == 303
        yield client


def assert_secured(response) -> None:
    for name, value in SECURITY_HEADERS.items():
        assert response.headers.get(name) == value, name


@pytest.mark.parametrize("path", ["/", "/jobs", "/fragments/jobs", "/fragments/approvals"])
def test_dashboard_responses_carry_security_headers(web: TestClient, path: str) -> None:
    response = web.get(path)

    assert response.status_code == 200
    assert_secured(response)


def test_login_page_carries_security_headers(rig) -> None:
    with TestClient(rig.app, base_url="http://127.0.0.1:8787") as client:
        assert_secured(client.get("/login"))


def test_api_response_carries_security_headers(rig) -> None:
    with TestClient(rig.app, base_url="http://127.0.0.1:8787") as client:
        response = client.get(
            "/api/v1/jobs", headers={"Authorization": f"Bearer {rig.tokens.client}"}
        )

    assert response.status_code == 200
    assert_secured(response)


def test_api_error_carries_security_headers(rig) -> None:
    with TestClient(rig.app, base_url="http://127.0.0.1:8787") as client:
        response = client.get("/api/v1/jobs")

    assert response.status_code == 401
    assert_secured(response)


def test_rejected_host_carries_security_headers(rig) -> None:
    with TestClient(rig.app, base_url="http://evil.example:8787") as client:
        response = client.get("/healthz")

    assert response.status_code == 400
    assert_secured(response)


def test_static_asset_keeps_normal_caching(rig) -> None:
    with TestClient(rig.app, base_url="http://127.0.0.1:8787") as client:
        response = client.get("/static/app.css")

    assert response.status_code == 200
    assert "no-store" not in response.headers.get("cache-control", "")


def test_static_asset_carries_the_other_security_headers(rig) -> None:
    with TestClient(rig.app, base_url="http://127.0.0.1:8787") as client:
        response = client.get("/static/app.css")

    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("x-frame-options") == "DENY"


def test_missing_static_file_carries_security_headers(rig) -> None:
    with TestClient(rig.app, base_url="http://127.0.0.1:8787") as client:
        response = client.get("/static/missing.css")

    assert response.status_code == 404
    assert_secured(response)


def test_rejected_host_on_a_static_path_carries_security_headers(rig) -> None:
    with TestClient(rig.app, base_url="http://evil.example:8787") as client:
        response = client.get("/static/app.css")

    assert response.status_code == 400
    assert_secured(response)
