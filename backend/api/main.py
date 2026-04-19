"""FastAPI app exposing the claim-review agent."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from backend.models import Claim, ReviewResult
from backend.agent.agent import review_claim


app = FastAPI(title="Insurance Claims Review Agent", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


_ROOT = Path(__file__).resolve().parents[2]
_CLAIMS_PATH = _ROOT / "data" / "claims.json"


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/claims")
def list_claims() -> list[dict]:
    return json.loads(_CLAIMS_PATH.read_text())


@app.post("/review_claim", response_model=ReviewResult)
def post_review_claim(claim: Claim) -> ReviewResult:
    try:
        return review_claim(claim)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")
