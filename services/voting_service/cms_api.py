"""Backward-compatible shim — CMS routes live in ``services.voting_service.cms``."""

from services.voting_service.cms import cms_router
from services.voting_service.cms import *  # noqa: F403
