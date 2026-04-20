"""Conversational Insurance Claims Review Agent — Streamlit UI."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

import json
import streamlit as st

from backend.agent.intake import (
    extract_claim_fields,
    missing_required_fields,
    next_question,
    build_claim_dict,
    parse_targeted_answer,
    try_parse_json_claim,
    _normalize_provider,
)
from backend.models import Claim
from backend.agent.agent import review_claim


# ── Page config ────────────────────────────────────────────────────────────
st.set_page_config(page_title="Claims Review Agent", layout="wide", page_icon="🏥")

ACTION_COLORS = {
    "approve_pre_authorization": "green",
    "approve_claim_payment": "green",
    "deny_claim": "red",
    "request_missing_documentation": "orange",
    "route_to_senior_reviewer": "violet",
    "route_to_coding_review": "violet",
}
ACTION_LABELS = {
    "approve_pre_authorization": "✅ Pre-authorization Approved",
    "approve_claim_payment": "✅ Claim Payment Approved",
    "deny_claim": "❌ Claim Denied",
    "request_missing_documentation": "📋 Additional Documentation Required",
    "route_to_senior_reviewer": "👤 Escalated to Senior Reviewer",
    "route_to_coding_review": "🔍 Routed to Coding Review",
}


# ── Session state init ─────────────────────────────────────────────────────
def _init():
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "partial_claim" not in st.session_state:
        st.session_state.partial_claim = {}
    if "stage" not in st.session_state:
        # stages: intake | validating | reviewing | complete
        st.session_state.stage = "intake"
    if "result" not in st.session_state:
        st.session_state.result = None
    if "greeted" not in st.session_state:
        st.session_state.greeted = False
    if "awaiting_docs" not in st.session_state:
        st.session_state.awaiting_docs = []
    if "pending_field" not in st.session_state:
        st.session_state.pending_field = None  # field we last asked about

_init()


def add_message(role: str, content: str):
    st.session_state.messages.append({"role": role, "content": content})


def reset():
    for k in ["messages", "partial_claim", "stage", "result", "greeted"]:
        st.session_state.pop(k, None)
    _init()


# ── Layout ─────────────────────────────────────────────────────────────────
st.title("🏥 Insurance Claims Review Agent")
st.caption("Policy-emulation decision support · Not medical advice · Powered by Claude")

col_chat, col_status = st.columns([2, 1])

# ── Status sidebar ─────────────────────────────────────────────────────────
with col_status:
    st.subheader("Claim Fields")
    partial = st.session_state.partial_claim
    fields = {
        "Claim type": partial.get("claim_type"),
        "Member status": partial.get("member_status"),
        "Plan": partial.get("insurance_provider"),
        "Diagnosis code": partial.get("diagnosis_code"),
        "Procedure": partial.get("procedure_code"),
        "Amount": f"${partial.get('claimed_amount'):,.2f}" if partial.get("claimed_amount") else None,
        "Prior denial": partial.get("prior_denial_reason"),
        "Documents": ", ".join(partial.get("supporting_documents") or []) or None,
    }
    for label, val in fields.items():
        if val:
            st.markdown(f"**{label}:** {val}")
        else:
            st.markdown(f"<span style='color:#aaa'>**{label}:** —</span>", unsafe_allow_html=True)

    stage = st.session_state.stage
    st.divider()
    stage_labels = {
        "intake": "🟡 Gathering claim info",
        "validating": "🟡 Validating fields",
        "reviewing": "🔵 Running review...",
        "complete": "✅ Review complete",
    }
    st.markdown(f"**Status:** {stage_labels.get(stage, stage)}")

    if st.button("🔄 Start new claim", use_container_width=True):
        reset()
        st.rerun()

    # Show raw claim JSON once complete
    if st.session_state.stage == "complete" and st.session_state.result:
        with st.expander("📄 Full decision JSON"):
            st.code(json.dumps(st.session_state.result, indent=2), language="json")


# ── Chat panel ─────────────────────────────────────────────────────────────
with col_chat:
    # Greeting
    if not st.session_state.greeted:
        greeting = (
            "Hello! I'm your claims review assistant.\n\n"
            "Tell me about the claim you'd like to submit — you can describe it in plain English. "
            "For example:\n\n"
            "> *\"I have an active Blue Shield PPO member requesting pre-authorization for a lumbar spine MRI. "
            "Diagnosis is M54.5, claimed amount is $2,200.\"*\n\n"
            "I'll extract the details, ask for anything missing, then run the policy review."
        )
        add_message("assistant", greeting)
        st.session_state.greeted = True

    # Render history
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # Show decision result if complete (rendered once, not duplicated in messages)
    if st.session_state.stage == "complete" and st.session_state.result:
        result = st.session_state.result
        decision = result["decision"]
        action = decision["action"]
        color = ACTION_COLORS.get(action, "blue")
        label = ACTION_LABELS.get(action, action)

        with st.chat_message("assistant"):
            st.markdown(f"### :{color}[{label}]")
            st.info(decision["reason"])
            st.caption("_Review finalized — click 'Start new claim' to submit another._")

            if decision.get("payment_in_dollars") is not None:
                st.metric("Approved Payment", f"${decision['payment_in_dollars']:,.2f}")
            if decision.get("routing_target"):
                st.metric("Routed to", decision["routing_target"].replace("_", " ").title())
            if decision.get("requested_documents"):
                st.markdown("**Documents requested:**")
                for d in decision["requested_documents"]:
                    st.markdown(f"- {d}")

            if decision.get("citations"):
                st.markdown("**Policy citations:**")
                for c in decision["citations"]:
                    verified = c["score"] > 0
                    icon = "✅" if verified else "⚠️"
                    with st.expander(f"{icon} {c['section']}"):
                        st.caption(f"Plan: {c['plan']}")
                        st.markdown(f"> {c['excerpt']}")

            st.markdown("**Decision trace:**")
            step_icons = {"rule": "⚙️", "rag": "🔎", "llm": "🤖", "validate": "✔️", "guard": "🛡️"}
            step_labels = {
                "validate.input": "Step 0",
                "guard.input": "Step 1",
                "rule.": "Step 2",
                "rag.search": "Step 3",
                "rag.rule_grounding": "Step 3",
                "llm.": "Step 4",
                "validate.decision": "Step 5",
                "guard.low_confidence_retrieval": "Step 5",
                "guard.ungrounded_decision": "Step 5",
                "guard.output": "Step 6",
            }
            for step in result["trace"]:
                prefix = step["step"].split(".")[0]
                icon = step_icons.get(prefix, "•")
                # Find step label — exact match first, then prefix match
                label = step_labels.get(step["step"])
                if label is None:
                    for k, v in step_labels.items():
                        if k.endswith(".") and step["step"].startswith(k):
                            label = v
                            break
                label_str = f"[{label}] " if label else ""
                with st.expander(f"{icon} {label_str}{step['step']}"):
                    st.json(step["detail"])

    # Chat input (disabled once complete)
    if st.session_state.stage != "complete":
        user_input = st.chat_input("Describe the claim or answer the question above...")

        if user_input:
            add_message("user", user_input)
            with st.chat_message("user"):
                st.markdown(user_input)

            # ── Path 1: user is confirming awaited docs ──────────────────
            if st.session_state.awaiting_docs:
                declined = any(w in user_input.lower() for w in ["no", "don't have", "cannot", "can't", "skip"])
                existing = st.session_state.partial_claim.get("supporting_documents") or []
                if not declined:
                    new_docs = list(set(existing + st.session_state.awaiting_docs))
                    st.session_state.partial_claim["supporting_documents"] = new_docs
                    # If user is attaching content, append to provider_notes
                    if any(w in user_input.lower() for w in [" is ", ":"]):
                        prior = st.session_state.partial_claim.get("provider_notes") or ""
                        st.session_state.partial_claim["provider_notes"] = (prior + "\n" + user_input).strip()
                st.session_state.awaiting_docs = []

            # ── Path 2: user is answering a targeted field question ──────
            elif st.session_state.pending_field:
                field = st.session_state.pending_field
                value, error = parse_targeted_answer(field, user_input)
                if error:
                    add_message("assistant", error)
                    with st.chat_message("assistant"):
                        st.markdown(error)
                    st.rerun()
                if value is not None:
                    st.session_state.partial_claim[field] = value
                st.session_state.pending_field = None

            # ── Path 3: initial / free-text / JSON message ───────────────
            else:
                raw_json = try_parse_json_claim(user_input)

                if raw_json:
                    # Direct JSON parse — no LLM needed
                    provider_raw = raw_json.get("insurance_provider")
                    normalized, unsupported, needs_plan = _normalize_provider(provider_raw)
                    extracted = {
                        "member_status": raw_json.get("member_status"),
                        "claim_type": raw_json.get("claim_type"),
                        "insurance_provider": normalized,
                        "diagnosis_code": raw_json.get("diagnosis_code"),
                        "procedure_code": raw_json.get("procedure_code"),
                        "claimed_amount": raw_json.get("claimed_amount"),
                        "supporting_documents": raw_json.get("supporting_documents") or [],
                        "provider_notes": raw_json.get("provider_notes") or "",
                        "prior_denial_reason": raw_json.get("prior_denial_reason"),
                    }
                else:
                    # Natural-language input — run Claude extraction
                    with st.spinner("Extracting claim details..."):
                        raw = extract_claim_fields(
                            [{"role": m["role"], "content": m["content"]}
                             for m in st.session_state.messages]
                        )
                    provider_raw = raw.get("insurance_provider") or raw.pop("_unsupported_provider", None)
                    normalized, unsupported, needs_plan = _normalize_provider(provider_raw)
                    raw.pop("_unsupported_provider", None)
                    raw.pop("_needs_plan_type", None)
                    extracted = {**raw, "insurance_provider": normalized}

                # Handle unsupported provider
                if unsupported:
                    reply = (
                        f"This demo currently does not support **{unsupported}**. "
                        f"Please use one of the following Blue Shield of California plans: "
                        f"**PPO**, **EPO**, or **HMO Access+**."
                    )
                    add_message("assistant", reply)
                    with st.chat_message("assistant"):
                        st.markdown(reply)
                    st.rerun()

                # Merge non-null extracted values into partial claim
                for k, v in extracted.items():
                    if v is not None and v != "" and v != []:
                        st.session_state.partial_claim[k] = v

                # Blue Shield detected but plan type missing — ask once
                if needs_plan and not st.session_state.partial_claim.get("insurance_provider"):
                    reply = "Which Blue Shield plan type does the member have — **PPO**, **EPO**, or **HMO Access+**?"
                    add_message("assistant", reply)
                    st.session_state.pending_field = "insurance_provider"
                    with st.chat_message("assistant"):
                        st.markdown(reply)
                    st.rerun()

            partial = st.session_state.partial_claim
            missing = missing_required_fields(partial)

            if missing:
                # Still collecting — ask for the next missing field
                st.session_state.stage = "validating"
                st.session_state.pending_field = missing[0]
                question = next_question(missing)
                reply = f"Got it. I still need a few more details.\n\n{question}"
                add_message("assistant", reply)
                with st.chat_message("assistant"):
                    st.markdown(reply)
            else:
                # All fields present — run the review pipeline
                st.session_state.stage = "reviewing"
                claim_dict = build_claim_dict(partial)
                with st.spinner("Running policy review..."):
                    try:
                        claim = Claim(**claim_dict)
                        result_obj = review_claim(claim)
                        st.session_state.result = result_obj.model_dump()
                        action = result_obj.decision.action

                        if action == "request_missing_documentation":
                            # Keep conversation open — ask user to provide the docs
                            docs = result_obj.decision.requested_documents or []
                            st.session_state.awaiting_docs = docs
                            st.session_state.stage = "validating"
                            doc_list = "\n".join(f"- `{d}`" for d in docs)
                            reply = (
                                f"The policy review flagged missing documentation for this claim:\n\n"
                                f"{doc_list}\n\n"
                                f"Can you confirm you have these documents and want to re-submit? "
                                f"Say something like *\"yes, I have the clinical note\"* and I'll re-run the review."
                            )
                            add_message("assistant", reply)
                        else:
                            st.session_state.stage = "complete"
                    except Exception as e:
                        err = f"Review failed: {e}"
                        add_message("assistant", err)
                        st.session_state.stage = "validating"

            st.rerun()
