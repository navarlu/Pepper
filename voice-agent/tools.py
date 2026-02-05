import json
import logging

from livekit.agents import RunContext, function_tool

from .config import LOG_MAX_RESULT_CHARS, LOG_MAX_TOOL_RESULTS
from .utils import search_vectors

logger = logging.getLogger("voice-agent")


def build_tools():
    @function_tool
    async def query_search(context: RunContext, query: str) -> str:
        """Search the vector database for relevant information."""
        logger.info(
            "tool_call=%s query=%s",
            "query_search",
            query,
        )
        results = search_vectors(query=query, limit=5)
        preview = results[:LOG_MAX_TOOL_RESULTS]
        preview_text = json.dumps(preview, ensure_ascii=False)
        if len(preview_text) > LOG_MAX_RESULT_CHARS:
            preview_text = preview_text[:LOG_MAX_RESULT_CHARS] + "...(truncated)"
        logger.info(
            "tool_result=%s count=%s results_preview=%s",
            "query_search",
            len(results),
            preview_text,
        )
        return json.dumps({"results": results}, ensure_ascii=False)

    return [query_search]
