"""Conversational claim intake: free-text → structured Claim fields."""
from __future__ import annotations

import json
import os
import uuid

from anthropic import Anthropic


INTAKE_MODEL = os.getenv("CLAIMS_INTAKE_MODEL", os.getenv("CLAIMS_AGENT_MODEL", "claude-sonnet-4-5"))
INTAKE_MODEL_TIER = "lightweight"  # Production: point CLAIMS_INTAKE_MODEL at a smaller/cheaper model

REQUIRED_FIELDS = [
    "member_status",
    "claim_type",
    "insurance_provider",
    "diagnosis_code",
    "procedure_code",
    "claimed_amount",
]

FIELD_QUESTIONS = {
    "member_status": "Is the member currently **active** or **inactive** on their plan?",
    "claim_type": "What type of claim is this — **standard** (post-service), **preapproval** (pre-authorization request), or **appeal** (disputing a prior denial)?",
    "insurance_provider": "Which Blue Shield plan does the member have — **PPO**, **EPO**, or **HMO Access+**? (This system only supports Blue Shield of California plans.)",
    "diagnosis_code": "What is the **diagnosis code** (ICD-10) for this claim? (e.g. M54.5 for lower back pain)",
    "procedure_code": "What **procedure or service** is being claimed? (e.g. MRI-LS-SPINE, OFFICE-VISIT, HIP-REPLACEMENT)",
    "claimed_amount": "What is the **claimed amount** in dollars?",
    "prior_denial_reason": "Since this is an appeal, what was the **reason given for the prior denial**?",
}

EXTRACT_SYSTEM = """You are a claim intake assistant. Extract insurance claim fields from the conversation.

Return ONLY valid JSON with these exact keys (use null for anything not yet mentioned):
{
  "member_status": "active" | "inactive" | null,
  "claim_type": "standard" | "preapproval" | "appeal" | null,
  "insurance_provider": "Blue Shield PPO" | "Blue Shield EPO" | "Blue Shield HMO" | null,
  "diagnosis_code": "<ICD-10 string>" | null,
  "procedure_code": "<canonical code from list below>" | null,
  "claimed_amount": <number> | null,
  "supporting_documents": ["<doc>", ...],
  "provider_notes": "<string>",
  "prior_denial_reason": "<string>" | null
}

## Canonical procedure codes — map any natural language to the closest match:
- MRI-LS-SPINE: lumbar spine MRI, lower back MRI, spinal MRI
- OFFICE-VISIT: office visit, clinic visit, outpatient visit, consultation
- APPENDECTOMY: appendix removal, appendectomy
- BIOLOGIC-INJECTION: biologic injection, biologic drug, biologic therapy, monoclonal antibody
- ORAL-ANTIBIOTIC: oral antibiotic, antibiotic pills, antibiotic prescription
- PSYCH-THERAPY: psychotherapy, therapy sessions, mental health therapy, counseling
- HIP-REPLACEMENT: hip replacement, hip arthroplasty, hip surgery
- LIPID-PANEL: lipid panel, cholesterol test, lipid test
- EPIDURAL-INJECTION: epidural injection, epidural steroid, nerve block
- PHYSICAL-THERAPY: physical therapy, PT, physiotherapy
- COLONOSCOPY: colonoscopy, colon screening
- MAMMOGRAM: mammogram, breast screening
- If no canonical match, use the clearest short uppercase code you can derive (e.g. KNEE-MRI, CARDIAC-STRESS-TEST)

## Other mapping rules:
- Plan names: "PPO" → "Blue Shield PPO", "EPO" → "Blue Shield EPO", "HMO" → "Blue Shield HMO"
- Member status: "active member", "enrolled" → "active"; "lapsed", "cancelled", "terminated" → "inactive"
- Claim type: "pre-auth", "prior authorization", "authorization" → "preapproval"; "appeal", "dispute", "reconsideration" → "appeal"; everything else → "standard"
- supporting_documents: use values from ["claim_form", "clinical_note", "op_note", "appeal_letter", "discharge_summary", "lab_results"]
- provider_notes: combine any clinical context into a single string; "" if none
- claimed_amount: extract number only, strip "$" and commas
- Do NOT invent values. Only extract what is explicitly stated or strongly implied.
"""


