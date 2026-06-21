"""Shared SQL fragments for PostgresCmsStore mixins."""

from __future__ import annotations


class SqlConstantsMixin:
    """SQL SELECT fragments used across CMS store mixins."""

    _PLAN_SELECT = """
        SELECT p.id, p.name, p.payload, p.created_by, p.created_at, p.updated_at,
               p.team_id, t.name AS team_name, t.slug AS team_slug,
               a.username AS created_by_username,
               a.display_name AS created_by_display_name
    """

    _SCOPE_BOARD_SELECT = """
        SELECT b.id, b.name, b.month, b.capacity_sp, b.capacity_sp_dev, b.capacity_sp_test,
               b.workload_mode, b.plan_jql, b.unplan_jql,
               b.todo_jql, b.test_jql, b.report_type, b.previous_release_jql,
               b.next_release_jql, b.custom_release_name, b.custom_release_jql,
               b.release_queries,
               b.release_comment, b.previous_release_comment, b.next_release_comment, b.custom_release_comment,
               b.plan_epic_key,
               b.scope_sections, b.snapshot,
               b.ai_summary, b.ai_summary_history, b.layout_order, b.flow_pace_chart_order,
               b.created_by, b.created_at, b.updated_at, b.team_id,
               t.name AS team_name, t.slug AS team_slug,
               a.username AS created_by_username,
               a.display_name AS created_by_display_name
        FROM cms_scope_boards b
        LEFT JOIN cms_teams t ON t.id = b.team_id
        LEFT JOIN cms_admin_accounts a ON a.id = b.created_by
    """

    _SCOPE_BOARD_LIST_SELECT = """
        SELECT b.id, b.name, b.month, b.capacity_sp, b.capacity_sp_dev, b.capacity_sp_test,
               b.workload_mode, b.plan_jql, b.unplan_jql,
               b.todo_jql, b.test_jql, b.report_type, b.previous_release_jql,
               b.next_release_jql, b.custom_release_name, b.custom_release_jql,
               b.release_queries,
               b.release_comment, b.previous_release_comment, b.next_release_comment, b.custom_release_comment,
               b.plan_epic_key,
               NULL::jsonb AS scope_sections,
               CASE
                 WHEN b.snapshot IS NULL THEN NULL
                 ELSE jsonb_build_object('metrics', b.snapshot->'metrics')
               END AS snapshot,
               NULL::jsonb AS ai_summary,
               '[]'::jsonb AS ai_summary_history,
               b.layout_order, b.flow_pace_chart_order,
               b.created_by, b.created_at, b.updated_at, b.team_id,
               t.name AS team_name, t.slug AS team_slug,
               a.username AS created_by_username,
               a.display_name AS created_by_display_name
        FROM cms_scope_boards b
        LEFT JOIN cms_teams t ON t.id = b.team_id
        LEFT JOIN cms_admin_accounts a ON a.id = b.created_by
    """

    _RETRO_SELECT = """
        SELECT r.id, r.title, r.status, r.config, r.snapshot, r.ai_summary,
               r.created_by, r.created_at, r.updated_at, r.team_id,
               t.name AS team_name, t.slug AS team_slug,
               a.username AS created_by_username,
               a.display_name AS created_by_display_name
        FROM cms_retros r
        LEFT JOIN cms_teams t ON t.id = r.team_id
        LEFT JOIN cms_admin_accounts a ON a.id = r.created_by
    """

    _SESSION_SCOPE = """
        ($1::boolean OR s.team_id IS NULL OR s.team_id = ANY($2::bigint[]))
        AND ($3::bigint IS NULL OR s.team_id IS NOT DISTINCT FROM $3)
    """

    _SESSION_LIST_SELECT = """
        SELECT s.id, s.session_key, s.chat_id, s.topic_id, s.title, s.current_task_index,
               s.participants_count, s.tasks_queue_count, s.history_count,
               s.last_batch_count, s.total_tasks, s.total_votes, s.batch_completed,
               s.is_active, s.current_batch_id, s.current_batch_started_at,
               s.current_task_id, s.tasks_version, s.updated_at, s.team_id
    """

    _SESSION_DETAIL_SELECT = """
        SELECT s.id, s.session_key, s.chat_id, s.topic_id, s.title, s.current_task_index,
               s.participants_count, s.tasks_queue_count, s.history_count,
               s.last_batch_count, s.total_tasks, s.total_votes, s.batch_completed,
               s.is_active, s.current_batch_id, s.current_batch_started_at,
               s.current_task_id, s.tasks_version, s.updated_at, s.team_id,
               t.name AS team_name, t.slug AS team_slug
    """
