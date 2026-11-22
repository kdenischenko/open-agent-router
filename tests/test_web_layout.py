"""Dashboard layout: navigation highlighting, approval badge, CSS hygiene and flash messages."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from test_web_dashboard import authenticated_client, login
from test_web_jobs import WebRig

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "agent_router"
HEX_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b")

NAV_LINKS = ("/", "/jobs", "/calibrate")


@pytest.fixture
def rig(home, config, store, jobstore, clock, tokens) -> WebRig:
    return WebRig(home, config, store, jobstore, clock, tokens)


@pytest.fixture
def web(rig) -> TestClient:
    with TestClient(rig.app, base_url="http://127.0.0.1:8787") as client:
        assert login(client, rig.tokens.admin).status_code == 303
        yield client


def nav_menu(text: str) -> str:
    found = re.search(r"<nav\b[^>]*>(.*?)</nav>", text, re.S)
    assert found is not None
    return found.group(1)


def current_nav_href(nav: str) -> str:
    links = re.findall(r'<a\b[^>]*aria-current="page"[^>]*>', nav)
    assert len(links) == 1
    href = re.search(r'href="([^"]+)"', links[0])
    assert href is not None
    return href.group(1)


def assert_nav_link(nav: str, href: str) -> None:
    link = re.search(
        r'<a\b(?=[^>]*href="' + re.escape(href) + r'")(?=[^>]*class="[^"]*\bnav-link\b)[^>]*>',
        nav,
    )
    assert link is not None, f"no nav-link to {href} in {nav}"


def approval_badge(text: str) -> re.Match[str] | None:
    return re.search(
        r'<a\b(?=[^>]*class="approval-badge")(?=[^>]*href="/jobs")[^>]*>\s*(.*?)\s*</a>',
        text,
        re.S,
    )


# --- nav: exactly one active link, on the matching item -----------------------------------


@pytest.mark.parametrize(
    ("path", "active"),
    [
        ("/", "/"),
        ("/jobs", "/jobs"),
        ("/calibrate", "/calibrate"),
    ],
)
def test_nav_marks_exactly_one_active_link(web, path, active) -> None:
    nav = nav_menu(web.get(path).text)

    for href in NAV_LINKS:
        assert_nav_link(nav, href)
    assert current_nav_href(nav) == active


def test_job_page_marks_jobs_nav_active(web, rig) -> None:
    job = rig.add_job("queued")

    nav = nav_menu(web.get(f"/jobs/{job.id}").text)

    assert current_nav_href(nav) == "/jobs"


def test_login_page_has_no_nav(client) -> None:
    assert "<nav" not in client.get("/login").text


# --- approval badge -------------------------------------------------------------------------


def test_badge_absent_when_nothing_awaits_approval(web) -> None:
    assert approval_badge(web.get("/").text) is None


def test_badge_shows_visible_count_and_index_line_stays(web, rig) -> None:
    rig.add_job("awaiting_approval")
    rig.add_job("awaiting_approval")

    text = web.get("/").text

    badge = approval_badge(text)
    assert badge is not None
    assert badge.group(1) == "2 awaiting approval"
    assert "Awaiting approval: 2" in text


def test_badge_absent_when_dispatcher_is_off(client, tokens) -> None:
    authenticated_client(client, tokens)

    assert approval_badge(client.get("/").text) is None


def test_badge_caps_display_at_99_plus(web, rig, monkeypatch) -> None:
    original = rig.dispatcher.jobs.list
    calls: list[tuple[object, int]] = []

    def fake_list(status=None, limit=50):
        calls.append((status, limit))
        if status == "awaiting_approval":
            return [SimpleNamespace(status="awaiting_approval") for _ in range(100)]
        return original(status, limit)

    monkeypatch.setattr(rig.dispatcher.jobs, "list", fake_list)

    text = web.get("/").text

    assert ("awaiting_approval", 100) in calls
    badge = approval_badge(text)
    assert badge is not None
    assert badge.group(1) == "99+ awaiting approval"
    assert "100 awaiting approval" not in text


# --- css and template hygiene ----------------------------------------------------------------


def template_paths() -> list[Path]:
    return sorted((PACKAGE / "templates").glob("*.html"))


@pytest.mark.parametrize("path", template_paths(), ids=lambda path: path.name)
def test_templates_have_no_hex_colours_or_inline_styles(path) -> None:
    text = path.read_text(encoding="utf-8")
    text = re.sub(r'href="#[^"]*"', "", text)

    found = HEX_COLOUR.search(text)
    assert found is None, f"hex colour {found.group(0)} in {path.name}"
    assert "style=" not in text


def balanced_block(text: str, open_brace: int) -> str:
    depth = 0
    for i in range(open_brace, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace : i + 1]
    raise AssertionError("unbalanced braces in app.css")


def test_app_css_keeps_hex_only_in_token_blocks() -> None:
    css = (PACKAGE / "static" / "app.css").read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)

    media = re.search(r"@media\s*\([^)]*prefers-color-scheme[^)]*\)\s*\{", css)
    assert media is not None
    dark_root = re.search(r":root\s*\{", css[media.end() :])
    assert dark_root is not None
    dark_block = balanced_block(css, media.end() + dark_root.end() - 1)

    light_root = re.search(r":root\s*\{", css)
    assert light_root is not None
    light_block = balanced_block(css, light_root.end() - 1)

    for token in ("--color-bg:", "--color-accent:", "--color-danger:"):
        assert token in light_block
        assert token in dark_block
    assert HEX_COLOUR.search(light_block)
    assert HEX_COLOUR.search(dark_block)

    rest = css.replace(light_block, "", 1).replace(dark_block, "", 1)
    found = HEX_COLOUR.search(rest)
    assert found is None, f"hex colour {found.group(0)} outside the token blocks"


def test_app_css_defines_layout_classes() -> None:
    css = (PACKAGE / "static" / "app.css").read_text(encoding="utf-8")

    for klass in (".nav-link", ".approval-badge", ".flash--danger", ".flash--ok", ".empty"):
        assert klass in css


# --- flash markup -----------------------------------------------------------------------------


def test_login_error_uses_danger_flash(client) -> None:
    response = login(client, "wrong token")

    assert response.status_code == 401
    text = response.text
    assert '<p class="flash flash--danger" role="alert">' in text
    assert '<span class="flash__prefix">Error:</span>' in text
    assert "admin token not accepted" in text
    assert "flash--ok" not in text


def test_jobs_page_marks_empty_list(web) -> None:
    text = web.get("/jobs").text

    assert re.search(r'<p class="empty"[^>]*>\s*No jobs yet', text)


def test_static_urls_carry_the_package_version(web: TestClient) -> None:
    from agent_router import __version__

    text = web.get("/").text
    assert f'href="/static/app.css?v={__version__}"' in text
    assert f'src="/static/app.js?v={__version__}"' in text


# --- colour contrast of the theme tokens ----------------------------------------------------


def theme_tokens() -> dict[str, dict[str, str]]:
    css = (PACKAGE / "static" / "app.css").read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    light_root = re.search(r":root\s*\{", css)
    media = re.search(r"@media\s*\([^)]*prefers-color-scheme[^)]*\)\s*\{", css)
    assert light_root is not None and media is not None
    dark_root = re.search(r":root\s*\{", css[media.end() :])
    assert dark_root is not None
    blocks = {
        "light": balanced_block(css, light_root.end() - 1),
        "dark": balanced_block(css, media.end() + dark_root.end() - 1),
    }
    pattern = re.compile(r"(--color-[\w-]+)\s*:\s*(#[0-9a-fA-F]{6})\b")
    return {theme: dict(pattern.findall(block)) for theme, block in blocks.items()}


def contrast(first: str, second: str) -> float:
    def luminance(colour: str) -> float:
        channels = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, b = (c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


TEXT_PAIRS = [
    (fg, bg)
    for fg in ("--color-text", "--color-muted", "--color-accent")
    for bg in ("--color-bg", "--color-surface", "--color-surface-2")
] + [
    ("--color-accent-contrast", "--color-accent"),
    ("--color-ok", "--color-ok-bg"),
    ("--color-warn", "--color-warn-bg"),
    ("--color-danger", "--color-danger-bg"),
    ("--color-info", "--color-info-bg"),
]


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize(("fg", "bg"), TEXT_PAIRS)
def test_text_tokens_meet_aa_contrast(theme, fg, bg) -> None:
    tokens = theme_tokens()[theme]
    ratio = contrast(tokens[fg], tokens[bg])
    assert ratio >= 4.5, f"{theme}: {fg} on {bg} is {ratio:.2f}:1"


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("bg", ["--color-bg", "--color-surface"])
def test_control_border_meets_non_text_contrast(theme, bg) -> None:
    tokens = theme_tokens()[theme]
    ratio = contrast(tokens["--color-control-border"], tokens[bg])
    assert ratio >= 3.0, f"{theme}: control border on {bg} is {ratio:.2f}:1"


def test_form_controls_use_the_control_border() -> None:
    css = (PACKAGE / "static" / "app.css").read_text(encoding="utf-8")
    rules = re.findall(r"^input,\s*select,\s*textarea\s*\{([^}]*)\}", css, re.M)
    borders = [line.strip() for rule in rules for line in rule.splitlines() if "border:" in line]
    assert borders == ["border: 1px solid var(--color-control-border);"]


def test_badge_accessible_name_is_its_visible_label(web, rig) -> None:
    rig.add_job("awaiting_approval")

    text = web.get("/").text

    badge = re.search(r'<a\b[^>]*class="approval-badge"[^>]*>', text)
    assert badge is not None
    assert "aria-label" not in badge.group(0)
