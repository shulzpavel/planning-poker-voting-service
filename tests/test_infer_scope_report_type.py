"""Tests for scope report type inference by team."""

from app.domain.scope_board import RELEASE_SCOPE_TEAM_SLUGS, infer_scope_report_type


def test_release_template_for_mobile_team_slugs() -> None:
    for slug in RELEASE_SCOPE_TEAM_SLUGS:
        assert infer_scope_report_type(slug) == "release"


def test_monthly_template_for_other_teams() -> None:
    assert infer_scope_report_type("igaming-rip", "iGaming RIP") == "monthly"
    assert infer_scope_report_type("igaming-test", "iGaming Test") == "monthly"
    assert infer_scope_report_type("studios", "Studios") == "monthly"
    assert infer_scope_report_type("alpha", "Alpha") == "monthly"
    assert infer_scope_report_type("ios", "iOS") == "monthly"
    assert infer_scope_report_type("android", "Android") == "monthly"
