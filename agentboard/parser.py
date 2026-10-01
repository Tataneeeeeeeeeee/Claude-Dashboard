"""Compatibility shim: the Claude Code transcript reader.

The reader moved to :mod:`agentboard.providers.claude` when the dashboard
became provider-agnostic, and the format-neutral helpers to
:mod:`agentboard.providers.common`.  This module keeps the old import path
working.
"""

from .model import (  # noqa: F401
    EMPTY_DAY_TOTALS,
    EMPTY_MODEL_TOTALS,
    EMPTY_USAGE,
    ParsedMessage,
    SessionMeta,
    parse_timestamp,
)
from .providers.claude import (  # noqa: F401
    MESSAGE_TYPES,
    STATE_TYPES,
    api_message_key,
    is_human_turn,
    parse_conversation,
    scan_session,
)
from .providers.common import (  # noqa: F401
    block_list,
    file_signature,
    iter_entries,
    iter_lines,
    text_of_blocks,
)

__all__ = [
    "SessionMeta",
    "ParsedMessage",
    "iter_lines",
    "iter_entries",
    "scan_session",
    "parse_conversation",
    "block_list",
    "text_of_blocks",
    "is_human_turn",
    "parse_timestamp",
    "STATE_TYPES",
    "MESSAGE_TYPES",
    "EMPTY_USAGE",
    "EMPTY_MODEL_TOTALS",
    "EMPTY_DAY_TOTALS",
    "api_message_key",
    "file_signature",
]
