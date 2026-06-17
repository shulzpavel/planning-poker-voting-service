import os
from enum import Enum


class UserRole(Enum):
    PARTICIPANT = "participant"
    LEAD = "lead"
    ADMIN = "admin"


# Microservices configuration (REQUIRED)
JIRA_SERVICE_URL = os.getenv("JIRA_SERVICE_URL", "http://localhost:8001")
VOTING_SERVICE_URL = os.getenv("VOTING_SERVICE_URL", "http://localhost:8002")

# Postgres metrics storage
POSTGRES_DSN = os.getenv("POSTGRES_DSN", "")

# Redis configuration (for Voting Service)
REDIS_URL = os.getenv("REDIS_URL", "")

# Legacy Jira config (used by Jira Service internally)
JIRA_URL = os.getenv("JIRA_URL", "https://your-domain.atlassian.net")
JIRA_USERNAME = os.getenv("JIRA_USERNAME", "your-email@domain.com")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "YOUR_JIRA_API_TOKEN_HERE")
STORY_POINTS_FIELD = os.getenv("STORY_POINTS_FIELD", "customfield_10022")
JIRA_SP_DEV_FIELD = os.getenv("JIRA_SP_DEV_FIELD", "").strip()
JIRA_SP_TEST_FIELD = os.getenv("JIRA_SP_TEST_FIELD", "").strip()
JIRA_SP_FRONT_FIELD = os.getenv("JIRA_SP_FRONT_FIELD", "").strip()
JIRA_SP_BACK_FIELD = os.getenv("JIRA_SP_BACK_FIELD", "").strip()
JIRA_SP_QA_FIELD = os.getenv("JIRA_SP_QA_FIELD", "").strip()
JIRA_FRONT_ASSIGNEE_FIELD = os.getenv("JIRA_FRONT_ASSIGNEE_FIELD", os.getenv("JIRA_FRONT_FIELD", "")).strip()
JIRA_BACK_ASSIGNEE_FIELD = os.getenv("JIRA_BACK_ASSIGNEE_FIELD", os.getenv("JIRA_BACK_FIELD", "")).strip()
JIRA_QA_ASSIGNEE_FIELD = os.getenv(
    "JIRA_QA_ASSIGNEE_FIELD",
    os.getenv("JIRA_TESTER_FIELD", os.getenv("JIRA_TEST_ASSIGNEE_FIELD", "")),
).strip()
JIRA_PLAN_STATUS_FIELD = os.getenv("JIRA_PLAN_STATUS_FIELD", "customfield_13045").strip()
JIRA_PLAN_CHANGE_REASON_FIELD = os.getenv("JIRA_PLAN_CHANGE_REASON_FIELD", "customfield_13047").strip()
JIRA_DEV_STATUS_KEYWORDS = os.getenv(
    "JIRA_DEV_STATUS_KEYWORDS",
    "dev,development,in progress,разработ,в работе,к выполнению,ready for dev",
).strip()

# Web UI base URL (e.g. https://poker.example.com); leave empty to disable web links
WEB_UI_URL = os.getenv("WEB_UI_URL", "")

# GitLab API (scope role attribution)
GITLAB_BASE_URL = os.getenv("GITLAB_BASE_URL", os.getenv("GITLAB_URL", "")).strip().rstrip("/")
GITLAB_TOKEN = os.getenv("GITLAB_TOKEN", os.getenv("GITLAB_PRIVATE_TOKEN", "")).strip()
GITLAB_GROUP_ID = os.getenv("GITLAB_GROUP_ID", "").strip()
