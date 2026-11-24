"""Server-rendered spend history charts on the Limits page and its fragment."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest

from agent_router.spend import SpendEntry
from test_web_limits import PATHS, element_block, rig, web

__all__ = ["rig", "web"]


def add_spend(
    rig,
    ref: str,
    *,
    pool: str = "opencode-go",
    hours_ago: float = 1,
    cost: float | None = 1.25,
    tokens: int = 20,
    estimated: bool = False,
) -> None:
    assert rig.jobs.add_spend(
        SpendEntry(
            pool=pool,
            at=rig.clock.now() - timedelta(hours=hours_ago),
            source="job",
            route=f"{pool}/model",
            tokens_in=tokens,
            tokens_out=tokens // 2,
            cost_usd=cost,
            estimated=estimated,
            prices_version="v1" if estimated else None,
            ref_id=ref,
            ref_n=1,
        )
    )


@pytest.mark.parametrize("path", PATHS)
def test_empty_spend_journal_shows_text_instead_of_charts(web, path) -> None:
    response = web.get(path)

    assert response.status_code == 200
    assert "no spend recorded" in response.text
    assert 'class="spend-chart"' not in response.text


@pytest.mark.parametrize("path", PATHS)
def test_pool_spend_renders_svg_and_matching_accessible_tables(web, rig, path) -> None:
    add_spend(rig, "first", hours_ago=2, cost=2.75, estimated=True)
    add_spend(rig, "second", hours_ago=1, cost=1.25)

    response = web.get(path)
    text = response.text

    assert response.status_code == 200
    day = element_block(text, r'<section\b[^>]*\bid="spend-day-opencode-go"[^>]*>')
    week = element_block(text, r'<section\b[^>]*\bid="spend-week-opencode-go"[^>]*>')
    for chart, total in ((day, "4.00 USD"), (week, "4.00 USD")):
        svg = re.search(r"<svg\b[^>]*>", chart)
        assert svg is not None
        assert 'role="img"' in svg.group(0)
        assert 'aria-label="' in svg.group(0)
        assert 'viewBox="' in svg.group(0)
        assert total in chart
        assert "estimated" in chart
        assert "<table>" in chart
        assert chart.index("</svg>") < chart.index("<table>")
    assert "2.75 USD" in day
    assert "1.25 USD" in day
    assert "2.75 USD" not in week
    assert "1.25 USD" not in week
    assert len(re.findall(r"<rect\b", day)) == 2
    assert len(re.findall(r"<rect\b", week)) == 1
    assert "3 tokens" not in day
    assert "<script" not in text if path == "/fragments/limits" else text.count("<script") == 1


@pytest.mark.parametrize("path", PATHS)
def test_each_pool_with_spend_gets_its_own_charts_and_unit(web, rig, path) -> None:
    add_spend(rig, "usd", cost=1.5)
    add_spend(rig, "tokens", pool="claude-pro", cost=None, tokens=20)

    response = web.get(path)

    assert response.status_code == 200
    for pool, value in (("opencode-go", "1.50 USD"), ("claude-pro", "30 tokens")):
        day = element_block(response.text, rf'<section\b[^>]*\bid="spend-day-{pool}"[^>]*>')
        week = element_block(response.text, rf'<section\b[^>]*\bid="spend-week-{pool}"[^>]*>')
        assert value in day
        assert value in week
    empty = element_block(response.text, r'<section\b[^>]*\bid="spend-history-kimi-code"[^>]*>')
    assert "no spend recorded" in empty
