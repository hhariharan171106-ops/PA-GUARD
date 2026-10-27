"""Shared validation for deadlines used by time-sensitive case agents."""
from datetime import datetime


class InvalidCaseDeadline(ValueError):
    """A case deadline is missing, ambiguous, or not an ISO-8601 timestamp."""


def parse_case_deadline(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise InvalidCaseDeadline(
            "The stored case deadline is missing. Ask a manager to correct it "
            "before running time-sensitive agents."
        )
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise InvalidCaseDeadline(
            "The stored case deadline is not a valid ISO-8601 timestamp. Ask a "
            "manager to correct it before running time-sensitive agents."
        ) from error
    if parsed.tzinfo is None:
        raise InvalidCaseDeadline(
            "The stored case deadline has no timezone. Ask a manager to correct "
            "it with an ISO-8601 timezone before running time-sensitive agents."
        )
    return parsed


def validate_case_deadline(value: object) -> str:
    """Return a canonical ISO-8601 timestamp, rejecting ambiguous input."""
    return parse_case_deadline(value).isoformat()
