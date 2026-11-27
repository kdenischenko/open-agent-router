"""Server-rendered pages of the web dashboard."""

from __future__ import annotations

import hmac
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from starlette.datastructures import FormData
from starlette.responses import HTMLResponse, PlainTextResponse, Response
from starlette.templating import Jinja2Templates

from agent_router import __version__, api, route_report
from agent_router.clock import format_age, format_local
from agent_router.config import TIERS
from agent_router.diagnostics import doctor_findings
from agent_router.jobs import MODES, STATUSES, TERMINAL, Attempt, Job
from agent_router.periods import parse_moment
from agent_router.service import Gateway, decision_json
from agent_router.usage_reader import UnknownPoolError, usage_drift_warnings

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
# static URLs carry the package version, so a browser does not keep old assets after an upgrade
TEMPLATES.env.globals["asset_version"] = __version__
TEMPLATES.env.filters["age"] = format_age

JOBS_PAGE_SIZE = 50
JOB_SORTS = ("newest", "oldest", "duration")
JOB_FILTER_MAX_CHARS = 200
# the badge colour of each job status; the status name itself is always shown as text
STATUS_KIND = {
    "queued": "info",
    "running": "info",
    "awaiting_approval": "warn",
    "completed": "ok",
    "partial": "warn",
    "failed": "danger",
    "declined": "danger",
    "cancelled": "muted",
}
TIER_TITLES = {"cheap": "Economy", "standard": "Standard", "strong": "Strong"}
LIMIT_STALE_AFTER_S = 6 * 3600
USAGE_MESSAGE_MAX_CHARS = 300  # the flash after a limits refresh, carried in the query


def _countdown(moment: str, now: datetime) -> str:
    seconds = (datetime.fromisoformat(moment) - now).total_seconds()
    return "now" if seconds <= 0 else f"in {format_age(seconds)}"


TEMPLATES.env.filters["countdown"] = _countdown


def job_totals(job: Job, attempts: list[Attempt]) -> dict:
    """Totals over attempts; a field stays None when no attempt has a value for it."""
    durations = [attempt.duration_s for attempt in attempts if attempt.duration_s is not None]
    duration_s: float | None
    if job.finished_at is not None:
        duration_s = (job.finished_at - job.created_at).total_seconds()
    elif durations:
        duration_s = sum(durations)
    else:
        duration_s = None
    return {
        "tokens_in": _sum_field(attempts, "tokens_in"),
        "tokens_out": _sum_field(attempts, "tokens_out"),
        "cost_usd": _sum_field(attempts, "cost_usd"),
        "duration_s": duration_s,
        "attempts": len(attempts),
    }


def _sum_field(attempts: list[Attempt], name: str) -> int | float | None:
    values = [getattr(attempt, name) for attempt in attempts]
    values = [value for value in values if value is not None]
    return sum(values) if values else None


def _field(form: FormData, name: str) -> str:
    value = form.get(name)
    return value if isinstance(value, str) else ""


