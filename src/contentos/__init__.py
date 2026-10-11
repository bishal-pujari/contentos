"""contentos — agent-based content pipeline for SEO content."""

from contentos.core.compat import ensure_uuid7_available

# Run before anything imports langchain, whose ``uuid_utils`` dependency ships a native
# extension that a Windows Application Control policy can block (see core/compat.py).
ensure_uuid7_available()

__version__ = "0.1.0"
