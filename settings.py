"""Shared local application defaults."""
DEFAULT_DATABASE = "scroopyluu"


def resolve_database_name(value=None):
    if value is None:
        return DEFAULT_DATABASE
    if not isinstance(value, str):
        raise ValueError("db must be a string")
    return value.strip() or DEFAULT_DATABASE