def web_router(deps: Gateway) -> APIRouter:
    router = APIRouter()
    local = partial(format_local, tz=deps.tz)

    def session_id(request: Request) -> str | None:
        sid = request.cookies.get("ar_session")
        return sid if deps.panel.valid(sid) else None

    def login_redirect() -> RedirectResponse:
        return RedirectResponse("/login", status_code=303)

    def awaiting_count() -> int | None:
        if deps.dispatcher is None:
            return None
        return len(deps.dispatcher.jobs.list("awaiting_approval", 100))

    def render_page(
        request: Request,
        template: str,
        context: dict,
        *,
        sid: str | None,
        nav: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        context = dict(context)
        if sid is not None:
            context["csrf"] = deps.panel.csrf_for(sid)
            context["nav"] = nav
        if nav is not None:
            count = awaiting_count()
            context["awaiting_count"] = count
            context["awaiting_display"] = (
                None if count is None else "99+" if count > 99 else str(count)
            )
        return TEMPLATES.TemplateResponse(request, template, context, status_code=status_code)

    def message_page(
        request: Request,
        sid: str | None,
        message: str,
        href: str,
        link_text: str,
        *,
        status_code: int = 200,
    ) -> HTMLResponse:
        nav = None
        if sid is not None:
            nav = "jobs" if href.startswith("/jobs") else "limits"
        return render_page(
            request,
            "message.html",
            {
                "title": "Notice",
                "message": message,
                "link_href": href,
                "link_text": link_text,
            },
            sid=sid,
            nav=nav,
            status_code=status_code,
        )

    def csrf_failed(request: Request, sid: str) -> HTMLResponse:
        return message_page(
            request, sid, "invalid CSRF token", "/", "Back to the dashboard", status_code=403
        )

    def utc_offset_minutes() -> int:
        """The dashboard time zone offset now, for the calibration preview in the browser."""
        offset = deps.clock.now().astimezone(deps.tz).utcoffset()
        return 0 if offset is None else int(offset.total_seconds() // 60)

    def calibration_page(
        request: Request,
        sid: str,
        *,
        error: str | None = None,
        errors: dict[str, str] | None = None,
        values: dict[str, str] | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        return render_page(
            request,
            "calibrate.html",
            {
                "title": "Manual calibration",
                "pools": deps.config.pools,
                "error": error,
                "errors": errors or {},
                "values": values or {},
                "utc_offset_minutes": utc_offset_minutes(),
            },
            sid=sid,
            nav="calibrate",
            status_code=status_code,
        )

    @router.get("/login")
    def get_login(request: Request) -> Response:
        if session_id(request) is not None:
            return RedirectResponse("/", status_code=303)
        return TEMPLATES.TemplateResponse(request, "login.html", {"title": "Log in", "error": None})

    @router.post("/login")
    async def post_login(request: Request) -> Response:
        form = await request.form()
        given = _field(form, "token").encode("utf-8")
        expected = deps.tokens.admin.encode("utf-8")
        if not given or not hmac.compare_digest(given, expected):
            return TEMPLATES.TemplateResponse(
                request,
                "login.html",
                {"title": "Log in", "error": "admin token not accepted"},
                status_code=401,
            )
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            "ar_session",
            deps.panel.issue(),
            httponly=True,
            samesite="strict",
            path="/",
            secure=False,
            max_age=deps.config.panel_session_days * 86400,
        )
        return response

    @router.post("/logout")
    async def post_logout(request: Request) -> Response:
        sid = session_id(request)
        if sid is None:
            return login_redirect()
        form = await request.form()
        if not deps.panel.csrf_ok(sid, _field(form, "csrf")):
            return csrf_failed(request, sid)
        deps.panel.logout()
        response = login_redirect()
        response.delete_cookie("ar_session", path="/", httponly=True, samesite="strict")
        return response

    def job_filters(request: Request) -> dict[str, str]:
        """Filters from the query string; unknown or malformed values are dropped, not errors."""
        params = request.query_params
        filters: dict[str, str] = {}
        status = params.get("status", "")
        if status in STATUSES:
            filters["status"] = status
        route = params.get("route", "")
        if any(item.id == route for item in deps.config.routes):
            filters["route"] = route
        caller = params.get("caller", "").strip()
        if 0 < len(caller) <= JOB_FILTER_MAX_CHARS:
            filters["caller"] = caller
        q = params.get("q", "").strip()
        if 0 < len(q) <= JOB_FILTER_MAX_CHARS:
            filters["q"] = q
        sort = params.get("sort", "")
        if sort in JOB_SORTS and sort != JOB_SORTS[0]:
            filters["sort"] = sort
        return filters

    def job_page_number(request: Request) -> int:
        raw = request.query_params.get("page", "")
        # a length check first: int() refuses strings of thousands of digits
        return int(raw) if raw.isdecimal() and len(raw) <= 6 and int(raw) > 0 else 1

    def job_list(request: Request) -> dict | None:
        if deps.dispatcher is None:
            return None
        filters = job_filters(request)
        search = {
            "status": filters.get("status"),
            "route": filters.get("route"),
            "caller": filters.get("caller"),
            "q": filters.get("q"),
            "sort": filters.get("sort", JOB_SORTS[0]),
            "limit": JOBS_PAGE_SIZE,
        }
        page = job_page_number(request)
        found, total = deps.dispatcher.jobs.search(**search, offset=(page - 1) * JOBS_PAGE_SIZE)
        pages = max(1, -(-total // JOBS_PAGE_SIZE))
        if page > pages:
            page = pages
            found, total = deps.dispatcher.jobs.search(**search, offset=(page - 1) * JOBS_PAGE_SIZE)
        if "sort" not in filters:
            # jobs waiting for a human come first within the page, as on the home page
            found = sorted(found, key=lambda job: job.status != "awaiting_approval")

        def page_href(number: int) -> str:
            query = {**filters, "page": str(number)} if number > 1 else filters
            return "/jobs" + ("?" + urlencode(query) if query else "")

        return {
            "jobs": found,
            "total": total,
            "page": page,
            "pages": pages,
            "first": (page - 1) * JOBS_PAGE_SIZE + 1 if found else 0,
            "last": (page - 1) * JOBS_PAGE_SIZE + len(found),
            "prev_href": page_href(page - 1) if page > 1 else None,
            "next_href": page_href(page + 1) if page < pages else None,
            "filters": filters,
        }

    def jobs_page(
        request: Request,
        sid: str,
        listing: dict | None,
        *,
        fragment: bool = False,
    ) -> HTMLResponse:
        classifier = None
        if deps.config.classifier.kind != "none":
            matched, compared = (0, 0) if deps.jobs is None else deps.jobs.classifier_agreement()
            classifier = {
                "matched": matched,
                "compared": compared,
                "percent": round(100 * matched / compared) if compared else 0,
            }
        context = {
            "title": "Jobs",
            "jobs": [] if listing is None else listing["jobs"],
            "listing": listing,
            "filters": {} if listing is None else listing["filters"],
            "tasks_disabled": listing is None,
            "classifier": classifier,
            "statuses": STATUSES,
            "routes": [item.id for item in deps.config.routes],
            "callers": sorted(deps.config.callers),
            "sorts": JOB_SORTS,
            "status_kind": STATUS_KIND,
            "local": local,
        }
        template = "_jobs.html" if fragment else "jobs.html"
        return render_page(request, template, context, sid=sid, nav=None if fragment else "jobs")

    def approval_items() -> list[dict]:
        if deps.dispatcher is None:
            return []
        found = deps.dispatcher.jobs.list("awaiting_approval", 100)
        now = deps.clock.now()
        items = []
        for job in found:
            route = next(
                (item for item in deps.config.routes if item.id == job.approval_route), None
            )
            pool = (
                next((item for item in deps.config.pools if item.id == route.pool), None)
                if route is not None
                else None
            )
            remaining = (
                (job.approval_deadline - now).total_seconds()
                if job.approval_deadline is not None
                else 0.0
            )
            prompt = job.request.prompt
            items.append(
                {
                    "job": job,
                    "pool_title": pool.title if pool is not None else None,
                    "prompt": prompt[:200],
                    "prompt_truncated": len(prompt) > 200,
                    "expired": remaining <= 0,
                    "countdown": "expired" if remaining <= 0 else f"in {format_age(remaining)}",
                }
            )
        return items

    def usage_refresh_pools() -> set[str]:
        """Pools whose card offers a refresh: a source is built and the mode is not manual."""
        reader = deps.usage_reader
        if reader is None:
            return set()
        return {
            pool.id
            for pool in deps.config.pools
            if reader.state(pool.id) is not None and deps.config.usage_mode(pool.id) != "manual"
        }

    def usage_warnings(snapshot: dict) -> dict[str, list[str]]:
        configured = {pool.id: pool for pool in deps.config.pools}
        warnings = {}
        for pool in snapshot["pools"]:
            info = pool.get("usage")
            settings = configured.get(pool["id"])
            if info is None or settings is None or info["mode"] == "manual":
                continue
            messages = usage_drift_warnings(
                info["source"],
                info.get("missing_fields", []),
                info.get("format_changed", False),
                settings.usage.windows,
            )
            if messages:
                warnings[pool["id"]] = messages
        return warnings

    def spend_chart(pool, chart_id: str, title: str, starts: list[datetime], end: datetime) -> dict:
        rows = deps.jobs.spend_buckets(pool.id, starts, end)
        by_start = {row.start: row for row in rows}
        buckets = []
        values = []
        for start in starts:
            row = by_start.get(start)
            value = 0.0 if row is None else row.cost_usd if pool.unit == "usd" else row.tokens
            values.append(value)
            displayed = f"{value:.2f} USD" if pool.unit == "usd" else f"{int(value)} tokens"
            label = (
                local(start) if chart_id == "day" else start.astimezone(deps.tz).strftime("%d.%m")
            )
            buckets.append(
                {
                    "label": label,
                    "iso": start.isoformat(),
                    "value": displayed,
                    "estimated": row is not None and row.estimated,
                }
            )
        slot = 600 / len(starts)
        peak = max(values, default=0)
        bars = []
        for index, value in enumerate(values):
            if value <= 0:
                continue
            height = 100 * value / peak
            bars.append(
                {
                    "x": f"{index * slot + 1:.2f}",
                    "y": f"{110 - height:.2f}",
                    "width": f"{slot - 2:.2f}",
                    "height": f"{height:.2f}",
                }
            )
        total = f"{sum(values):.2f} USD" if pool.unit == "usd" else f"{int(sum(values))} tokens"
        estimated = any(row.estimated for row in rows)
        return {
            "id": chart_id,
            "title": title,
            "aria_label": f"{pool.title}, {title}: {total}{', estimated' if estimated else ''}",
            "total": total,
            "estimated": estimated,
            "buckets": buckets,
            "bars": bars,
            "has_data": bool(rows),
        }

    def spend_charts(now: datetime) -> dict[str, list[dict] | None]:
        if deps.jobs is None:
            return {}
        result = {}
        day_starts = [now - timedelta(hours=24 - index) for index in range(24)]
        local_now = now.astimezone(deps.tz)
        today = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_starts = [today - timedelta(days=6 - index) for index in range(7)]
        for pool in deps.config.pools:
            charts = [
                spend_chart(pool, "day", "Last 24 hours", day_starts, now),
                spend_chart(pool, "week", "Last 7 days", week_starts, now),
            ]
            result[pool.id] = charts if any(chart["has_data"] for chart in charts) else None
        return result

    def limits_context(request: Request, snapshot: dict) -> dict:
        now = deps.clock.now()
        return {
            "pools": snapshot["pools"],
            "local": local,
            "now": now,
            "stale_after_s": LIMIT_STALE_AFTER_S,
            "recorded_pool": recorded_pool(request),
            "usage_refresh": usage_refresh_pools(),
            "usage_warnings": usage_warnings(snapshot),
            "spend_charts": spend_charts(now),
        }

    def advice_context(snapshot: dict) -> dict:
        decisions = [
            decision_json(tier, deps.advise(tier, None)) | {"title": TIER_TITLES[tier]}
            for tier in TIERS
        ]
        return {
            "decisions": decisions,
            "closed_pools": [pool for pool in snapshot["pools"] if pool["block"]],
            "local": local,
        }

    def routes_context() -> dict:
        """Per-route scorecard over the last 30 days; None without a job store."""
        if deps.jobs is None:
            return {"routes_report": None}
        if not hasattr(deps.jobs, "route_samples_since"):
            return {"routes_report": None, "routes_unavailable": True}
        since = deps.clock.now() - timedelta(days=30)
        jobs, attempts = deps.jobs.route_samples_since(since)
        quality = {route.id: route.quality for route in deps.config.routes}
        return {"routes_report": route_report.build(jobs, attempts, quality)}

    def recorded_pool(request: Request) -> str | None:
        """The pool of the recorded pair in the query, or None unless the pair is configured."""
        pool_id = request.query_params.get("recorded_pool")
        window_id = request.query_params.get("recorded_window")
        pool = next((item for item in deps.config.pools if item.id == pool_id), None)
        if pool is None or not any(window.id == window_id for window in pool.windows):
            return None
        return pool.id

    def recorded_line(request: Request, snapshot: dict) -> str | None:
        """The confirmation line for a just-recorded pair, or None when the params are absent."""
        pool_id = request.query_params.get("recorded_pool")
        window_id = request.query_params.get("recorded_window")
        if not pool_id or not window_id:
            return None
        pool = next((item for item in deps.config.pools if item.id == pool_id), None)
        if pool is None or not any(window.id == window_id for window in pool.windows):
            return None
        found = next(
            (
                window
                for item in snapshot["pools"]
                if item["id"] == pool_id
                for window in item["windows"]
                if window["id"] == window_id
            ),
            None,
        )
        if found is None or found.get("used") is None:
            return None
        line = f"Recorded {pool.title} / {window_id}: used {found['used'] * 100:.0f}%"
        resets_at = found.get("resets_at")
        if resets_at:
            line += f", reset {local(resets_at)}"
        return line

    def usage_message(request: Request) -> str | None:
        """The flash line after POST /limits/refresh; the redirect carries it in the query."""
        message = request.query_params.get("usage_message", "").strip()
        return message[:USAGE_MESSAGE_MAX_CHARS] or None

    @router.get("/")
    def get_index(request: Request) -> Response:
        sid = session_id(request)
        if sid is None:
            return login_redirect()
        snapshot = deps.snapshot_view()
        context = (
            {
                "title": "Limits",
                "recorded": recorded_line(request, snapshot),
                "usage_message": usage_message(request),
                "approvals": approval_items(),
            }
            | limits_context(request, snapshot)
            | advice_context(snapshot)
            | routes_context()
        )
        return render_page(request, "index.html", context, sid=sid, nav="limits")

    @router.get("/fragments/approvals")
    def get_approvals_fragment(request: Request) -> Response:
        sid = session_id(request)
        if sid is None:
            return login_redirect()
        return render_page(request, "_approvals.html", {"approvals": approval_items()}, sid=sid)

    @router.get("/fragments/limits")
    def get_limits(request: Request) -> Response:
        sid = session_id(request)
        if sid is None:
            return login_redirect()
        snapshot = deps.snapshot_view()
        return render_page(request, "_limits.html", limits_context(request, snapshot), sid=sid)

    @router.get("/fragments/advice")
    def get_advice(request: Request) -> Response:
        sid = session_id(request)
        if sid is None:
            return login_redirect()
        snapshot = deps.snapshot_view()
        return render_page(request, "_advice.html", advice_context(snapshot), sid=sid)

    @router.get("/jobs")
    def get_jobs(request: Request) -> Response:
        sid = session_id(request)
        if sid is None:
            return login_redirect()
        return jobs_page(request, sid, job_list(request))

    submit_error_fields = {
        "bad_prompt": "prompt",
        "bad_mode": "mode",
        "bad_tier": "tier",
        "bad_pool": "exclude_pools",
        "bad_timeout": "timeout_s",
        "cwd_not_allowed": "cwd",
        "not_a_git_repo": "cwd",
        "git_unavailable": "cwd",
    }

    def submit_page(
        request: Request,
        sid: str,
        *,
        error: str | None = None,
        errors: dict[str, str] | None = None,
        values: dict[str, str | list[str]] | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        return render_page(
            request,
            "submit.html",
            {
                "title": "New job",
                "error": error,
                "errors": errors or {},
                "values": values or {"mode": "read"},
                "modes": MODES,
                "tiers": TIERS,
                "pools": deps.config.pools,
            },
            sid=sid,
            nav="jobs",
            status_code=status_code,
        )

    @router.get("/jobs/new")
    def get_job_submit(request: Request) -> Response:
        sid = session_id(request)
        if sid is None:
            return login_redirect()
        if deps.dispatcher is None:
            return jobs_page(request, sid, None)
        return submit_page(request, sid)

    @router.post("/jobs/new")
    async def post_job_submit(request: Request) -> Response:
        sid = session_id(request)
        if sid is None:
            return login_redirect()
        form = await request.form()
        if not deps.panel.csrf_ok(sid, _field(form, "csrf")):
            return csrf_failed(request, sid)
        values = {
            name: _field(form, name) for name in ("cwd", "mode", "tier", "prompt", "timeout_s")
        }
        exclude_pools = [
            value for value in form.getlist("exclude_pools") if isinstance(value, str) and value
        ]
        values["exclude_pools"] = exclude_pools
        raw_timeout = values["timeout_s"]
        try:
            timeout_s = int(raw_timeout) if raw_timeout else None
        except ValueError:
            timeout_s = raw_timeout
        try:
            job = api.submit_job(
                deps,
                api.JobBody(
                    prompt=values["prompt"],
                    cwd=values["cwd"],
                    mode=values["mode"],
                    tier=values["tier"] or None,
                    timeout_s=timeout_s,
                    exclude_pools=exclude_pools,
                ),
            )
        except api.ApiError as exc:
            field = submit_error_fields.get(exc.code)
            return submit_page(
                request,
                sid,
                error="check the job field values" if field else exc.message,
                errors={field: exc.message} if field else {},
                values=values,
                status_code=exc.status,
            )
        except ValidationError:
            return submit_page(
                request,
                sid,
                error="check the job field values",
                errors={"exclude_pools": "invalid excluded pool selection"},
                values=values,
                status_code=422,
            )
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    @router.get("/fragments/jobs")
    def get_jobs_fragment(request: Request) -> Response:
        sid = session_id(request)
        if sid is None:
            return login_redirect()
        return jobs_page(request, sid, job_list(request), fragment=True)

    def job_context(job: Job, attempts: list[Attempt]) -> dict:
        prompt_chars = job.prompt_chars if job.prompt_chars is not None else len(job.request.prompt)
        return {
            "title": f"Job {job.id}",
            "job": job,
            "prompt": job.request.prompt[:2000],
            "prompt_truncated": prompt_chars > 2000,
            "prompt_chars": prompt_chars,
            "attempts": attempts,
            "totals": job_totals(job, attempts),
            "status_kind": STATUS_KIND,
            "caller": job.request.caller,
            "last_rating": deps.dispatcher.jobs.last_rating(job.id),
            "local": local,
            "terminal": job.status in TERMINAL,
        }

    @router.get("/jobs/{job_id}")
    def get_job(request: Request, job_id: str) -> Response:
        sid = session_id(request)
        if sid is None:
            return login_redirect()
        if deps.dispatcher is None:
            return jobs_page(request, sid, None)
        job = deps.dispatcher.jobs.get(job_id)
        if job is None:
            return PlainTextResponse("job not found", status_code=404)
        context = job_context(job, deps.dispatcher.jobs.attempts(job_id))
        return render_page(request, "job.html", context, sid=sid, nav="jobs")

    @router.get("/fragments/jobs/{job_id}")
    def get_job_fragment(request: Request, job_id: str) -> Response:
        sid = session_id(request)
        if sid is None:
            return login_redirect()
        if deps.dispatcher is None:
            return jobs_page(request, sid, None)
        job = deps.dispatcher.jobs.get(job_id)
        if job is None:
            return PlainTextResponse("job not found", status_code=404)
        context = job_context(job, deps.dispatcher.jobs.attempts(job_id))
        return render_page(request, "_job.html", context, sid=sid)

    CONFIRM_ACTIONS = {
        "deny": {
            "title": "Deny the route",
            "heading": "Deny this route?",
            "text": "the route is excluded and the job goes back to the queue",
            "button": "Deny",
        },
        "cancel": {
            "title": "Cancel the job",
            "heading": "Cancel this job?",
            "text": "the job stops and is marked as cancelled",
            "button": "Cancel",
        },
    }

    def get_job_confirmation(request: Request, job_id: str, action: str) -> Response:
        sid = session_id(request)
        if sid is None:
            return login_redirect()
        if deps.dispatcher is None:
            return PlainTextResponse(
                "jobs are disabled: the job dispatcher is not running", status_code=503
            )
        job = deps.dispatcher.jobs.get(job_id)
        if job is None:
            return PlainTextResponse("job not found", status_code=404)
        context = {"action": action, "job": job} | CONFIRM_ACTIONS[action]
        return render_page(request, "confirm_action.html", context, sid=sid, nav="jobs")

    @router.get("/jobs/{job_id}/deny")
    def get_job_deny(request: Request, job_id: str) -> Response:
        return get_job_confirmation(request, job_id, "deny")

    @router.get("/jobs/{job_id}/cancel")
    def get_job_cancel(request: Request, job_id: str) -> Response:
        return get_job_confirmation(request, job_id, "cancel")

    async def post_job_action(request: Request, job_id: str, action: str) -> Response:
        sid = session_id(request)
        if sid is None:
            return login_redirect()
        form = await request.form()
        if not deps.panel.csrf_ok(sid, _field(form, "csrf")):
            return csrf_failed(request, sid)
        if deps.dispatcher is None:
            return PlainTextResponse(
                "jobs are disabled: the job dispatcher is not running", status_code=503
            )
        try:
            getattr(deps.dispatcher, action)(job_id)
        except KeyError:
            return message_page(
                request, sid, "job not found", f"/jobs/{job_id}", "Back to the job", status_code=404
            )
        except api.WrongState as exc:
            return message_page(
                request, sid, str(exc), f"/jobs/{job_id}", "Back to the job", status_code=409
            )
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @router.post("/jobs/{job_id}/approve")
    async def post_job_approve(request: Request, job_id: str) -> Response:
        return await post_job_action(request, job_id, "approve")

    @router.post("/jobs/{job_id}/deny")
    async def post_job_deny(request: Request, job_id: str) -> Response:
        return await post_job_action(request, job_id, "deny")

    @router.post("/jobs/{job_id}/cancel")
    async def post_job_cancel(request: Request, job_id: str) -> Response:
        return await post_job_action(request, job_id, "cancel")

    @router.post("/jobs/{job_id}/rate")
    async def post_job_rate(request: Request, job_id: str) -> Response:
        sid = session_id(request)
        if sid is None:
            return login_redirect()
        form = await request.form()
        if not deps.panel.csrf_ok(sid, _field(form, "csrf")):
            return csrf_failed(request, sid)
        if deps.dispatcher is None:
            return PlainTextResponse(
                "jobs are disabled: the job dispatcher is not running", status_code=503
            )
        try:
            api.rate(
                deps,
                job_id,
                api.RatingBody(
                    value=_field(form, "value"),
                    should_tier=_field(form, "should_tier") or None,
                    by="dashboard",
                ),
            )
        except api.ApiError as exc:
            return message_page(
                request,
                sid,
                exc.message,
                f"/jobs/{job_id}",
                "Back to the job",
                status_code=exc.status,
            )
        except ValidationError:
            return message_page(
                request,
                sid,
                "check the rating values",
                f"/jobs/{job_id}",
                "Back to the job",
                status_code=422,
            )
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @router.post("/pools/{pool_id}/unblock")
    async def post_pool_unblock(request: Request, pool_id: str) -> Response:
        sid = session_id(request)
        if sid is None:
            return login_redirect()
        form = await request.form()
        if not deps.panel.csrf_ok(sid, _field(form, "csrf")):
            return csrf_failed(request, sid)
        if deps.dispatcher is None:
            return PlainTextResponse(
                "jobs are disabled: the job dispatcher is not running", status_code=503
            )
        if deps.pool(pool_id) is None:
            return PlainTextResponse("pool not found", status_code=404)
        deps.dispatcher.unblock(pool_id)
        return RedirectResponse("/", status_code=303)

    @router.post("/limits/refresh")
    async def post_limits_refresh(request: Request) -> Response:
        """Reads the usage sources now: one pool from the form field, or all of them."""
        sid = session_id(request)
        if sid is None:
            return login_redirect()
        form = await request.form()
        if not deps.panel.csrf_ok(sid, _field(form, "csrf")):
            return csrf_failed(request, sid)
        reader = deps.usage_reader
        if reader is None:
            return message_page(
                request, sid, "usage sources are not available", "/", "Back to the dashboard"
            )
        pool = _field(form, "pool") or None
        try:
            result = reader.refresh(pool)
        except UnknownPoolError:
            return PlainTextResponse("pool not found", status_code=404)
        parts = [f"Read: {item.pool} ({', '.join(item.windows)})" for item in result.read]
        parts += [f"Failed: {item.pool} — {item.reason}" for item in result.failed]
        parts += [f"Skipped: {item.pool} — {item.reason}" for item in result.skipped]
        anchor = f"#limit-{pool}" if pool is not None else "#limits"
        query = urlencode({"usage_message": "; ".join(parts)}) if parts else ""
        return RedirectResponse(f"/{'?' + query if query else ''}{anchor}", status_code=303)

    def settings_context() -> dict:
        pools = []
        for pool in deps.config.pools:
            usage = None
            if pool.usage.source is not None or pool.usage.mode is not None:
                usage = {
                    "source": pool.usage.source,
                    "mode": deps.config.usage_mode(pool.id),
                    "api_key_env": pool.usage.api_key_env,
                }
            pools.append(
                {
                    "id": pool.id,
                    "title": pool.title,
                    "unit": pool.unit,
                    "usage": usage,
                    "windows": [
                        {
                            "id": window.id,
                            "kind": window.kind,
                            "role": window.role,
                            "duration": str(window.duration),
                            "anchor": window.anchor.isoformat() if window.anchor else None,
                        }
                        for window in pool.windows
                    ],
                }
            )
        routes = [
            {
                "id": route.id,
                "kind": route.kind,
                "pool": route.pool,
                "backend": route.backend if route.kind == "harness" else route.upstream,
                "model": route.model,
                "tiers": [tier for tier in TIERS if tier in route.tiers],
                "modes": [mode for mode in ("read", "write") if mode in route.modes],
                "enabled": route.enabled,
            }
            for route in deps.config.routes
        ]
        return {
            "title": "Settings",
            "pools": pools,
            "routes": routes,
            "callers": [
                {"name": name, "pool": pool} for name, pool in sorted(deps.config.callers.items())
            ],
            "findings": doctor_findings(deps.config),
        }

    @router.get("/settings")
    def get_settings(request: Request) -> Response:
        sid = session_id(request)
        if sid is None:
            return login_redirect()
        return render_page(request, "settings.html", settings_context(), sid=sid, nav="settings")

    @router.get("/calibrate")
    def get_calibration(request: Request) -> Response:
        sid = session_id(request)
        if sid is None:
            return login_redirect()
        return calibration_page(request, sid)

    @router.post("/calibrate")
    async def post_calibration(request: Request) -> Response:
        sid = session_id(request)
        if sid is None:
            return login_redirect()
        form = await request.form()
        if not deps.panel.csrf_ok(sid, _field(form, "csrf")):
            return csrf_failed(request, sid)
        values = {
            name: _field(form, name)
            for name in ("target", "percent", "resets_date", "resets_time", "resets_in")
        }

        def invalid(
            message: str, field: str, *, status: int = 422, field_message: str | None = None
        ) -> HTMLResponse:
            return calibration_page(
                request,
                sid,
                error=message,
                errors={field: field_message or message},
                values=values,
                status_code=status,
            )

        target = values["target"]
        if not target:
            return invalid("choose a pool and window", "target")
        reset_date, reset_time = values["resets_date"], values["resets_time"]
        resets_in = values["resets_in"] or None
        if reset_time and not reset_date:
            return invalid("set the reset date", "resets_date")
        if reset_date and resets_in:
            return invalid("set either the reset date or reset in, not both", "resets_in")
        try:
            pool_index, window_index = (int(part) for part in target.split(":"))
            if pool_index < 0 or window_index < 0:
                raise ValueError
            pool = deps.config.pools[pool_index]
            window = pool.windows[window_index]
        except (ValueError, IndexError):
            return invalid("check the calibration field values", "target")
        bad_percent = {"field_message": "enter a number from 0 to 100"}
        try:
            percent = float(values["percent"])
        except ValueError:
            return invalid("check the calibration field values", "percent", **bad_percent)
        try:
            resets_at = parse_moment(f"{reset_date} {reset_time}", deps.tz) if reset_date else None
        except ValueError:
            return invalid("check the calibration field values", "resets_date")
        try:
            # every other field is already checked: a validation error here is the percent range
            body = api.ObservationBody(
                pool=pool.id,
                window=window.id,
                percent=percent,
                resets_at=resets_at,
                resets_in=resets_in,
            )
        except ValidationError:
            return invalid("check the calibration field values", "percent", **bad_percent)
        try:
            api.calibrate(deps, body)
        except api.ApiError as exc:
            if exc.code in {"unknown_pool", "unknown_window"}:
                field = "target"
            else:
                field = "resets_in" if resets_in else "resets_date"
            return invalid(exc.message, field, status=exc.status)
        query = urlencode({"recorded_pool": pool.id, "recorded_window": window.id})
        return RedirectResponse(f"/?{query}#limit-{pool.id}", status_code=303)

    return router
