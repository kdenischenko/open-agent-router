"""Delegation skill files for supported orchestrators."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = (
    ROOT / "integrations/claude-code/agent-router-delegate/SKILL.md",
    ROOT / "integrations/kimi-code/agent-router-delegate/SKILL.md",
    ROOT / "integrations/cline/agent-router-delegate/SKILL.md",
    ROOT / "integrations/opencode/agent-router-delegate/SKILL.md",
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
