"""Checks for local links, image accessibility, and README asset sizes."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
README_RU = ROOT / "README_RU.md"
READMES = (README, README_RU)
ASSETS = ROOT / "docs" / "assets"
MAX_ASSET_BYTES = 500 * 1024

MARKDOWN_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
HTML_IMAGE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
HTML_IMAGE_SRC = re.compile(r"<img\b[^>]*?\bsrc=[\"']([^\"']+)[\"']", re.IGNORECASE)
HTML_SOURCE_SRCSET = re.compile(r"<source\b[^>]*?\bsrcset=[\"']([^\"']+)[\"']", re.IGNORECASE)
HTML_ALT = re.compile(r"\balt=[\"']([^\"']*)[\"']", re.IGNORECASE)


def _readme(path: Path = README) -> str:
    return path.read_text(encoding="utf-8")


def _local_target(target: str) -> Path | None:
    target = target.strip().split(maxsplit=1)[0].strip("<>")
    if target.startswith("#"):
        return None
    target = target.split("#", maxsplit=1)[0]
    for prefix in (f"{REPO_URL}/blob/main/", f"{REPO_URL}/tree/main/", RAW_URL):
        if target.startswith(prefix):
            return ROOT / target.removeprefix(prefix)
    if target.startswith(("http://", "https://")):
        return None
    return ROOT / target


@pytest.mark.parametrize("readme", READMES, ids=("README.md", "README_RU.md"))
def test_readme_local_links(readme: Path) -> None:
    text = _readme(readme)
    targets = [match.group(1) for match in MARKDOWN_LINK.finditer(text)]
    targets += [match.group(2) for match in MARKDOWN_IMAGE.finditer(text)]
    targets += HTML_IMAGE_SRC.findall(text) + HTML_SOURCE_SRCSET.findall(text)

    missing = []
    for target in targets:
        path = _local_target(target)
        if path is not None and not path.exists():
            missing.append(target)
    assert missing == []


@pytest.mark.parametrize("readme", READMES, ids=("README.md", "README_RU.md"))
def test_readme_images_have_alt(readme: Path) -> None:
    text = _readme(readme)
    markdown_missing = [target for alt, target in MARKDOWN_IMAGE.findall(text) if not alt.strip()]
    html_missing = [
        image
        for image in HTML_IMAGE.findall(text)
        if not (match := HTML_ALT.search(image)) or not match.group(1).strip()
    ]
    assert markdown_missing == []
    assert html_missing == []


def test_readme_assets_are_small() -> None:
    required = {
        "architecture-dark.svg",
        "architecture-light.svg",
        "banner-dark.svg",
        "banner-light.svg",
        "demo.svg",
        "cycle.svg",
        "dashboard-light.png",
        "dashboard-dark.png",
        "jobs-light.png",
        "jobs-dark.png",
        "job-light.png",
        "job-dark.png",
    }
    missing = sorted(name for name in required if not (ASSETS / name).is_file())
    assert missing == []

    oversized = [
        f"{path.relative_to(ROOT)}: {path.stat().st_size} bytes"
        for path in ASSETS.rglob("*")
        if path.is_file() and path.stat().st_size > MAX_ASSET_BYTES
    ]
    assert oversized == []


def _section(text: str, heading: str) -> str:
    start = text.index(f"\n## {heading}\n")
    end = text.find("\n## ", start + 1)
    return text[start:] if end == -1 else text[start:end]


def _headings(text: str) -> list[str]:
    return re.findall(r"^## (.+)$", text, re.MULTILINE)


def test_readme_compares_with_api_routers() -> None:
    text = _readme()
    headings = _headings(text)
    comparison = next(h for h in headings if "different" in h.lower())
    assert headings.index(comparison) == headings.index("Why") + 1
    section = _section(text, comparison)
    for name in ("LiteLLM", "OpenRouter"):
        assert name in comparison or name in section
    lowered = section.lower()
    assert "api key" in lowered
    assert "official cli" in lowered
    assert "never sees" in lowered
    if "proxy" in lowered:
        assert "planned" in lowered
        assert f"({REPO_URL}/blob/main/ROADMAP.md)" in section


def test_readme_says_who_it_is_for() -> None:
    lowered = _section(_readme(), "Why").lower()
    assert "who it is for" in lowered or "it is for you if" in lowered
    assert "two or more" in lowered
    assert "orchestrator" in lowered


def test_readme_platforms_mark_linux_and_macos_experimental() -> None:
    text = _readme()
    lowered = _section(text, "Platforms").lower()
    assert "experimental" in lowered
    assert "linux" in lowered and "macos" in lowered
    assert "help wanted" in lowered
    badge = next(line for line in text.splitlines() if "badge/platform-" in line)
    assert "Linux" in badge and "macOS" in badge


CYCLE_COMMANDS = (
    "agent-router run",
    "--no-wait",
    "agent-router approve",
    "agent-router wait",
    "agent-router rate",
    "agent-router status",
)


def test_readme_shows_the_full_job_cycle() -> None:
    images = {target: alt for alt, target in MARKDOWN_IMAGE.findall(_readme())}
    cycle_image = f"{RAW_URL}docs/assets/cycle.svg"
    assert cycle_image in images
    assert "approve" in images[cycle_image].lower()

    svg = (ASSETS / "cycle.svg").read_text(encoding="utf-8").replace("&#160;", " ")
    positions = [svg.find(command) for command in CYCLE_COMMANDS]
    assert -1 not in positions, dict(zip(CYCLE_COMMANDS, positions, strict=True))
    assert positions == sorted(positions)  # the commands appear in the order of the cycle
    assert "status completed" in svg  # wait shows the finished job
    assert "rating recorded" in svg
    assert "@keyframes" in svg
    assert "prefers-reduced-motion" in svg


TROUBLESHOOTING = ROOT / "docs" / "TROUBLESHOOTING.md"


def test_readme_links_troubleshooting() -> None:
    assert f"({REPO_URL}/blob/main/docs/TROUBLESHOOTING.md)" in _readme()


def test_troubleshooting_covers_common_failures() -> None:
    text = TROUBLESHOOTING.read_text(encoding="utf-8")
    lowered = text.lower()
    for needle in (
        "gateway unavailable",
        "agent-router serve",
        "agent_router_url",
        "not found",
        "doctor --live",
        "signed in",
        "agent-router unblock",
        "awaiting_approval",
        "agent-router approve",
        "approval_timeout_s",
    ):
        assert needle in lowered, needle
    assert re.search(r"exit code `?5`?", lowered)


REPO_URL = "https://github.com/kdenischenko/open-agent-router"
RAW_URL = "https://raw.githubusercontent.com/kdenischenko/open-agent-router/main/"


def _all_targets(text: str) -> list[str]:
    targets = [match.group(1) for match in MARKDOWN_LINK.finditer(text)]
    targets += [match.group(2) for match in MARKDOWN_IMAGE.finditer(text)]
    return targets + HTML_IMAGE_SRC.findall(text) + HTML_SOURCE_SRCSET.findall(text)


@pytest.mark.parametrize("readme", READMES, ids=("README.md", "README_RU.md"))
def test_readme_urls_are_absolute_for_pypi(readme: Path) -> None:
    relative = [
        target
        for target in _all_targets(_readme(readme))
        if not target.strip("<>").startswith(("http://", "https://", "#"))
    ]
    assert relative == []


@pytest.mark.parametrize("readme", READMES, ids=("README.md", "README_RU.md"))
def test_readme_images_come_from_raw_github(readme: Path) -> None:
    text = _readme(readme)
    images = [match.group(2) for match in MARKDOWN_IMAGE.finditer(text)]
    images += HTML_IMAGE_SRC.findall(text) + HTML_SOURCE_SRCSET.findall(text)
    local = [image for image in images if "docs/assets/" in image]

    assert local
    assert all(image.startswith(RAW_URL + "docs/assets/") for image in local)


def test_readmes_link_to_each_other() -> None:
    assert f"[Русский]({REPO_URL}/blob/main/README_RU.md)" in _readme(README)
    assert f"[English]({REPO_URL}/blob/main/README.md)" in _readme(README_RU)


def test_local_target_maps_repository_urls() -> None:
    assert _local_target(f"{REPO_URL}/blob/main/docs/INSTALL.md#top") == ROOT / "docs/INSTALL.md"
    assert _local_target(f"{RAW_URL}docs/assets/demo.svg") == ROOT / "docs/assets/demo.svg"
    assert _local_target(f"{REPO_URL}/blob/main/docs/NOPE.md") == ROOT / "docs/NOPE.md"
    assert _local_target("https://docs.astral.sh/uv/") is None


def test_readme_does_not_promise_cheapest() -> None:
    assert "cheapest" not in _readme().lower()


def test_readme_says_alpha_before_quick_start() -> None:
    text = _readme()
    assert text.index("early alpha") < text.index("## Quick start")


def test_pyproject_lists_platforms_and_keywords() -> None:
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    for platform in ("POSIX :: Linux", "MacOS", "Microsoft :: Windows"):
        assert f"Operating System :: {platform}" in project["classifiers"]
    for keyword in ("mcp", "claude-code", "opencode", "kimi", "cline", "orchestration"):
        assert keyword in project["keywords"]


def test_quick_start_installs_the_pypi_package() -> None:
    quick_start = _section(_readme(), "Quick start")
    commands = quick_start.split("```bash\n", 1)[1].split("```", 1)[0].splitlines()

    assert commands[0] == "uv tool install open-agent-router"
    assert f"uv tool install git+{REPO_URL}" in quick_start


def test_quick_start_covers_first_run_surprises() -> None:
    quick_start = _section(_readme(), "Quick start")

    assert "uv tool update-shell" in quick_start
    assert "keeps running" in quick_start
    assert "restart" in quick_start
    assert "claude-cli/sonnet" in quick_start
    assert "cline/glm-5.3" in quick_start


def test_readme_links_repository_folders() -> None:
    text = _readme()

    assert f"]({REPO_URL}/tree/main/docs/examples)" in text
    assert f"]({REPO_URL}/tree/main/integrations)" in text
    assert "clone" in _section(text, "Use from your agent")


@pytest.mark.parametrize("readme", READMES, ids=("README.md", "README_RU.md"))
def test_readme_has_no_page_anchors(readme: Path) -> None:
    assert [target for target in _all_targets(_readme(readme)) if target.startswith("#")] == []
