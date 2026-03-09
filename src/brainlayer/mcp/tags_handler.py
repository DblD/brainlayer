"""Tags discovery MCP handler — brain_tags tool (Phase B)."""

import json
import re

from mcp.types import CallToolResult, TextContent

from ._shared import _error_result, _get_vector_store, logger

_STOP_WORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "for",
        "in",
        "on",
        "at",
        "to",
        "of",
        "with",
        "from",
        "by",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "not",
        "no",
        "so",
        "if",
        "do",
        "did",
        "has",
        "have",
        "had",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "can",
        "our",
        "my",
        "your",
        "their",
        "his",
        "her",
        "we",
        "they",
        "you",
        "he",
        "she",
        "all",
        "any",
        "but",
    ]
)


def _brain_tags(
    store,
    action: str,
    pattern: str | None = None,
    content: str | None = None,
    project: str | None = None,
    limit: int = 20,
) -> dict:
    """Tag discovery interface.

    Actions:
    - list: top tags with counts (optional project filter)
    - search: tags matching a prefix/pattern (case-insensitive)
    - suggest: suggest tags from content text

    Returns: {"tags": [{"tag": str, "count": int}, ...], "total": int}
    """
    cursor = store.conn.cursor()

    if action == "list":
        return _list_tags(cursor, project=project, limit=limit)
    elif action == "search":
        if not pattern:
            raise ValueError("'pattern' is required for action='search'")
        return _search_tags(cursor, pattern=pattern, limit=limit)
    elif action == "suggest":
        if not content:
            raise ValueError("'content' is required for action='suggest'")
        return _suggest_tags(cursor, content=content, limit=limit)
    else:
        return {"error": f"Unknown action '{action}'. Valid actions: list, search, suggest"}


def _list_tags(cursor, project: str | None, limit: int) -> dict:
    """Return top tags by chunk count, optionally filtered by project."""
    if project:
        cursor.execute(
            """
            SELECT ct.tag, COUNT(DISTINCT ct.chunk_id) AS count
            FROM chunk_tags ct
            JOIN chunks c ON c.id = ct.chunk_id
            WHERE c.project = ?
            GROUP BY ct.tag
            ORDER BY count DESC
            LIMIT ?
            """,
            (project, limit),
        )
    else:
        cursor.execute(
            """
            SELECT tag, COUNT(DISTINCT chunk_id) AS count
            FROM chunk_tags
            GROUP BY tag
            ORDER BY count DESC
            LIMIT ?
            """,
            (limit,),
        )
    rows = cursor.fetchall()
    tags = [{"tag": row[0], "count": row[1]} for row in rows]
    return {"tags": tags, "total": len(tags)}


def _search_tags(cursor, pattern: str, limit: int) -> dict:
    """Return tags matching prefix pattern (case-insensitive)."""
    cursor.execute(
        """
        SELECT tag, COUNT(DISTINCT chunk_id) AS count
        FROM chunk_tags
        WHERE LOWER(tag) LIKE LOWER(?)
        GROUP BY tag
        ORDER BY count DESC
        LIMIT ?
        """,
        (pattern + "%", limit),
    )
    rows = cursor.fetchall()
    tags = [{"tag": row[0], "count": row[1]} for row in rows]
    return {"tags": tags, "total": len(tags)}


def _suggest_tags(cursor, content: str, limit: int) -> dict:
    """Suggest existing tags from content keywords.

    Extracts keywords from content, finds tags matching any keyword,
    falls back to top tags if no matches found.
    """
    # Extract candidate keywords (3+ chars, filter stop words)
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9\-]{2,}", content.lower())
    keywords = [w for w in words if w not in _STOP_WORDS]

    if keywords:
        seen: dict[str, int] = {}
        for kw in keywords:
            cursor.execute(
                """
                SELECT tag, COUNT(DISTINCT chunk_id) AS count
                FROM chunk_tags
                WHERE LOWER(tag) LIKE ?
                GROUP BY tag
                """,
                ("%" + kw + "%",),
            )
            for row in cursor.fetchall():
                tag, count = row[0], row[1]
                if tag not in seen or seen[tag] < count:
                    seen[tag] = count

        if seen:
            sorted_tags = sorted(seen.items(), key=lambda x: x[1], reverse=True)[:limit]
            tags = [{"tag": tag, "count": count} for tag, count in sorted_tags]
            return {"tags": tags, "total": len(tags)}

    # Fall back to top tags
    return _list_tags(cursor, project=None, limit=limit)


async def _brain_tags_mcp(
    action: str,
    pattern: str | None = None,
    content: str | None = None,
    project: str | None = None,
    limit: int = 20,
) -> CallToolResult:
    """Async MCP wrapper for brain_tags."""
    import asyncio

    store = _get_vector_store()
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: _brain_tags(store, action=action, pattern=pattern, content=content, project=project, limit=limit),
        )
    except (ValueError, KeyError) as e:
        return _error_result(f"brain_tags validation error: {e}")
    except Exception as e:
        logger.exception("brain_tags failed")
        return _error_result(f"brain_tags failed: {e}")

    return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, indent=2))])
