"""Streamlit UI for the claim-review agent.

Deployment modes:
  - Streamlit Cloud: calls review_claim() directly (set ANTHROPIC_API_KEY in Secrets)
  - Local dev with separate FastAPI: set CLAIMS_API=http://localhost:8000
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import streamlit as st

# Load .env when running locally
try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

API_BASE = os.getenv("CLAIMS_API", "")  # empty → use direct import


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
    "approve_claim_payment": "✅ Claim Approved",
    "deny_claim": "❌ Claim Denied",
    "request_missing_documentation": "📋 Documentation Requested",
    "route_to_senior_reviewer": "👤 Escalated to Senior Reviewer",
    "route_to_coding_review": "🔍 Routed to Coding Review",
}


st.set_page_config(page_title="Claims Review Agent", layout="wide", page_icon="🏥")

st.title("🏥 Insurance Claims Review Agent")
st.caption("Policy-emulation decision support · Not medical advice · Powered by Claude")

ROOT = Path(__file__).resolve().parent.parent
CLAIMS_PATH = ROOT / "data" / "claims.json"


@st.cache_data(ttl=60)
def load_claims() -> list[dict]:
    return json.loads(CLAIMS_PATH.read_text())


def run_review(claim: dict) -> dict:
    if API_BASE:
        import requests
        r = requests.post(f"{API_BASE}/review_claim", json=claim, timeout=120)
        r.raise_for_status()
        return r.json()
    else:
        from backend.models import Claim
        from backend.agent.agent import review_claim
        result = review_claim(Claim(**claim))
        return result.model_dump()


claims = load_claims()

# ── Sidebar ────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Select Claim")
    claim_options = {
        f"{c['claim_id']} — {c['claim_type']} ({c['insurance_provider'].replace('Blue Shield ', '')})": c
        for c in claims
    }
    selected_label = st.radio("", list(claim_options.keys()), label_visibility="collapsed")
    claim = claim_options[selected_label]

    st.divider()
    st.subheader("Claim Details")
    for k, v in claim.items():
        if v and v != [] and v is not None:
            st.markdown(f"**{k}**")
            if isinstance(v, list):
                for item in v:
                    st.markdown(f"- {item}")
            else:
                st.markdown(f"{v}")

    st.divider()
    run = st.button("▶ Review Claim", type="primary", use_container_width=True)

# ── Main panel ─────────────────────────────────────────────────────────────
if run:
    with st.spinner("Running rule engine → retrieval → LLM → validation..."):
        try:
            result = run_review(claim)
        except Exception as e:
            st.error(f"Review failed: {e}")
            st.stop()

    decision = result["decision"]
    trace = result["trace"]
    action = decision["action"]
    color = ACTION_COLORS.get(action, "blue")
    label = ACTION_LABELS.get(action, action)

    st.markdown(f"## :{color}[{label}]")
    st.info(decision["reason"])

    # Metrics row
    cols = st.columns(4)
    cols[0].metric("Action", action.replace("_", " ").title())
    if decision.get("payment_in_dollars") is not None:
        cols[1].metric("Payment", f"${decision['payment_in_dollars']:,.2f}")
    if decision.get("routing_target"):
        cols[2].metric("Routed to", decision["routing_target"].replace("_", " ").title())
    if decision.get("requested_documents"):
        cols[3].metric("Docs needed", len(decision["requested_documents"]))

    # Requested docs
    if decision.get("requested_documents"):
        st.markdown("### 📋 Requested Documents")
        for d in decision["requested_documents"]:
            st.markdown(f"- {d}")

    # Citations
    if decision.get("citations"):
        st.markdown("### 📖 Policy Citations")
        for c in decision["citations"]:
            verified = c["score"] > 0
            icon = "✅" if verified else "⚠️"
            status = "verified" if verified else "UNVERIFIED — could not match to retrieved chunks"
            with st.expander(f"{icon} {c['section']} · {status}"):
                st.caption(f"Plan: {c['plan']}")
                st.markdown(f"> {c['excerpt']}")

    # Trace
    st.markdown("### 🔍 Decision Trace")
    step_icons = {
        "rule": "⚙️", "rag": "🔎", "llm": "🤖", "validate": "✔️", "guard": "🛡️"
    }
    for step in trace:
        prefix = step["step"].split(".")[0]
        icon = step_icons.get(prefix, "•")
        with st.expander(f"{icon} {step['step']}"):
            st.json(step["detail"])

    with st.expander("📄 Raw JSON"):
        st.code(json.dumps(result, indent=2), language="json")

else:
    st.markdown("""
### How it works

1. **Pick a claim** from the sidebar
2. Click **▶ Review Claim**
3. The agent runs:
   - Deterministic rule engine (inactive member, code mismatch, missing docs, high amount)
   - BM25 policy retrieval (per-plan namespace)
   - Claude tool-call decision with citation verification
4. See the decision, policy citations, and full trace

---

**Plans covered:** Blue Shield PPO · EPO · HMO Access+

**Actions available:** Approve · Deny · Request docs · Escalate to reviewer · Route to coding review
""")
