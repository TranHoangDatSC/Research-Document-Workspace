"""Data access: SQL (PostgreSQL) and MongoDB queries, one connection per call.
No HTTP or business rules here; services/ translate errors into HTTP codes."""


def escape_like(text):
    """Makes %, _ and \\ literal inside an ILIKE ... ESCAPE '\\' pattern."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
