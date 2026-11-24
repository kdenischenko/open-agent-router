"""OpenAI-compatible proxy: /v1 model requests routed across the configured api routes.

A client (a coding CLI, an agent, a script) points its OpenAI-compatible base URL at the
gateway and authenticates with the client token. The gateway maps the requested model to
the api routes that serve it, picks one with the route policy over the pools' quota state,
forwards the request to the route's upstream (for example LM Studio or Strata on
loopback), and records the spend with source "proxy". When the first route's upstream
cannot be reached, the next one is tried; an upstream that answers with an error status
speaks for itself and its response is passed through unchanged.
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from agent_router.auth import Role
from agent_router.classifier_backends import is_loopback_url
from agent_router.config import Config, Route, UpstreamSettings
from agent_router.policy import RouteRequest, decide
from agent_router.service import Gateway, route_views
from agent_router.spend import SpendEntry, estimate_proxy_cost

# default upstream timeout; route.timeout_s replaces it. The connect timeout stays short so
# a dead upstream fails fast and the request falls back to the next route.
DEFAULT_TIMEOUT_S = 600.0
CONNECT_TIMEOUT_S = 10.0

# cap on the buffered stream tail scanned for the usage event
USAGE_BUFFER_MAX = 2**20


class _KeyMissing(Exception):
    """The upstream declares a key source, but the key is not there."""


def upstream_key(upstream: UpstreamSettings) -> str | None:
    """The upstream's key, read at request time; None when the upstream takes no key."""
    if upstream.key_env is not None:
        key = os.environ.get(upstream.key_env)
        if not key:
            raise _KeyMissing(f"environment variable {upstream.key_env} is not set")
        return key
    if upstream.key_file is not None:
        try:
            key = Path(upstream.key_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise _KeyMissing(f"key file is not readable: {exc.strerror or exc}") from exc
        if not key:
            raise _KeyMissing("key file is empty")
        return key
    return None


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _timeout(route: Route) -> httpx.Timeout:
    total = float(route.timeout_s) if route.timeout_s else DEFAULT_TIMEOUT_S
    return httpx.Timeout(total, connect=CONNECT_TIMEOUT_S)


def _upstream_body(
    payload: Mapping[str, Any], route: Route, upstream: UpstreamSettings, stream: bool
) -> dict[str, Any]:
    body = dict(payload)
    if route.model is not None:
        body["model"] = route.model  # the requested id may be the route's model_key alias
    if stream and upstream.stream_usage:
        options = body.get("stream_options")
        options = dict(options) if isinstance(options, dict) else {}
        options["include_usage"] = True
        body["stream_options"] = options
    return body


def _upstream_headers(upstream: UpstreamSettings, key: str | None, request: Request) -> dict:
    headers = {}
    for name in upstream.pass_headers:
        value = request.headers.get(name)
        if value is not None:
            headers[name] = value
    if key is not None:
        headers["authorization"] = f"Bearer {key}"
    return headers


def _token_count(usage: Mapping[str, Any], field: str) -> int | None:
    value = usage.get(field)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _record_spend(gateway: Gateway, route: Route, usage: Mapping[str, Any] | None) -> None:
    jobs = gateway.jobs
    if jobs is None:
        return
    tokens_in = _token_count(usage, "prompt_tokens") if usage else None
    tokens_out = _token_count(usage, "completion_tokens") if usage else None
    pool = next((item for item in gateway.config.pools if item.id == route.pool), None)
    estimated = (
        estimate_proxy_cost(pool, route, tokens_in, tokens_out) if pool is not None else None
    )
    cost, version = estimated if estimated is not None else (None, None)
    entry = SpendEntry(
        pool=route.pool,
        at=gateway.clock.now(),
        source="proxy",
        route=route.id,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost,
        estimated=estimated is not None,
        prices_version=version,
        ref_id=f"px-{uuid.uuid4().hex}",
        ref_n=1,
    )
    with contextlib.suppress(Exception):
        # a spend write must not fail a request the upstream already served
        jobs.add_spend(entry)


def _scan_usage(buffer: bytearray, chunk: bytes) -> Mapping[str, Any] | None:
    """Usage object of the stream's terminal event, scanned from complete SSE lines."""
    buffer.extend(chunk)
    if len(buffer) > USAGE_BUFFER_MAX:
        buffer.clear()  # pathological event: stop scanning, the request still streams
        return None
    found = None
    while (at := buffer.find(b"\n")) >= 0:
        line = bytes(buffer[:at])
        del buffer[: at + 1]
        if not line.startswith(b"data:"):
            continue
        try:
            event = json.loads(line[5:].strip())
        except ValueError:
            continue
        if isinstance(event, dict) and isinstance(event.get("usage"), dict):
            found = event["usage"]
    return found


async def _stream_and_record(
    gateway: Gateway,
    client: httpx.AsyncClient,
    context: Any,
    response: httpx.Response,
    route: Route,
) -> AsyncIterator[bytes]:
    usage = None
    buffer = bytearray()
    try:
        async for chunk in response.aiter_raw():
            usage = _scan_usage(buffer, chunk) or usage
            yield chunk
    finally:
        await context.__aexit__(None, None, None)
        await client.aclose()
        _record_spend(gateway, route, usage)


def _media_type(response: httpx.Response, fallback: str) -> str:
    return response.headers.get("content-type", fallback).split(";")[0].strip()


async def _send(
    gateway: Gateway, upstream: UpstreamSettings, route: Route, body: dict, headers: dict
) -> Response:
    async with httpx.AsyncClient(
        trust_env=not is_loopback_url(upstream.base_url), timeout=_timeout(route)
    ) as client:
        response = await client.post(
            f"{upstream.base_url}/chat/completions", json=body, headers=headers
        )
    if response.status_code >= 400:
        return _passthrough(response)
    usage = None
    with contextlib.suppress(ValueError):
        parsed = response.json()
        if isinstance(parsed, dict) and isinstance(parsed.get("usage"), dict):
            usage = parsed["usage"]
    _record_spend(gateway, route, usage)
    return _passthrough(response)


def _passthrough(response: httpx.Response) -> Response:
    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=_media_type(response, "application/json"),
    )


