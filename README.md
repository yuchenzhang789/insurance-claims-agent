# Insurance Claims Review Agent

A working prototype of an AI-powered health-insurance claims-review system, submitted as a take-home design exercise. The agent reviews one claim at a time and returns one of six actions with a plain-English reason and verbatim policy citations.

The repository contains two artifacts:
- **A running prototype** — conversational intake → rule engine → policy RAG → Claude agent → structured decision with trace.
- **A design report** ([`report/REPORT.md`](report/REPORT.md)) — production system design, evaluation framework, and open-ended discussion. The report explains why every design decision was made and documents the intentional gaps between the prototype and a production system.

---

## System overview

```
User (chat)
    │
    ▼
Conversational intake loop
  Extract structured claim fields from free text (Claude NL extraction)
  Ask targeted follow-up questions until all required fields are present
    │
    ▼
7-step review pipeline
  1. validate.input   — Pydantic schema check
  2. guard.input      — carrier scope, injection heuristic, amount sanity
  3. rule engine      — 4 deterministic overrides (inactive member, dx/proc
                        mismatch, missing docs, amount > $5 000)
     └─ terminal?  ─→  Decision (LLM never called)
  4. rag.search       — BM25 per-plan namespace, source-boosted ranking
  5. llm              — Claude tool call (exactly one of six actions)
  6. validate.decision — JSON schema + citation span verifier
  7. guard.output     — medical-advice regex, ungrounded-decision check
    │
    ▼
Decision + citations + full trace
```

Every step writes a `TraceStep` into the result. Wrong denial? Walk the trace backwards.

---

## Six actions

| Tool | Used for |
|---|---|
| `approve_pre_authorization` | Preapproval: coverage criteria met |
| `approve_claim_payment` | Standard / overturned appeal: covered, no exclusion |
| `deny_claim` | Exclusion applies, or appeal new evidence insufficient |
| `request_missing_documentation` | Required docs absent |
| `route_to_senior_reviewer` | Ambiguity, low-confidence retrieval, high-value, guardrail block |
| `route_to_coding_review` | ICD-10 / CPT mismatch |

---

## Quickstart

### Prerequisites

- Python 3.11+
- An Anthropic API key

```bash
git clone https://github.com/yuchenzhang789/insurance-claims-agent.git
cd insurance-claims-agent
pip install -r requirements.txt
export ANTHROPIC_API_KEY=your_key_here
```

### Ingest policy documents (run once)

```bash
python -m backend.rag.ingest
# Reads policies/blueshield_*.pdf → writes data/chunks.json
```

### Start the API server

```bash
uvicorn backend.api.main:app --reload --port 8000
```

### Start the Streamlit UI

```bash
streamlit run frontend/app.py
```

Open `http://localhost:8501`. Pick a scenario from the sidebar or type a claim description in plain English.

---

## Demo scenarios

Six pre-loaded scenarios cover every pipeline path:

| Scenario | Plan | Claim type | Expected action | Pipeline path |
|---|---|---|---|---|
| MRI pre-authorization | PPO | preapproval | `approve_pre_authorization` | Rules pass → RAG → LLM approve |
| Inactive member denial | EPO | standard | `deny_claim` | Rule fires (inactive plan) — LLM never called |
| Missing clinical note | PPO | preapproval | `request_missing_documentation` | Rule fires (missing docs) — LLM never called |
| Experimental treatment | PPO | standard | `deny_claim` | Rules pass → RAG → LLM deny with exclusion citation |
| High-amount hip replacement | PPO | standard | `route_to_senior_reviewer` | Rule fires (amount > $5 000) — LLM never called |
| Reconstructive surgery appeal | PPO | appeal | `approve_claim_payment` | Rules pass → RAG → LLM overturns cosmetic-exclusion denial |

Scenarios 2, 3, and 5 demonstrate deterministic short-circuits — the LLM is never reached. Scenarios 1, 4, and 6 exercise the full retrieval + LLM path. Scenario 6 is the most complex: the prior denial cited cosmetic exclusion; the appeal succeeds because the PPO policy has an explicit carve-out for post-trauma reconstructive procedures.

### Free-text input

Describe a claim in plain English — the intake agent extracts structured fields and asks follow-up questions for anything missing. Or paste JSON directly to skip NL extraction:

