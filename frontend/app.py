"""Streamlit UI for the claim-review agent.

Run: streamlit run frontend/app.py
Requires: FastAPI backend running at http://localhost:8000
"""
from __future__ import annotations

import json
import os

import requests
import streamlit as st


API_BASE = os.getenv("CLAIMS_API", "http://localhost:8000")

ACTION_COLORS = {
    "approve_pre_authorization": ":green",
    "approve_claim_payment": ":green",
    "deny_claim": ":red",
    "request_missing_documentation": ":orange",
    "route_to_senior_reviewer": ":violet",
    "route_to_coding_review": ":violet",
}


st.set_page_config(page_title="Claims Review Agent", layout="wide")
st.title("Insurance Claims Review Agent")
st.caption("Policy-emulation decision support. Not medical advice.")


@st.cache_data(ttl=60)
def fetch_claims() -> list[dict]:
    r = requests.get(f"{API_BASE}/claims", timeout=10)
    r.raise_for_status()
    return r.json()


def review(claim: dict) -> dict:
    r = requests.post(f"{API_BASE}/review_claim", json=claim, timeout=120)
    r.raise_for_status()
    return r.json()


try:
    claims = fetch_claims()
except Exception as e:
    st.error(f"Cannot reach backend at {API_BASE}. Start it with `uvicorn backend.api.main:app --reload`.\n\n{e}")
    st.stop()


left, right = st.columns([1, 2])

with left:
    st.subheader("Claims")
    claim_ids = [c["claim_id"] for c in claims]
    selected_id = st.radio("Select a claim", claim_ids, label_visibility="collapsed")
    claim = next(c for c in claims if c["claim_id"] == selected_id)

    st.markdown("**Claim details**")
    st.json(claim, expanded=False)

    run = st.button("Review claim", type="primary", use_container_width=True)

with right:
    if run:
        with st.spinner("Running rule engine → retrieval → LLM → validation..."):
            try:
                result = review(claim)
            except Exception as e:
                st.error(f"Review failed: {e}")
                st.stop()

        decision = result["decision"]
        trace = result["trace"]

        action = decision["action"]
        color = ACTION_COLORS.get(action, ":blue")
        st.markdown(f"### Decision: {color}[**{action}**]")
        st.write(decision["reason"])

        cols = st.columns(3)
        if decision.get("payment_in_dollars") is not None:
            cols[0].metric("Payment", f"${decision['payment_in_dollars']:,.2f}")
        if decision.get("routing_target"):
            cols[1].metric("Routed to", decision["routing_target"])
        if decision.get("requested_documents"):
            cols[2].metric("Docs requested", len(decision["requested_documents"]))

        if decision.get("requested_documents"):
            st.markdown("**Requested documents**")
            for d in decision["requested_documents"]:
                st.markdown(f"- {d}")

        if decision.get("citations"):
            st.markdown("**Citations**")
            for c in decision["citations"]:
                verified = "verified" if c["score"] > 0 else "UNVERIFIED"
                with st.expander(f"{c['section']} — {verified}"):
                    st.caption(f"plan: {c['plan']}")
                    st.write(c["excerpt"])

        st.markdown("---")
        st.markdown("### Trace")
        for step in trace:
            with st.expander(step["step"]):
                st.json(step["detail"])

        with st.expander("Raw JSON"):
            st.code(json.dumps(result, indent=2), language="json")
    else:
        st.info("Pick a claim on the left and click **Review claim**.")
