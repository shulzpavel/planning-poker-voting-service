"""Application-wide constants."""

MAX_MESSAGE_LENGTH = 4000
HEALTH_CHECK_INTERVAL = 300

VOTE_VALUES = ("0", "1", "2", "3", "5", "8", "13", "21", "?", "skip")
VALID_VOTE_VALUES: frozenset[str] = frozenset(VOTE_VALUES) | {"needs_review"}
