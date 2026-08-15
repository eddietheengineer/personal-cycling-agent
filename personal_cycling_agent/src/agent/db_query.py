from src.config.constants import DEFAULT_SQL_QUERY_LIMIT

"""
Safe database query tool for the coach AI.

Allows the coach to query the cycling database for any data it needs
to answer questions about training history, patterns, and causes.
"""

from pathlib import Path

import re
import logging

logger = logging.getLogger(__name__)

# Tables the coach is allowed to read
ALLOWED_TABLES = {
    "activities",
    "activity_streams",
    "activity_metrics",
    "activity_routes",
    "wellness",
    "morning_checkin",
    "daily_readiness",
    "raw_activities",
    "raw_fit_sessions",
    "sync_state",
    "hr_calibration",
}

# Dangerous keywords that are never allowed
BLOCKED_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE",
    "ATTACH", "DETACH", "REPLACE", "PRAGMA", "VACUUM",
    "UNION", "LOAD_EXTENSION", "READFILE", "WRITEFILE",
}

# Dangerous SQLite functions that can leak data or execute arbitrary code
BLOCKED_FUNCTIONS = {
    "LOAD_EXTENSION", "READFILE", "WRITEFILE",
    "HEX", "CHAR", "QUOTE", "PRINTF",
    "CHANGES", "LAST_INSERT_ROWID", "SQLITE_VERSION",
}

# Dangerous patterns (substring checks)
BLOCKED_PATTERNS = [
    "--",       # SQL comment
    "/*",       # Block comment start
    "*/",       # Block comment end
    ";",        # Statement separator
]


def query_db(sql: str, vault_path: Path, limit: int = DEFAULT_SQL_QUERY_LIMIT) -> str:
    """
    Execute a read-only SQL query against the cycling database.

    Args:
        sql: SQL SELECT query
        vault_path: Path to the cycling agent vault
        limit: Maximum rows to return

    Returns:
        Query results as a formatted string, or an error message.
    """
    sql_upper = sql.strip().upper()

    # Safety checks: blocked keywords
    for kw in BLOCKED_KEYWORDS:
        if kw in sql_upper:
            return f"ERROR: Query contains blocked keyword '{kw}'. Only SELECT queries are allowed."

    # Safety checks: blocked patterns (comments, statement separators)
    for pattern in BLOCKED_PATTERNS:
        if pattern in sql:
            return f"ERROR: Query contains blocked pattern '{pattern}'. Only simple SELECT queries are allowed."

    if not sql_upper.startswith("SELECT"):
        return "ERROR: Only SELECT queries are allowed."

    # Safety: block dangerous SQLite functions
    for func in BLOCKED_FUNCTIONS:
        if re.search(r'\b' + func + r'\s*\(', sql_upper):
            return f"ERROR: Query contains blocked function '{func}'. Only safe queries are allowed."

    # Safety: ensure only a single top-level statement (no chained statements)
    # Count top-level semicolons — already blocked by BLOCKED_PATTERNS, but double-check
    # by verifying the stripped SQL starts with SELECT and contains no additional keywords
    # that would indicate a second statement
    first_keyword = sql_upper.split()[0] if sql_upper.split() else ""
    if first_keyword != "SELECT":
        return "ERROR: Only SELECT queries are allowed."

    # Inject LIMIT if not present
    if "LIMIT" not in sql_upper:
        sql = f"{sql} LIMIT {limit}"

    db_path = vault_path / "data" / "cycling_agent.sqlite"
    if not db_path.exists():
        return f"ERROR: Database not found at {db_path}"

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(sql)
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        conn.close()

        if not rows:
            return "No results found."

        # Format as table
        lines = ["| " + " | ".join(columns) + " |"]
        lines.append("| " + " | ".join(["---"] * len(columns)) + " |")

        for row in rows:
            values = []
            for val in row:
                if val is None:
                    values.append("—")
                elif isinstance(val, float):
                    values.append(f"{val:.1f}")
                else:
                    values.append(str(val))
            lines.append("| " + " | ".join(values) + " |")

        return "\n".join(lines)

    except sqlite3.Error as e:
        return f"SQL ERROR: {e}"
    except Exception as e:
        return f"ERROR: {e}"