async def _open_stream(
    gateway: Gateway, upstream: UpstreamSettings, route: Route, body: dict, headers: dict
) -> Response:
    client = httpx.AsyncClient(
        trust_env=not is_loopback_url(upstream.base_url), timeout=_timeout(route)
    )
    try:
        context = client.stream(
            "POST", f"{upstream.base_url}/chat/completions", json=body, headers=headers
        )
        response = await context.__aenter__()
    except BaseException:
        await client.aclose()
        raise
    if response.status_code >= 400:
        await response.aread()
        await context.__aexit__(None, None, None)
        await client.aclose()
        return _passthrough(response)
    return StreamingResponse(
        _stream_and_record(gateway, client, context, response, route),
        status_code=response.status_code,
        media_type=_media_type(response, "text/event-stream"),
    )


async def _chat(gateway: Gateway, request: Request) -> Response:
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return _error(400, "bad_request", "malformed json body")
    if not isinstance(payload, dict):
        return _error(400, "bad_request", "a json object is expected")
    model = payload.get("model")
    if not isinstance(model, str) or not model:
        return _error(400, "bad_request", "model is required")
    config = gateway.config
    candidates = {
        route.id: route
        for route in config.routes
        if route.kind == "api" and model in (route.model, route.model_group)
    }
    if not candidates:
        available = sorted(
            {route.model or route.model_group for route in config.routes if route.kind == "api"}
        )
        return _error(
            404, "model_not_found", f"unknown model: {model}; available: {', '.join(available)}"
        )
    availability = {}
    for route in candidates.values():
        upstream = config.upstreams.get(route.upstream or "")
        availability[route.id] = (
            None if upstream is not None and upstream.enabled else "upstream disabled"
        )
    views = [view for view in route_views(config, availability) if view.route.id in candidates]
    decision = decide(RouteRequest(tier="standard", kind="api"), gateway.snapshot(), views)
    if not decision.chosen:
        reasons = "; ".join(f"{item.route.id} ({item.reason})" for item in decision.excluded)
        return _error(503, "no_route", f"no route for model {model}: {reasons}")
    stream = payload.get("stream") is True
    unreachable = []
    for choice in decision.chosen:
        route = choice.route
        upstream = config.upstreams[route.upstream or ""]
        try:
            key = upstream_key(upstream)
        except _KeyMissing as exc:
            return _error(500, "upstream_key_missing", f"upstream {upstream.name}: {exc}")
        body = _upstream_body(payload, route, upstream, stream)
        headers = _upstream_headers(upstream, key, request)
        try:
            if stream:
                return await _open_stream(gateway, upstream, route, body, headers)
            return await _send(gateway, upstream, route, body, headers)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            unreachable.append(f"{upstream.name} ({type(exc).__name__})")
        except httpx.ReadTimeout:
            return _error(504, "upstream_timeout", f"upstream {upstream.name} did not answer")
    return _error(
        502,
        "upstream_unavailable",
        f"no upstream reachable for model {model}: {', '.join(unreachable)}",
    )


def _models_json(config: Config) -> dict[str, Any]:
    data = []
    seen = set()
    for route in config.routes:
        if route.kind != "api" or not route.enabled:
            continue
        for name in dict.fromkeys((route.model, route.model_key)):
            if not name or name in seen:
                continue
            seen.add(name)
            data.append({"id": name, "object": "model", "created": 0, "owned_by": route.pool})
    return {"object": "list", "data": data}


def proxy_router(gateway: Gateway, client: Any) -> APIRouter:
    """/v1 model endpoints; `client` is the client-role auth dependency from the API app."""
    router = APIRouter()

    @router.get("/v1/models")
    async def list_models(role: Role = client) -> dict[str, Any]:
        return _models_json(gateway.config)

    @router.post("/v1/chat/completions")
    async def chat_completions(request: Request, role: Role = client) -> Response:
        return await _chat(gateway, request)

    return router
