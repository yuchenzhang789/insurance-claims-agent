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

DEMO_PRESETS = {
    "— Select a scenario —": None,
    "✅ Approve preauth (PPO, lumbar MRI)": (
        "Active Blue Shield PPO member requesting preauthorization for a lumbar spine MRI. "
        "Diagnosis M54.5, procedure MRI-LS-SPINE, claimed amount $2,200. "
        "Supporting documents: clinical_note. "
        "Provider notes: Persistent low back pain for 6 months, conservative therapy failed."
    ),
    "📋 Missing documentation (PPO preauth)": (
        "Active Blue Shield PPO member requesting preauthorization for a biologic injection. "
        "Diagnosis J45.40, procedure BIOLOGIC-INJECTION, claimed amount $12,000. "
        "No supporting documents attached yet. "
        "Provider notes: Severe persistent asthma uncontrolled on inhaled steroids."
    ),
    "❌ Inactive member (EPO standard)": (
        "Inactive Blue Shield EPO member submitting a standard claim for an office visit. "
        "Diagnosis E11.9, procedure OFFICE-VISIT, claimed amount $150. "
        "Supporting documents: claim_form. "
        "Provider notes: Routine visit for diabetes follow-up."
    ),
    "🔍 Code mismatch (PPO standard)": (
        "Active Blue Shield PPO member submitting a standard claim. "
        "Diagnosis Z00.00 (general adult medical exam), procedure APPENDECTOMY, claimed amount $7,800. "
        "Supporting documents: op_note. "
        "Provider notes: Appendectomy performed in emergency setting."
    ),
    "👤 High-amount senior review (PPO, hip replacement)": (
        "Active Blue Shield PPO member submitting a standard claim for hip replacement. "
        "Diagnosis S72.001A, procedure HIP-REPLACEMENT, claimed amount $15,500. "
        "Supporting documents: op_note, discharge_summary. "
        "Provider notes: Hip fracture treated surgically."
    ),
    "🔁 Appeal with new evidence (EPO, psychotherapy)": (
        "Active Blue Shield EPO member submitting an appeal for psychotherapy. "
        "Diagnosis F32.9, procedure PSYCH-THERAPY, claimed amount $900. "
        "Supporting documents: appeal_letter, clinical_note. "
        "Prior denial reason: Insufficient documentation of medical necessity. "
        "Provider notes: Appeal for previously denied sessions, now including updated progress notes "
        "demonstrating medical necessity and functional impairment."
    ),
}


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
    if "preset_to_inject" not in st.session_state:
        st.session_state.preset_to_inject = None

_init()


def compute_confidence(result: dict) -> dict:
    """Composite confidence — mirrors the production design's §Step 5 composite signal.
    Inputs: retrieval strength, citation verification, guard outcomes, rule-terminal status.
    """
    trace = result.get("trace", [])
    decision = result.get("decision", {})

    # Pull signals from the trace
    rag_step = next((t for t in trace if t["step"] == "rag.search"), None)
    top_score = (rag_step["detail"].get("top_score") or 0.0) if rag_step else None
    low_conf_guard = any(t["step"] == "guard.low_confidence_retrieval" for t in trace)
    ungrounded_guard = any(t["step"] == "guard.ungrounded_decision" for t in trace)
    output_guard = next((t for t in trace if t["step"] == "guard.output"), None)
    output_passed = bool(output_guard and output_guard["detail"].get("passed"))
    rule_terminal = any(
        t["step"].startswith("rule.") and (
            t["detail"].get("triggered") or
            t["detail"].get("consistent") is False or
            t["detail"].get("missing")
        )
        for t in trace
    )

    cits = decision.get("citations") or []
    verified = sum(1 for c in cits if (c.get("score") or 0) > 0)
    cit_rate = f"{verified}/{len(cits)}" if cits else "0/0"

    # Tier logic
    if rule_terminal:
        tier = "HIGH"
        tier_reason = "Deterministic rule terminal — no model judgment needed."
    elif low_conf_guard or ungrounded_guard:
        tier = "LOW"
        tier_reason = "Escalated by a safety guard (insufficient grounding)."
    elif not output_passed:
        tier = "LOW"
        tier_reason = "Output guardrail flagged the decision."
    elif cits and verified == len(cits) and top_score and top_score >= 3.0:
        tier = "HIGH"
        tier_reason = "All citations verified; strong retrieval score."
    elif cits and verified == len(cits):
        tier = "MEDIUM"
        tier_reason = "Citations verified but retrieval score is marginal."
    else:
        tier = "LOW"
        tier_reason = "Some citations did not verify against retrieved chunks."

    return {
        "tier": tier,
        "tier_reason": tier_reason,
        "top_score": top_score,
        "citations_verified": cit_rate,
        "output_guard_passed": output_passed if output_guard else None,
        "rule_terminal": rule_terminal,
    }


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

    st.divider()
    st.markdown("**🎬 Try a scenario**")
    st.caption("Pre-loaded claims — each exercises a different pipeline path.")
    preset_choice = st.selectbox(
        "Scenario",
        list(DEMO_PRESETS.keys()),
        label_visibility="collapsed",
        key="preset_select",
    )
    if st.button("▶️ Run scenario", use_container_width=True,
                 disabled=(DEMO_PRESETS[preset_choice] is None or st.session_state.stage == "reviewing")):
        reset()
        st.session_state.preset_to_inject = DEMO_PRESETS[preset_choice]
        st.rerun()

    # Composite confidence + raw decision JSON once complete
    if st.session_state.stage == "complete" and st.session_state.result:
        st.divider()
        conf = compute_confidence(st.session_state.result)
        tier_color = {"HIGH": "green", "MEDIUM": "orange", "LOW": "red"}.get(conf["tier"], "gray")
        st.markdown(f"**Confidence:** :{tier_color}[**{conf['tier']}**]")
        st.caption(conf["tier_reason"])
        st.markdown(
            f"- BM25 top score: `{conf['top_score']:.2f}`" if conf["top_score"] is not None
            else "- BM25 top score: _n/a (rule-terminal)_"
        )
        st.markdown(f"- Citations verified: `{conf['citations_verified']}`")
        if conf["output_guard_passed"] is not None:
            st.markdown(f"- Output guard: {'✅ passed' if conf['output_guard_passed'] else '❌ failed'}")
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
                "llm.appeal_context": "Step 3b",
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
        typed_input = st.chat_input("Describe the claim or answer the question above...")

        # Preset injection: treat a preset exactly like a typed user message
        user_input = typed_input
        if user_input is None and st.session_state.preset_to_inject:
            user_input = st.session_state.preset_to_inject
            st.session_state.preset_to_inject = None

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
