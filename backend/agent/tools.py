"""Tool schemas exposed to the Claude agent. These correspond 1:1 to the
actions listed in the spec. Output validation happens in agent.py after tool_use."""
from __future__ import annotations

TOOL_SCHEMAS = [
    {
        "name": "approve_pre_authorization",
        "description": (
            "Approve a pre-authorization request. Use ONLY when the claim_type is "
            "'preapproval', required documentation is present, and retrieved policy "
            "evidence supports medical necessity for the procedure under the member's plan."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Plain-English justification grounded in policy + claim data.",
                },
                "citations": {
                    "type": "array",
                    "description": "Policy citations supporting the approval.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "section": {"type": "string"},
                            "excerpt": {"type": "string"},
                        },
                        "required": ["section", "excerpt"],
                    },
                },
            },
            "required": ["reason", "citations"],
        },
    },
    {
        "name": "approve_claim_payment",
        "description": (
            "Approve payment on a standard claim or an overturned appeal. "
            "payment_in_dollars should generally equal claim.claimed_amount unless policy "
            "specifies a different allowed amount."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "payment_in_dollars": {
                    "type": "number",
                    "description": "Dollar amount to pay. Usually equals claim.claimed_amount.",
                },
                "reason": {"type": "string"},
                "citations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "section": {"type": "string"},
                            "excerpt": {"type": "string"},
                        },
                        "required": ["section", "excerpt"],
                    },
                },
            },
            "required": ["payment_in_dollars", "reason", "citations"],
        },
    },
    {
        "name": "deny_claim",
        "description": (
            "Deny the claim. Use when retrieved policy evidence shows the service is "
            "excluded, not medically necessary per the policy, or the appeal does not "
            "overcome the prior denial reason. Must cite the exclusion/limitation clause."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "citations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "section": {"type": "string"},
                            "excerpt": {"type": "string"},
                        },
                        "required": ["section", "excerpt"],
                    },
                },
            },
            "required": ["reason", "citations"],
        },
    },
    {
        "name": "request_missing_documentation",
        "description": "Request specific additional documents needed to adjudicate the claim.",
        "input_schema": {
            "type": "object",
            "properties": {
                "documents": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Specific document names to request.",
                },
                "reason": {"type": "string"},
            },
            "required": ["documents", "reason"],
        },
    },
    {
        "name": "route_to_senior_reviewer",
        "description": (
            "Route to a senior medical reviewer when the claim requires human judgment: "
            "policy is ambiguous, evidence is conflicting, or the agent is not confident."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "citations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "section": {"type": "string"},
                            "excerpt": {"type": "string"},
                        },
                        "required": ["section", "excerpt"],
                    },
                },
            },
            "required": ["reason"],
        },
    },
    {
        "name": "route_to_coding_review",
        "description": "Flag to a medical coder when diagnosis and procedure codes are inconsistent.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
            },
            "required": ["reason"],
        },
    },
]


TOOL_NAMES = {t["name"] for t in TOOL_SCHEMAS}
