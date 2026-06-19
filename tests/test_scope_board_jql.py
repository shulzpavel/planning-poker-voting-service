from app.domain.scope_board import jql_has_status_filter, pause_supplement_jql


def test_jql_has_status_filter_detects_status_clauses():
    assert jql_has_status_filter('project = FLEX AND status = "In Progress"') is True
    assert jql_has_status_filter("parent = FLEX-2861 AND status in (Done, Closed)") is True
    assert jql_has_status_filter("parent = FLEX-2861") is False
    assert jql_has_status_filter("assignee = currentUser()") is False


def test_pause_supplement_wraps_base_jql():
    assert "Пауза" in pause_supplement_jql("parent = FLEX-2861")
