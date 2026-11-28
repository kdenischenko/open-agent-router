"""Job page Files panel: live file list while the job runs, stored list afterwards,
per-file diff from the live tree or the stored attempt patches."""

from __future__ import annotations

import re
import shutil

import pytest
from fastapi.testclient import TestClient

from agent_router.jobs import Attempt
from agent_router.worktree import build_patch, head, tree_snapshot
from helpers import BASE_URL, break_index, git, make_git_repo
from test_web_jobs import WebRig


@pytest.fixture
def rig(home, config, store, jobstore, clock, tokens) -> WebRig:
    return WebRig(home, config, store, jobstore, clock, tokens)


@pytest.fixture
def web(rig) -> TestClient:
    with TestClient(rig.app, base_url=BASE_URL) as client:
        response = client.post("/login", data={"token": rig.tokens.admin})
        assert response.status_code == 200
        yield client


def running_job(rig: WebRig, repo, **fields):
    """A running job in `repo` whose attempt recorded the tree before any edit."""
    job = rig.add_job("running", repo_root=repo, cwd=repo, **fields)
    rig.jobs.add_attempt(
        Attempt(
            job_id=job.id,
            n=1,
            route="kimi-cli/kimi-for-coding",
            pool="kimi-code",
            started_at=rig.clock.now(),
            head_before=head(repo),
            tree_before=tree_snapshot(repo),
        )
    )
    return job


def files_fragment(web, job, path: str | None = None) -> str:
    query = {} if path is None else {"path": path}
    response = web.get(f"/fragments/jobs/{job.id}/files", params=query)
    assert response.status_code == 200
    return response.text


def diff_fragment(web, job, path: str | None) -> str:
    query = {} if path is None else {"path": path}
    response = web.get(f"/fragments/jobs/{job.id}/diff", params=query)
    assert response.status_code == 200
    return response.text


# --- the file list ---------------------------------------------------------------------------


def test_running_job_lists_files_measured_from_the_working_directory(web, rig, tmp_path) -> None:
    repo = make_git_repo(tmp_path / "репозиторий")  # non-ascii on purpose
    job = running_job(rig, repo)
    (repo / "README.md").write_text("changed by the agent\n", encoding="utf-8")
    name = "новый файл.txt"  # non-ascii on purpose
    (repo / name).write_text("создан агентом\n", encoding="utf-8")  # non-ascii on purpose

    text = files_fragment(web, job)

    assert 'id="job-files"' in text
    assert 'data-terminal="false"' in text
    assert f'href="/jobs/{job.id}?path=README.md#job-files"' in text
    for path in ("README.md", name):
        assert re.search(r'<a\b[^>]*data-file-path="' + re.escape(path) + r'"[^>]*>', text)