```json
{
  "claim_type": "standard",
  "member_status": "active",
  "insurance_provider": "Blue Shield PPO",
  "diagnosis_code": "M54.5",
  "procedure_code": "MRI-LS-SPINE",
  "claimed_amount": 2200
}
```

### Demo scope

Blue Shield of California plans only — PPO (381 chunks), EPO (26 chunks), HMO (17 chunks). Entering a claim for any other carrier triggers the input guardrail and routes to senior review. This is correct behavior: the production design supports one indexed corpus per carrier/plan.

---

## Running the eval harness

```bash
python -m backend.eval.run_eval
# Emits backend/eval/last_run.json
```

The eval set covers all six tool actions, all four override conditions, and perturbation variants (missing docs, paraphrased notes, injected contradiction). Each failed case is tagged with a failure bucket (retrieval miss, citation hallucination, over-escalation, etc.) and matched against gold labels in `backend/eval/labels.json`.

---

## Project structure

```
.
├── backend/
│   ├── agent/
│   │   ├── agent.py        # review_claim(): rules → RAG → LLM → validation
│   │   ├── intake.py       # conversational field extraction + gap-filling loop
│   │   ├── prompts.py      # SYSTEM_PROMPT, build_user_message(), build_retrieval_query()
│   │   └── tools.py        # six tool schemas
│   ├── api/
│   │   └── main.py         # FastAPI: /health, /claims, /review_claim
│   ├── eval/
│   │   ├── run_eval.py     # offline harness: load cases → agent → score → report
│   │   ├── scorers.py      # deterministic scorers + LLM-judge rubric
│   │   └── labels.json     # gold labels for eval cases
│   ├── rag/
│   │   ├── ingest.py       # PDF → section-aware chunks → chunks.json
│   │   └── retriever.py    # BM25 per-plan namespace (PlanIndex, Retriever)
│   ├── rules/
│   │   ├── engine.py       # check_overrides(): 4-rule deterministic pipeline
│   │   └── codes.py        # ICD-10 ↔ procedure code consistency table
│   └── models.py           # Claim, Citation, Decision, TraceStep, ReviewResult
├── data/
│   ├── chunks.json         # pre-built BM25 index (generated by ingest.py)
│   └── claims.json         # sample claims from the case study
├── frontend/
│   └── app.py              # Streamlit UI: intake chat + decision card + trace panel
├── policies/               # Blue Shield PDF source documents
├── report/
│   └── REPORT.md           # full design report (Parts 1–4)
├── requirements.txt
└── DATA_NOTES.md           # data assumptions, gotchas, and resolution decisions
```

---

## Key design decisions

**Rules before LLMs.** Anything derivable from structured fields alone is decided in code — faster, cheaper, and fully auditable. The LLM only runs when genuine policy reasoning is required.

**Per-plan namespaced retrieval.** The BM25 index is hard-filtered by plan before the LLM is called. Cross-policy evidence leakage is impossible by construction.

**Citation span verification.** Every approve/deny must cite a verbatim excerpt from the retrieved chunks. A post-generation substring check confirms the excerpt exists in the chunks the LLM saw. Unverified citations auto-escalate.

**Abstain over guess.** Low retrieval confidence, failed citation verification, or ambiguous policy → `route_to_senior_reviewer`. Over-escalation is recoverable; a wrong auto-decision is not.

**Structured traces.** Every rule firing, retrieval score, LLM tool call, and validator result is logged. The trace is returned with every decision and rendered in the UI.

---

## Design report

[`report/REPORT.md`](report/REPORT.md) covers:

- **Part 1** — Agent design: storage/retrieval/ranking, workflow automation, response composition, safety and compliance boundaries.
- **Part 2** — Evaluation framework: dataset curation, metrics, offline harness, LLM-as-judge rubric, failure analysis.
- **Part 3** — Open-ended discussion: human escalation design, contradiction handling, conversational intake and mid-process gap filling.
- **Part 4** — Demo and prototype: why the prototype exists, prototype vs. production comparison table, component map (design section → code file), how to run and interpret the six scenarios.

The report uses a two-track format throughout: **Track A** is the production design; **Track B** is what the prototype actually does, with explicit notes on each gap.

---

## Environment variables

| Variable | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | (required) | Anthropic API key |
| `CLAIMS_AGENT_MODEL` | `claude-sonnet-4-5` | Model used for claim review |
| `CLAIMS_INTAKE_MODEL` | same as agent | Model used for NL field extraction; point at a cheaper model in production |
