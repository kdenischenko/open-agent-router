from __future__ import annotations

import json

from agent_router.api import FORM_BODY_MAX_BYTES, api_body_limit
from helpers import bearer


def assert_too_large(response) -> None:
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "body_too_large"


def test_oversized_content_length_rejected_before_auth(client, config):
    body = b"x" * (api_body_limit(config) + 1)
    response = client.post(
        "/api/v1/jobs", content=body, headers={"Content-Type": "application/json"}
    )
    assert_too_large(response)


def test_oversized_streamed_body_rejected(client, config, tokens):
    limit = api_body_limit(config)

    def chunks():
        sent = 0
        while sent <= limit:
            yield b"x" * 65536
            sent += 65536

    response = client.post(
        "/api/v1/jobs",
        content=chunks(),
        headers=bearer(tokens.client) | {"Content-Type": "application/json"},
    )
    assert_too_large(response)


def test_largest_accepted_prompt_fits_the_limit(client, config, tokens, tmp_path):
    # a character outside the BMP is the worst case: twelve bytes as an escaped surrogate pair
    prompt = "\U0001f600" * config.tasks.max_prompt_chars
    body = json.dumps({"prompt": prompt, "cwd": str(tmp_path), "mode": "read"}).encode()
    response = client.post(
        "/api/v1/jobs",
        content=body,
        headers=bearer(tokens.client) | {"Content-Type": "application/json"},
    )
    assert response.status_code != 413


def test_dashboard_form_has_a_small_limit(client):
    body = b"token=" + b"x" * FORM_BODY_MAX_BYTES
    response = client.post(
        "/login", content=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    assert_too_large(response)


def test_small_bodies_pass(client):
    response = client.post(
        "/login",
        content=b"token=wrong",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 401
