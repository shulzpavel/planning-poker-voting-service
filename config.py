import os
from enum import Enum


class UserRole(Enum):
    PARTICIPANT = "participant"
    LEAD = "lead"
    ADMIN = "admin"


# Microservices configuration
JIRA_SERVICE_URL = os.getenv("JIRA_SERVICE_URL", "http://localhost:8001")
VOTING_SERVICE_URL = os.getenv("VOTING_SERVICE_URL", "http://localhost:8002")

# Postgres / Redis (voting-service)
POSTGRES_DSN = os.getenv("POSTGRES_DSN", "")
REDIS_URL = os.getenv("REDIS_URL", "")

# Web UI base URL (e.g. https://poker.example.com); leave empty to disable web links
WEB_UI_URL = os.getenv("WEB_UI_URL", "")
