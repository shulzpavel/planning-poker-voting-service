"""Tests for scope board layout order helpers."""

from services.voting_service.cms_api import (
    DEFAULT_SCOPE_LAYOUT_ORDER,
    _normalize_scope_layout_order,
)


def test_normalize_scope_layout_order_filters_unknown_and_appends_missing() -> None:
    result = _normalize_scope_layout_order(["report", "unknown", "topItems", "report"])
    assert result[:2] == ["report", "topItems"]
    assert "unknown" not in result
    assert result == _normalize_scope_layout_order(result)
    assert set(result) == set(DEFAULT_SCOPE_LAYOUT_ORDER)


def test_normalize_scope_layout_order_preserves_known_order() -> None:
    custom = list(reversed(DEFAULT_SCOPE_LAYOUT_ORDER))
    assert _normalize_scope_layout_order(custom) == custom


def test_normalize_scope_layout_order_includes_flow_pace() -> None:
    assert "flowPace" in DEFAULT_SCOPE_LAYOUT_ORDER
    assert "flowPace" in _normalize_scope_layout_order(["topItems"])
