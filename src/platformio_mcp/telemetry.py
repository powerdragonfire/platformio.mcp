"""Allowlisted client metadata shared by the recorder and PostHog SDK.

Original prompts are accepted only from explicit host metadata with local opt-in.
No conversation files, arbitrary arguments, or client configuration files are read.
"""

from __future__ import annotations

import os
import re
from typing import Any

from . import __version__

EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,127}")
PROMPT_LIMIT = 2000


def context_meta(context: Any) -> dict[str, Any]:
    """Read public MCP request metadata; malformed contexts contribute nothing."""
    try:
        context = getattr(context, "request_context", context)
        meta = getattr(context, "meta", None)
        if hasattr(meta, "model_dump"):
            meta = meta.model_dump(mode="python", by_alias=True)
        return meta if isinstance(meta, dict) else {}
    except Exception:
        return {}


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def metadata_properties(meta: Any) -> dict[str, Any]:
    """Prefer per-call metadata over explicit deployment configuration, recording sources."""
    meta = _mapping(meta)
    codex = _mapping(meta.get("x-codex-turn-metadata"))
    host = _mapping(meta.get("platformio.mcp"))
    configured = {
        "model": os.environ.get("PLATFORMIO_MCP_MODEL"),
        "reasoning_effort": os.environ.get("PLATFORMIO_MCP_REASONING_EFFORT"),
    }
    props: dict[str, Any] = {}
    for field, property_name in (("model", "$mcp_llm_model"), ("reasoning_effort", "reasoning_effort")):
        for values, source in ((codex, "client_metadata"), (host, "client_metadata"), (configured, "configuration")):
            value = values.get(field)
            if not isinstance(value, str):
                continue
            value = value.strip()
            if field == "model":
                valid = bool(MODEL.fullmatch(value)) and value.lower() != "unknown"
            else:
                value = value.lower()
                valid = value in EFFORTS
            if valid:
                props[property_name] = value
                props[property_name + "_source"] = source
                break
    # Exact explicit opt-in; remote opt-outs are enforced by each delivery path.
    if os.environ.get("PLATFORMIO_MCP_CAPTURE_PROMPTS") == "1":
        prompt = host.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            props["user_prompt"] = prompt[:PROMPT_LIMIT]
            props["user_prompt_source"] = "client_metadata"
            props["user_prompt_truncated"] = len(prompt) > PROMPT_LIMIT
    return props


def sdk_properties(request: dict[str, Any], extra: Any) -> dict[str, Any]:
    """SDK callback: metadata is available through extra.ctx on MCP v2."""
    meta = context_meta(_mapping(extra).get("ctx"))
    if not meta:
        meta = _mapping(_mapping(request.get("params")).get("_meta"))
    properties = metadata_properties(meta)
    arguments = _mapping(_mapping(request.get("params")).get("arguments"))
    model = arguments.get("llm_model")
    # The SDK's per-call self-report is fresher than static deployment configuration.
    if properties.get("$mcp_llm_model_source") == "configuration" and isinstance(model, str) and model.strip() and model.strip().lower() != "unknown":
        properties.pop("$mcp_llm_model", None)
        properties.pop("$mcp_llm_model_source", None)
    return {"platformio_mcp_version": __version__, **properties}
