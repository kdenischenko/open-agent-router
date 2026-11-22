"""Server-rendered pages of the web dashboard."""

from __future__ import annotations

import hmac
from datetime import datetime
from functools import partial
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from starlette.datastructures import FormData
from starlette.responses import HTMLResponse, PlainTextResponse, Response
from starlette.templating import Jinja2Templates

from agent_router import __version__, api
from agent_router.clock import format_age, format_local
from agent_router.config import TIERS
from agent_router.jobs import TERMINAL, Attempt, Job
from agent_router.periods import parse_moment
from agent_router.service import Gateway, decision_json

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
# static URLs carry the package version, so a browser does not keep old assets after an upgrade
TEMPLATES.env.globals["asset_version"] = __version__
TEMPLATES.env.filters["age"] = format_age
TIER_TITLES = {"cheap": "Economy", "standard": "Standard", "strong": "Strong"}
LIMIT_STALE_AFTER_S = 6 * 3600


def _countdown(moment: str, now: datetime) -> str:
    seconds = (datetime.fromisoformat(moment) - now).total_seconds()
    return "now" if seconds <= 0 else f"in {format_age(seconds)}"


TEMPLATES.env.filters["countdown"] = _countdown


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

    def calibration_page(
        request: Request,
        sid: str,
        *,
        error: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        return render_page(
            request,
            "calibrate.html",
            {
                "title": "Manual calibration",
                "pools": deps.config.pools,
                "error": error,
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

    def jobs_page(
        request: Request,
        sid: str,
        jobs: list | None,
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
            "jobs": jobs or [],
            "tasks_disabled": jobs is None,
            "classifier": classifier,
        }
        template = "_jobs.html" if fragment else "jobs.html"
        return render_page(request, template, context, sid=sid, nav=None if fragment else "jobs")

    def job_list() -> list | None:
        if deps.dispatcher is None:
            return None
        found = deps.dispatcher.jobs.list(None, api.JOBS_LIST_MAX)
        return sorted(found, key=lambda job: job.status != "awaiting_approval")

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

    def limits_context(snapshot: dict) -> dict:
        return {
            "pools": snapshot["pools"],
            "local": local,
            "now": deps.clock.now(),
            "stale_after_s": LIMIT_STALE_AFTER_S,
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
                "approvals": approval_items(),
            }
            | limits_context(snapshot)
            | advice_context(snapshot)
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
        return render_page(request, "_limits.html", limits_context(snapshot), sid=sid)

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
        return jobs_page(request, sid, job_list())

    @router.get("/fragments/jobs")
    def get_jobs_fragment(request: Request) -> Response:
        sid = session_id(request)
        if sid is None:
            return login_redirect()
        return jobs_page(request, sid, job_list(), fragment=True)

    def job_context(job: Job, attempts: list[Attempt]) -> dict:
        prompt_chars = job.prompt_chars if job.prompt_chars is not None else len(job.request.prompt)
        return {
            "title": f"Job {job.id}",
            "job": job,
            "prompt": job.request.prompt[:2000],
            "prompt_truncated": prompt_chars > 2000,
            "prompt_chars": prompt_chars,
            "attempts": attempts,
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
        target = _field(form, "target")
        if not target:
            return calibration_page(request, sid, error="choose a pool and window", status_code=422)
        reset_date, reset_time = _field(form, "resets_date"), _field(form, "resets_time")
        resets_in = _field(form, "resets_in") or None
        if reset_time and not reset_date:
            return calibration_page(request, sid, error="set the reset date", status_code=422)
        if reset_date and resets_in:
            return calibration_page(
                request,
                sid,
                error="set either the reset date or reset in, not both",
                status_code=422,
            )
        try:
            resets_at = parse_moment(f"{reset_date} {reset_time}", deps.tz) if reset_date else None
            pool_index, window_index = (int(part) for part in target.split(":"))
            if pool_index < 0 or window_index < 0:
                raise ValueError
            pool = deps.config.pools[pool_index]
            window = pool.windows[window_index]
            body = api.ObservationBody(
                pool=pool.id,
                window=window.id,
                percent=float(_field(form, "percent")),
                resets_at=resets_at,
                resets_in=resets_in,
            )
            api.calibrate(deps, body)
        except api.ApiError as exc:
            return calibration_page(request, sid, error=exc.message, status_code=exc.status)
        except (ValidationError, ValueError, IndexError):
            return calibration_page(
                request, sid, error="check the calibration field values", status_code=422
            )
        query = urlencode({"recorded_pool": pool.id, "recorded_window": window.id})
        return RedirectResponse(f"/?{query}", status_code=303)

    return router
