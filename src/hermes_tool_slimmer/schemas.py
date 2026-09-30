STATUS_SCHEMA = {
    "name": "tool_slimmer_status",
    "description": "Return Hermes Tool Slimmer status and configuration.",
    "parameters": {"type": "object", "properties": {}},
}

SELECT_SCHEMA = {
    "name": "tool_slimmer_select",
    "description": "Select likely relevant tools for a query from provided tool schemas.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "schemas": {"type": "array", "items": {"type": "object"}},
            "mode": {"type": "string", "enum": ["keyword", "hybrid", "anthropic_tool_search", "two_pass", "jev"]},
        },
        "required": ["query"],
    },
}

HYDRATE_TOOLS_SCHEMA = {
    "name": "tool_slimmer_hydrate_tools",
    "description": (
        "Request full schemas for specific tools in experimental Tool Slimmer two-pass mode. "
        "This does not execute the tools; it only exposes their schemas on the next model call."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tool names whose full schemas should be exposed on the next model call.",
            },
            "reason": {
                "type": "string",
                "description": "Short explanation of why these full schemas are needed.",
            },
        },
        "required": ["tools"],
    },
}

REQUEST_FULL_TOOLS_SCHEMA = {
    "name": "tool_slimmer_request_full_tools",
    "description": (
        "Request the full Hermes tool schema set for the next model call when "
        "a required tool is missing from the trimmed tool list."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Short explanation of the missing tool or skill requirement.",
            }
        },
    },
}