def try_parse_json_claim(text: str) -> dict | None:
    """Try to parse a raw string as a JSON claim object. Returns dict or None."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return None
    candidate = text[start:end + 1]
    # Collapse literal newlines inside string values (makes provider_notes safe)
    candidate = " ".join(candidate.splitlines())
    try:
        raw = json.loads(candidate)
        if isinstance(raw, dict) and any(k in raw for k in ("member_status", "claim_type", "diagnosis_code", "procedure_code")):
            return raw
    except Exception:
        pass
    return None


PROVIDER_MAP = {
    "blue shield ppo": "Blue Shield PPO",
    "blue shield epo": "Blue Shield EPO",
    "blue shield hmo": "Blue Shield HMO",
    "blue shield hmo access+": "Blue Shield HMO",
    "ppo": "Blue Shield PPO",
    "epo": "Blue Shield EPO",
    "hmo": "Blue Shield HMO",
    "hmo access+": "Blue Shield HMO",
}

# Companies that are Blue Shield of CA but need plan type clarification
BLUE_SHIELD_ALIASES = {
    "blue shield", "blue shield of california", "blue shield ca",
    "bcbs", "blue cross blue shield", "blue cross", "bluecross",
    "blueshield",
}

# Companies explicitly not supported
UNSUPPORTED_COMPANIES = {
    "aetna", "united healthcare", "unitedhealthcare", "uhc", "united health",
    "cigna", "humana", "kaiser", "molina", "anthem", "centene", "cvs",
    "health net", "healthnet",
}


def _normalize_provider(val: str | None) -> tuple[str | None, str | None, bool]:
    """Returns (normalized_provider, raw_val_if_unsupported, needs_plan_type).

    needs_plan_type=True means company is Blue Shield but plan (PPO/EPO/HMO) wasn't specified.
    """
    if not val:
        return None, None, False
    key = val.lower().strip()
    # Direct match (includes plan type)
    if key in PROVIDER_MAP:
        return PROVIDER_MAP[key], None, False
    # Blue Shield alias without plan type → ask for plan
    if key in BLUE_SHIELD_ALIASES:
        return None, None, True
    # Known unsupported company
    for company in UNSUPPORTED_COMPANIES:
        if company in key:
            return None, val, False
    # Unknown — treat as unsupported
    return None, val, False


def extract_claim_fields(messages: list[dict]) -> dict:
    """Run Claude extraction over the conversation history.

    First tries to parse a raw JSON claim object from the latest user message.
    Falls back to Claude NL extraction.
    Returns a partial claim dict (missing fields are None).
    """
    # Fast path: check latest user message for a JSON claim object
    last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), None)
    raw_json = try_parse_json_claim(last_user) if last_user else None
    if raw_json:
        provider, unsupported, needs_plan = _normalize_provider(raw_json.get("insurance_provider"))
        return {
            "member_status": raw_json.get("member_status"),
            "claim_type": raw_json.get("claim_type"),
            "insurance_provider": provider,
            "_unsupported_provider": unsupported,
            "_needs_plan_type": needs_plan,
            "diagnosis_code": raw_json.get("diagnosis_code"),
            "procedure_code": raw_json.get("procedure_code"),
            "claimed_amount": raw_json.get("claimed_amount"),
            "supporting_documents": raw_json.get("supporting_documents") or [],
            "provider_notes": raw_json.get("provider_notes") or "",
            "prior_denial_reason": raw_json.get("prior_denial_reason"),
        }

    client = Anthropic()
    resp = client.messages.create(
        model=INTAKE_MODEL,
        max_tokens=512,
        system=EXTRACT_SYSTEM,
        messages=messages,
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        result = json.loads(text.strip())
        # Normalize provider in NL path too
        if result.get("insurance_provider"):
            normalized, unsupported, needs_plan = _normalize_provider(result["insurance_provider"])
            result["insurance_provider"] = normalized
            result["_unsupported_provider"] = unsupported
            result["_needs_plan_type"] = needs_plan
        return result
    except Exception:
        return {}


def parse_targeted_answer(field: str, answer: str) -> tuple[str | None, str | None]:
    """Parse a user's answer to a specific field question.

    Returns (value_to_set, error_message_or_None).
    """
    answer = answer.strip()
    key = answer.lower()

    if field == "member_status":
        if any(w in key for w in ["active", "enrolled", "yes"]):
            return "active", None
        if any(w in key for w in ["inactive", "lapsed", "cancel", "terminat", "no"]):
            return "inactive", None
        return None, f"Please answer **active** or **inactive**."

    if field == "claim_type":
        if any(w in key for w in ["preapproval", "pre-approval", "pre approval", "prior auth", "authorization", "preauth"]):
            return "preapproval", None
        if any(w in key for w in ["appeal", "dispute", "reconsider"]):
            return "appeal", None
        if any(w in key for w in ["standard", "regular", "normal", "post"]):
            return "standard", None
        return None, "Please answer **standard**, **preapproval**, or **appeal**."

    if field == "insurance_provider":
        normalized, unsupported, needs_plan = _normalize_provider(answer)
        if normalized:
            return normalized, None
        if needs_plan:
            return None, "Which plan type — **PPO**, **EPO**, or **HMO Access+**?"
        if unsupported:
            return None, f"This demo does not support **{unsupported}**. Please specify **Blue Shield PPO**, **EPO**, or **HMO Access+**."
        return None, "Please specify **Blue Shield PPO**, **Blue Shield EPO**, or **Blue Shield HMO Access+**."

    if field == "claimed_amount":
        import re
        nums = re.findall(r"[\d,]+\.?\d*", answer.replace("$", ""))
        if nums:
            try:
                return float(nums[0].replace(",", "")), None
            except ValueError:
                pass
        return None, "Please provide a dollar amount (e.g. **$2,200**)."

    if field == "prior_denial_reason":
        if answer:
            return answer, None
        return None, "Please describe the reason given for the prior denial."

    # For free-text fields (diagnosis_code, procedure_code) return as-is
    if answer:
        return answer, None
    return None, None


def missing_required_fields(partial: dict) -> list[str]:
    """Return list of field names still needed to run the review pipeline."""
    missing = []
    for f in REQUIRED_FIELDS:
        val = partial.get(f)
        if val is None or (isinstance(val, str) and not val.strip()):
            missing.append(f)
    # prior_denial_reason required for appeals
    if partial.get("claim_type") == "appeal":
        val = partial.get("prior_denial_reason")
        if not val or (isinstance(val, str) and not val.strip()):
            missing.append("prior_denial_reason")
    return missing


def next_question(missing: list[str]) -> str:
    """Return the question to ask for the first missing field."""
    if not missing:
        return ""
    return FIELD_QUESTIONS.get(missing[0], f"Can you provide the **{missing[0].replace('_', ' ')}**?")


def build_claim_dict(partial: dict) -> dict:
    """Merge extracted fields with safe defaults and assign a claim_id."""
    return {
        "claim_id": f"CLM-{uuid.uuid4().hex[:6].upper()}",
        "member_status": partial.get("member_status", "active"),
        "claim_type": partial.get("claim_type", "standard"),
        "insurance_provider": partial.get("insurance_provider", ""),
        "diagnosis_code": partial.get("diagnosis_code", ""),
        "procedure_code": partial.get("procedure_code", ""),
        "claimed_amount": float(partial.get("claimed_amount") or 0),
        "supporting_documents": partial.get("supporting_documents") or [],
        "provider_notes": partial.get("provider_notes") or "",
        "prior_denial_reason": partial.get("prior_denial_reason"),
    }
