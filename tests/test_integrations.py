"""Delegation skill files for supported orchestrators."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = (
    ROOT / "integrations/claude-code/agent-router-delegate/SKILL.md",
    ROOT / "integrations/kimi-code/agent-router-delegate/SKILL.md",
    ROOT / "integrations/cline/agent-router-delegate/SKILL.md",
    ROOT / "integrations/opencode/agent-router-delegate/SKILL.md",
    ROOT / "integrations/codex/agent-router-delegate/SKILL.md",
)


@pytest.mark.parametrize("path", SKILLS, ids=lambda path: path.parts[-3])
def test_orchestrator_skill_exists(path: Path) -> None:
    assert path.is_file()


@pytest.mark.parametrize("path", SKILLS, ids=lambda path: path.parts[-3])
def test_orchestrator_skill_names_run_and_wait(path: Path) -> None:
    assert path.is_file()
    text = path.read_text(encoding="utf-8")

    assert "agent-router run" in text
    assert "agent-router wait" in text


@pytest.mark.parametrize("path", SKILLS, ids=lambda path: path.parts[-3])
def test_orchestrator_skill_documents_exit_codes(path: Path) -> None:
    assert path.is_file()
    text = path.read_text(encoding="utf-8")

    for code in ("0", "2", "3", "4", "5", "6"):
        assert f"`{code}`" in text


@pytest.mark.parametrize("path", SKILLS, ids=lambda path: path.parts[-3])
def test_orchestrator_skill_documents_rating_and_caller(path: Path) -> None:
    assert path.is_file()
    text = path.read_text(encoding="utf-8")

    assert "agent-router rate" in text
    assert "--caller" in text


@pytest.mark.parametrize("path", SKILLS, ids=lambda path: path.parts[-3])
def test_orchestrator_skill_leaves_approve_to_human(path: Path) -> None:
    assert path.is_file()
    text = path.read_text(encoding="utf-8")

    approve_lines = [line for line in text.splitlines() if "approve" in line]
    assert approve_lines
    assert all("human" in line.lower() for line in approve_lines)


@pytest.mark.parametrize("path", SKILLS, ids=lambda path: path.parts[-3])
def test_skill_hands_approval_to_human(path: Path) -> None:
    paragraphs = path.read_text(encoding="utf-8").split("\n\n")

    handover = [p for p in paragraphs if "job awaits approval" in p]
    assert handover, "the skill must explain the approval message"
    assert any("agent-router approve" in p and "human" in p.lower() for p in handover)


EXPECTED_TARGETS = [
    ("claude-code", "$HOME/.claude/skills"),
    ("kimi-code", "$HOME/.kimi-code/skills"),
    ("opencode", "$HOME/.config/opencode/skills"),
    ("cline", "$HOME/.cline/skills"),
    ("codex", "$HOME/.agents/skills"),
]


def _install_section() -> str:
    text = (ROOT / "docs/INSTALL.md").read_text(encoding="utf-8")
    section = text[text.index("## 9. Delegation skill") :]
    return section[: section.index("\n## ", 1)]


def test_install_powershell_block_targets_all_harnesses() -> None:
    section = _install_section()
    block = re.search(r"```powershell\n(.*?)```", section, flags=re.DOTALL)
    assert block, "section 9 must keep the PowerShell install block"

    pairs = re.findall(r'"\s*(\S+?)\s*"\s*=\s*"([^"]+)"', block[1])
    assert pairs == EXPECTED_TARGETS


def test_install_bash_block_targets_all_harnesses() -> None:
    section = _install_section()
    block = re.search(r"```bash\n(.*?)```", section, flags=re.DOTALL)
    assert block, "section 9 must keep the bash install block"

    pairs = re.findall(
        r"^(\S+)\s+(\$HOME/\S+|[A-Za-z]:\\Users\\\S+)$", block[1], flags=re.MULTILINE
    )
    assert pairs == EXPECTED_TARGETS
    assert "mkdir -p" in block[1]
    assert "read -r" in block[1]
    assert 'cp -R "integrations/$harness/agent-router-delegate" "$dir"' in block[1]


@pytest.mark.parametrize("path", SKILLS, ids=lambda path: path.parts[-3])
def test_orchestrator_skill_passes_the_job_kind(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    assert "--kind research" in text
    assert "--review-of" in text
