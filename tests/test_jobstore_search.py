"""Filtered, sorted and paginated job search."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from test_jobstore import T0, make_job


def test_search_filters_by_status(jobstore) -> None:
    jobstore.create(make_job("j_queued", status="queued"))
    jobstore.create(make_job("j_done", status="completed"))

    jobs, total = jobstore.search(status="completed")

    assert [job.id for job in jobs] == ["j_done"]
    assert total == 1


def test_search_filters_by_any_job_route(jobstore) -> None:
    direct = make_job("j_direct")
    direct.route = "kimi/k3"
    approval = make_job("j_approval")
    approval.approval_route = "kimi/k3"
    approved = make_job("j_approved")
    approved.approved_route = "kimi/k3"
    other = make_job("j_other")
    other.route = "opencode/big"
    request_only = make_job("j_request_only")
    request_only.request = replace(request_only.request, route="kimi/k3")
    for job in (direct, approval, approved, other, request_only):
        jobstore.create(job)

    jobs, total = jobstore.search(route="kimi/k3")

    assert {job.id for job in jobs} == {"j_direct", "j_approval", "j_approved"}
    assert total == 3


def test_search_filters_by_request_caller(jobstore) -> None:
    matching = make_job("j_matching")
    matching.request = replace(matching.request, caller="kimi-code")
    other = make_job("j_other")
    other.request = replace(other.request, caller="opencode")
    jobstore.create(matching)
    jobstore.create(other)

    jobs, total = jobstore.search(caller="kimi-code")

    assert [job.id for job in jobs] == ["j_matching"]
    assert total == 1


def test_search_q_matches_exact_job_id(jobstore) -> None:
    jobstore.create(make_job("j_exact"))
    jobstore.create(make_job("j_exact_suffix"))

    jobs, total = jobstore.search(q="j_exact")

    assert [job.id for job in jobs] == ["j_exact"]
    assert total == 1


def test_search_q_matches_prompt_case_insensitively(jobstore) -> None:
    matching = make_job("j_matching")
    matching.request = replace(matching.request, prompt="Fix the AlphaConnector")
    other = make_job("j_other")
    other.request = replace(other.request, prompt="Unrelated task")
    jobstore.create(matching)
    jobstore.create(other)

    jobs, total = jobstore.search(q="alphaconn")

    assert [job.id for job in jobs] == ["j_matching"]
    assert total == 1


def test_search_q_treats_percent_as_literal(jobstore) -> None:
    percent = make_job("j_percent")
    percent.request = replace(percent.request, prompt="100% complete")
    wildcard = make_job("j_wildcard")
    wildcard.request = replace(wildcard.request, prompt="1000 complete")
    jobstore.create(percent)
    jobstore.create(wildcard)

    jobs, total = jobstore.search(q="100%")

    assert [job.id for job in jobs] == ["j_percent"]
    assert total == 1


def test_search_sorts_newest_with_insertion_tiebreak(jobstore) -> None:
    jobstore.create(make_job("j_old", created_at=T0))
    jobstore.create(make_job("j_same_first", created_at=T0 + timedelta(minutes=1)))
    jobstore.create(make_job("j_same_second", created_at=T0 + timedelta(minutes=1)))

    jobs, total = jobstore.search()

    assert [job.id for job in jobs] == ["j_same_second", "j_same_first", "j_old"]
    assert total == 3


def test_search_sorts_oldest_with_insertion_tiebreak(jobstore) -> None:
    jobstore.create(make_job("j_same_first", created_at=T0 + timedelta(minutes=1)))
    jobstore.create(make_job("j_same_second", created_at=T0 + timedelta(minutes=1)))
    jobstore.create(make_job("j_new", created_at=T0 + timedelta(minutes=2)))

    jobs, total = jobstore.search(sort="oldest")

    assert [job.id for job in jobs] == ["j_same_first", "j_same_second", "j_new"]
    assert total == 3


def test_search_sorts_by_duration_with_unfinished_jobs_last(jobstore) -> None:
    for job_id, created, duration in (
        ("j_long_old", 0, 30),
        ("j_long_new", 1, 30),
        ("j_short", 2, 10),
    ):
        started = T0 + timedelta(minutes=created)
        job = make_job(job_id, status="completed", created_at=started)
        job.finished_at = started + timedelta(minutes=duration)
        jobstore.create(job)
    jobstore.create(make_job("j_unfinished_first", created_at=T0 + timedelta(minutes=3)))
    jobstore.create(make_job("j_unfinished_second", created_at=T0 + timedelta(minutes=3)))

    jobs, total = jobstore.search(sort="duration")

    assert [job.id for job in jobs] == [
        "j_long_new",
        "j_long_old",
        "j_short",
        "j_unfinished_second",
        "j_unfinished_first",
    ]
    assert total == 5


def test_search_unknown_sort_uses_newest(jobstore) -> None:
    jobstore.create(make_job("j_old", created_at=T0))
    jobstore.create(make_job("j_new", created_at=T0 + timedelta(minutes=1)))

    jobs, total = jobstore.search(sort="not-a-sort")

    assert [job.id for job in jobs] == ["j_new", "j_old"]
    assert total == 2


def test_search_pages_without_reducing_total(jobstore) -> None:
    for index in range(5):
        jobstore.create(make_job(f"j_{index}", created_at=T0 + timedelta(minutes=index)))

    jobs, total = jobstore.search(limit=2, offset=1)

    assert [job.id for job in jobs] == ["j_3", "j_2"]
    assert total == 5


def test_search_combines_all_filters(jobstore) -> None:
    matching = make_job("j_matching", status="completed")
    matching.approved_route = "kimi/k3"
    matching.request = replace(matching.request, caller="kimi-code", prompt="Fix Alpha")
    wrong_status = make_job("j_wrong_status", status="queued")
    wrong_status.approved_route = "kimi/k3"
    wrong_status.request = replace(wrong_status.request, caller="kimi-code", prompt="Fix Alpha")
    wrong_route = make_job("j_wrong_route", status="completed")
    wrong_route.approved_route = "opencode/big"
    wrong_route.request = replace(wrong_route.request, caller="kimi-code", prompt="Fix Alpha")
    wrong_caller = make_job("j_wrong_caller", status="completed")
    wrong_caller.approved_route = "kimi/k3"
    wrong_caller.request = replace(wrong_caller.request, caller="opencode", prompt="Fix Alpha")
    wrong_prompt = make_job("j_wrong_prompt", status="completed")
    wrong_prompt.approved_route = "kimi/k3"
    wrong_prompt.request = replace(wrong_prompt.request, caller="kimi-code", prompt="Unrelated")
    for job in (matching, wrong_status, wrong_route, wrong_caller, wrong_prompt):
        jobstore.create(job)

    jobs, total = jobstore.search(
        status="completed", route="kimi/k3", caller="kimi-code", q="alpha"
    )

    assert [job.id for job in jobs] == ["j_matching"]
    assert total == 1