def test_files_fragment_follows_edits_between_requests(web, rig, tmp_path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    job = running_job(rig, repo)

    first = files_fragment(web, job)
    assert "No files changed." in first

    (repo / "later.txt").write_text("added between requests\n", encoding="utf-8")
    second = files_fragment(web, job)
    assert "later.txt" in second


def test_files_fragment_lists_committed_files_when_head_moved(web, rig, tmp_path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    job = running_job(rig, repo)
    (repo / "committed.txt").write_text("committed mid-run\n", encoding="utf-8")
    git(repo, "add", "committed.txt")
    git(repo, "commit", "-q", "-m", "mid-run")

    text = files_fragment(web, job)

    assert "committed.txt" in text


def test_files_fragment_merges_files_of_earlier_attempts(web, rig, tmp_path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    job = running_job(rig, repo, changed_files=("раньше.txt",))  # non-ascii on purpose
    (repo / "сейчас.txt").write_text("now\n", encoding="utf-8")  # non-ascii on purpose

    text = files_fragment(web, job)

    assert "раньше.txt" in text  # non-ascii on purpose
    assert "сейчас.txt" in text  # non-ascii on purpose


def test_finished_job_shows_the_stored_files_not_the_current_tree(web, rig, tmp_path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    # the tree is clean: a live measurement would answer "no files changed"
    job = rig.add_job("completed", repo_root=repo, cwd=repo, changed_files=("README.md",))

    text = files_fragment(web, job)

    assert 'data-terminal="true"' in text
    assert "README.md" in text
    assert "No files changed." not in text


def test_running_job_git_failure_falls_back_to_the_stored_message(web, rig, tmp_path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    job = running_job(rig, repo, changed_files=None)
    break_index(repo)

    text = files_fragment(web, job)

    assert "Not measured yet." in text
    assert "outside git" not in text


def test_running_job_with_directory_gone_keeps_the_stored_list(web, rig, tmp_path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    job = running_job(rig, repo, changed_files=("gone.txt",))
    shutil.rmtree(repo)

    text = files_fragment(web, job)

    assert "gone.txt" in text
    assert "Not measured yet." not in text


def test_files_fragment_outside_git_says_not_tracked(web, rig) -> None:
    job = rig.add_job("running")  # the gateway home is not a repository

    text = files_fragment(web, job)

    assert "Not tracked: the directory is outside git." in text


def test_files_fragment_needs_session(client) -> None:
    response = client.get("/fragments/jobs/j_web_1/files", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_files_fragment_missing_job_is_404(web) -> None:
    response = web.get("/fragments/jobs/j_missing/files")

    assert response.status_code == 404
    assert "job not found" in response.text


@pytest.mark.parametrize(
    "path", ["/fragments/jobs/j_disabled/files", "/fragments/jobs/j_disabled/diff"]
)
def test_file_fragments_explain_when_tasks_are_disabled(client, tokens, path) -> None:
    response = client.post("/login", data={"token": tokens.admin})
    assert response.status_code == 200

    response = client.get(path)

    assert response.status_code == 200
    assert "Jobs are disabled" in response.text


# --- the per-file diff ------------------------------------------------------------------------


def test_diff_fragment_running_job_shows_the_live_file_diff(web, rig, tmp_path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    job = running_job(rig, repo)
    (repo / "README.md").write_text("changed content\n", encoding="utf-8")
    (repo / "другое.txt").write_text("other\n", encoding="utf-8")  # non-ascii on purpose

    text = diff_fragment(web, job, "README.md")

    assert 'id="job-file-diff"' in text
    assert "-test repo" in text
    assert re.search(r'<span class="diff-line diff-line--add">\+changed content\n</span>', text)
    assert "другое.txt" not in text  # non-ascii on purpose: the other file's diff stays out


def test_diff_fragment_finished_job_extracts_one_file_from_the_stored_patch(web, rig) -> None:
    job = rig.add_job("completed", changed_files=("first.txt", "second.txt"))
    patch = (
        "diff --git a/first.txt b/first.txt\n--- a/first.txt\n+++ b/first.txt\n"
        "@@ -1 +1 @@\n-old line\n+new line\n"
        "diff --git a/second.txt b/second.txt\n--- a/second.txt\n+++ b/second.txt\n"
        "@@ -1 +1 @@\n-old two\n+new two\n"
    )
    rig.add_attempt(job, patch=patch)

    text = diff_fragment(web, job, "first.txt")

    assert "+new line" in text
    assert "from attempt #1" in text
    assert "second.txt" not in text
    assert 'data-copy-target="job-file-diff-text"' in text


def test_diff_fragment_finished_job_prefers_the_complete_stored_patch(web, rig, tmp_path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    (repo / "README.md").write_text("version one\n", encoding="utf-8")
    stored, _truncated = build_patch(repo, ["README.md"])
    (repo / "README.md").write_text("version two\n", encoding="utf-8")  # edited after the job

    job = rig.add_job("completed", repo_root=repo, cwd=repo, changed_files=("README.md",))
    rig.add_attempt(job, patch=stored)

    text = diff_fragment(web, job, "README.md")

    assert "+version one" in text
    assert "from attempt #1" in text
    assert "+version two" not in text


def test_diff_fragment_truncated_stored_tail_falls_back_to_the_live_tree(
    web, rig, tmp_path
) -> None:
    repo = make_git_repo(tmp_path / "repo")
    (repo / "README.md").write_text("version one\n", encoding="utf-8")
    stored, _truncated = build_patch(repo, ["README.md"])
    (repo / "README.md").write_text("version two\n", encoding="utf-8")

    job = rig.add_job("completed", repo_root=repo, cwd=repo, changed_files=("README.md",))
    rig.add_attempt(job, patch=stored, patch_truncated=True)

    text = diff_fragment(web, job, "README.md")

    assert "+version two" in text
    assert "+version one" not in text
    assert "from attempt" not in text


def test_diff_fragment_finished_job_without_a_patch_diffs_the_live_tree(web, rig, tmp_path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    job = rig.add_job("completed", repo_root=repo, cwd=repo, changed_files=("README.md",))
    (repo / "README.md").write_text("after the job\n", encoding="utf-8")

    text = diff_fragment(web, job, "README.md")

    assert "+after the job" in text
    assert "from attempt" not in text


def test_diff_fragment_truncates_one_file_at_the_cap(web, rig, tmp_path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    job = running_job(rig, repo)
    big = "строка паттерна\n" * 40_000  # non-ascii on purpose
    (repo / "большой.txt").write_text(big, encoding="utf-8")  # non-ascii on purpose

    text = diff_fragment(web, job, "большой.txt")  # non-ascii on purpose

    assert "diff truncated at 256 KiB" in text


def test_diff_fragment_unknown_path_says_no_diff(web, rig, tmp_path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    job = running_job(rig, repo)

    text = diff_fragment(web, job, "missing.txt")

    assert "no diff for this file" in text
    assert 'id="job-file-diff"' in text


def test_diff_fragment_when_git_cannot_run_says_not_measured(web, rig, tmp_path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    job = running_job(rig, repo)
    break_index(repo)

    text = diff_fragment(web, job, "README.md")

    assert "could not measure the diff for this file" in text


def test_diff_fragment_without_a_path_renders_an_empty_area(web, rig) -> None:
    job = rig.add_job("completed", changed_files=("a.txt",))

    text = diff_fragment(web, job, None)

    assert '<div id="job-file-diff" hidden></div>' in text


def test_diff_fragment_needs_session(client) -> None:
    response = client.get("/fragments/jobs/j_web_1/diff", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# --- the full page ----------------------------------------------------------------------------


def test_job_page_renders_the_files_panel_and_the_selected_diff(web, rig, tmp_path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    job = running_job(rig, repo)
    (repo / "README.md").write_text("edited\n", encoding="utf-8")

    text = web.get(f"/jobs/{job.id}").text
    assert 'id="job-files"' in text
    assert 'data-file-path="README.md"' in text
    assert '<div id="job-file-diff" hidden></div>' in text

    selected = web.get(f"/jobs/{job.id}", params={"path": "README.md"}).text
    assert 'data-path="README.md"' in selected
    assert re.search(r'<a\b[^>]*aria-current="true"[^>]*>.*?README.md', selected, re.S)
    assert re.search(r'<span class="diff-line diff-line--add">\+edited\n</span>', selected)


def test_selected_file_entry_links_back_with_the_path_query(web, rig, tmp_path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    job = running_job(rig, repo)
    (repo / "README.md").write_text("edited\n", encoding="utf-8")

    text = files_fragment(web, job, "README.md")

    assert f'href="/jobs/{job.id}?path=README.md#job-files"' in text
    assert re.search(r'<a\b[^>]*aria-current="true"[^>]*>.*?README.md', text, re.S)
    assert 'data-path="README.md"' in text


# --- the page script --------------------------------------------------------------------------


def test_app_js_polls_the_files_fragment_while_the_job_runs(client) -> None:
    script = client.get("/static/app.js").text
    poll = script[script.index("function setupFilesRefresh") :]
    poll = poll[: poll.index("\n}\n") + 3]

    assert "setInterval(" not in poll
    assert "setTimeout(" in poll
    assert "await refresh(" in poll
    assert "'/fragments/jobs/' + panel.dataset.jobId + '/files'" in poll
    assert "dataset.terminal" in poll
    assert "dataset.path" in poll


def test_app_js_loads_the_file_diff_on_click(client) -> None:
    script = client.get("/static/app.js").text

    assert "data-file-path" in script
    assert "'/diff?path='" in script
    assert "'job-file-diff'" in script


def test_app_js_manual_sweep_includes_the_files_panel(client) -> None:
    script = client.get("/static/app.js").text
    sweep = script[script.index("one manual sweep") :]
    sweep = sweep[: sweep.index("return;")]

    assert "'job-files'" in sweep
    assert "'/files'" in sweep
