"""Anthropic tool definitions for GovFlow agent and tool validation schema."""

from __future__ import annotations

from typing import Any

AGENT_TOOLS: list[dict[str, Any]] = [
    {
        "name": "click",
        "description": "Click an interactive element by its [id] from the element list.",
        "input_schema": {
            "type": "object",
            "properties": {
                "element_id": {
                    "type": "integer",
                    "description": "The numeric ID of the element to click.",
                }
            },
            "required": ["element_id"],
        },
    },
    {
        "name": "type",
        "description": "Type text into an input or textarea element. You may reference citizen profile attributes symbolically (e.g., 'profile.name', 'profile.dob|DD/MM/YYYY', 'profile.id_number', 'profile.address|one_line', 'profile.phone', 'extra.<param>').",
        "input_schema": {
            "type": "object",
            "properties": {
                "element_id": {
                    "type": "integer",
                    "description": "The numeric ID of the input field.",
                },
                "text": {
                    "type": "string",
                    "description": "The text to type or a symbolic reference like 'profile.name'.",
                },
                "press_enter": {
                    "type": "boolean",
                    "description": "Whether to press Enter after typing.",
                    "default": False,
                },
            },
            "required": ["element_id", "text"],
        },
    },
    {
        "name": "select_option",
        "description": "Select an option from a <select> dropdown by option text or value.",
        "input_schema": {
            "type": "object",
            "properties": {
                "element_id": {
                    "type": "integer",
                    "description": "The numeric ID of the <select> element.",
                },
                "value": {
                    "type": "string",
                    "description": "The text or value of the option to select.",
                },
            },
            "required": ["element_id", "value"],
        },
    },
    {
        "name": "check",
        "description": "Set a checkbox or radio button to checked (true) or unchecked (false).",
        "input_schema": {
            "type": "object",
            "properties": {
                "element_id": {
                    "type": "integer",
                    "description": "The numeric ID of the checkbox or radio input.",
                },
                "checked": {
                    "type": "boolean",
                    "description": "Target state: true to check, false to uncheck.",
                    "default": True,
                },
            },
            "required": ["element_id"],
        },
    },
    {
        "name": "upload_file",
        "description": "Attach a citizen document to a file input by doc_key (e.g. 'id_proof', 'photo', 'income_certificate', 'marksheet').",
        "input_schema": {
            "type": "object",
            "properties": {
                "element_id": {
                    "type": "integer",
                    "description": "The numeric ID of the file input element.",
                },
                "doc_key": {
                    "type": "string",
                    "description": "The document key from the profile/plan requirements (e.g. 'id_proof', 'photo').",
                },
            },
            "required": ["element_id", "doc_key"],
        },
    },
    {
        "name": "scroll",
        "description": "Scroll the page viewport or scroll a specific element into view.",
        "input_schema": {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["up", "down", "left", "right"],
                    "description": "Scroll direction (default 'down').",
                    "default": "down",
                },
                "amount": {
                    "type": "integer",
                    "description": "Scroll distance in pixels (default 600).",
                    "default": 600,
                },
                "element_id": {
                    "type": "integer",
                    "description": "Optional: element ID to scroll directly into view.",
                },
            },
        },
    },
    {
        "name": "click_xy",
        "description": "Fallback click by viewport (x, y) coordinates ONLY when the target element is missing from the interactive list.",
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "number", "description": "X coordinate in viewport."},
                "y": {"type": "number", "description": "Y coordinate in viewport."},
                "reason": {
                    "type": "string",
                    "description": "Detailed explanation of why the element is not in the set-of-marks list.",
                },
            },
            "required": ["x", "y", "reason"],
        },
    },
    {
        "name": "press_key",
        "description": "Send a keyboard key press (Enter, Tab, Escape, etc.).",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Key name: Enter, Tab, Escape, Backspace, Delete, Space, ArrowDown, ArrowUp, etc.",
                }
            },
            "required": ["key"],
        },
    },
    {
        "name": "wait",
        "description": "Wait for page animations, network requests, or dropdown population.",
        "input_schema": {
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "number",
                    "description": "Duration in seconds (0.5 to 10.0).",
                    "default": 1.0,
                }
            },
        },
    },
    {
        "name": "go_back",
        "description": "Navigate back to the previous page in history.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "ask_human",
        "description": "Pause execution to ask the citizen for help: solving a CAPTCHA, entering an OTP, logging in, resolving an ambiguous option, providing missing data, or confirming final submission.",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": [
                        "otp",
                        "captcha",
                        "login",
                        "registration",
                        "ambiguous_choice",
                        "missing_data",
                        "confirm_submit",
                    ],
                    "description": "The category of help required.",
                },
                "question": {
                    "type": "string",
                    "description": "A clear, polite message explaining what is needed.",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of choices for ambiguous_choice.",
                },
                "summary": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "Summary of entered fields to show for confirm_submit.",
                },
            },
            "required": ["kind", "question"],
        },
    },
    {
        "name": "save_progress",
        "description": "Record a note or checkpoint after saving a form draft on the portal.",
        "input_schema": {
            "type": "object",
            "properties": {
                "note": {
                    "type": "string",
                    "description": "Description of saved progress (e.g. 'Draft saved at Step 2').",
                }
            },
            "required": ["note"],
        },
    },
    {
        "name": "finish",
        "description": "Mark the job as completed when the final confirmation/acknowledgement receipt with tracking or reference number is reached.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reference_no": {
                    "type": "string",
                    "description": "The application reference, ack, or tracking number extracted from the receipt page.",
                },
                "summary": {
                    "type": "string",
                    "description": "Summary of completed submission.",
                },
            },
            "required": ["reference_no"],
        },
    },
]


def get_agent_tools() -> list[dict[str, Any]]:
    return list(AGENT_TOOLS)

