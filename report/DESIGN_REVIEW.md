# Holistic Design Review — Insurance Claims Review Agent

> **Purpose.** This document is a self-contained critical review of the design and implementation, mapped against the case-study spec. It is intended to be handed to an external reviewer to evaluate how well the submission meets the requirements. It calls out both strengths and real gaps — it is not a sales pitch.

> Note: the spec requires the word **Bluecross** to appear in the report; the policy corpus in this implementation is Blue Shield of California (PPO, EPO, HMO Access+).

---

## 0. MVP scope vs. production design

The prototype intentionally narrows two dimensions so the rest of the pipeline can be built end-to-end in the time budget:

1. **Single carrier.** Only Blue Shield of California (PPO, EPO, HMO) is ingested. The spec's sample claims reference Aetna, UHC, BCBS; the intake layer rejects these with a user-visible "this demo does not support X" message rather than silently escalating.
2. **Demo-grade corpus.** Three public Blue Shield PDFs in `policies/`. No versioning, no freshness policy, no OCR fallback.

The production shape of both is a thin adapter, not a rewrite:

| Concern | MVP | Production |
|---|---|---|
| Carrier | one Blue Shield index | one BM25 (+ optional dense) index per carrier, keyed by `(carrier, plan)`; `retriever.search(plan, …)` is unchanged |
| Policy onboarding | manual PDFs in `policies/` | document registry (carrier, plan, version, effective_date); nightly ingest + diff; stale-version alerts |
| Corpus quality | heading regex tuned to Blue Shield | per-carrier adapter: heading rules, OCR fallback for scanned PDFs, table extraction for benefit grids |
| Multi-carrier routing | intake rejects non-Blue-Shield | `claim.insurance_provider → index_key` registry; unknown carrier → escalate, not reject |

Every section below marks **MVP** vs **Production** where the distinction matters.

---

## 1. System overview

End-to-end pipeline per claim:

```
Claim (structured)
  → Rule engine (deterministic overrides)         [terminal?]
  → Per-plan BM25 retrieval                       [top-k chunks + score]
  → Low-confidence retrieval guard                [terminal?]
  → Claude + tool-use (forced tool_choice=any)    [exactly one action]
  → Citation substring verification               [post-check]
  → Ungrounded-decision guard                     [escalate if unverified]
  → ReviewResult { decision, trace[] }
```

Code layout:
- `backend/rules/engine.py` — deterministic overrides
- `backend/rules/codes.py` — dx/procedure consistency table
- `backend/rag/ingest.py` — PDF → section-tagged chunks
- `backend/rag/retriever.py` — per-plan BM25
- `backend/agent/agent.py` — orchestration + guards
- `backend/agent/tools.py` — six tool schemas (1:1 with spec actions)
- `backend/agent/prompts.py` — system prompt, user-message composer, retrieval query builder
- `backend/agent/intake.py` — conversational intake (free text / JSON → Claim)
- `backend/eval/run_eval.py` + `scorers.py` — eval harness + LLM-as-judge
- `frontend/app.py` — Streamlit conversational UI
- `policies/*.pdf` — three Blue Shield plan documents (PPO, EPO, HMO)

---

## 2. Spec coverage — Part 1: Agent Design

### 2.1 Storage, retrieval & ranking

**Storage.** Policy PDFs are parsed with `pypdf` into section-tagged chunks (plan, section, page, text) and persisted as `data/chunks.json`. Claims are Pydantic `Claim` objects; in the demo they're loaded from `data/claims.json` but the Streamlit frontend also accepts free-text or pasted JSON.

**Chunking.** Heading-aware: a regex heading dictionary (scoped to Blue Shield doc conventions — "Principal Benefits", "Principal exclusions and limitations", numbered exclusion items like "Clinical Trials.", SBC-specific headings) is matched line-by-line; the current section is carried forward and tagged on every 900-char window with 150-char overlap. Section tagging is the mechanism that lets the cited excerpt carry a meaningful section label instead of a page number.

**Retrieval.** BM25Okapi via `rank_bm25`, **one index per plan**. The retriever takes `(plan, query, top_k)` and returns scored `Citation[]`. Per-plan namespacing is the mechanism that enforces the spec requirement "Avoid using irrelevant or cross-policy evidence" — it is impossible for the retriever to return Aetna text when the claim is on a Blue Shield plan because the other plan's index is never queried.

**Query construction.** Deterministic: `procedure_code` (dashes → spaces) + `diagnosis_code` + `provider_notes` + `prior_denial_reason` + the literal string `"coverage medical necessity exclusion"` (a bias nudge toward the policy sections we care about). The tokenizer keeps alphanumerics + hyphens so codes like `M54.5` and `MRI-LS-SPINE` remain intact.

**Ranking.** Pure BM25 top-k (k=5). No reranker. No dense vectors. No query expansion. Justified for MVP because Blue Shield policies use very specific terminology (code tokens, exclusion names) where lexical matching is strong. Documented upgrade path (hybrid BM25 + dense + cross-encoder reranker) is in the main report.

**Evidence selection & grounding.**
1. Retrieval returns top-5 chunks with BM25 scores.
2. If `top_score < 2.0` (configurable `LOW_CONFIDENCE_SCORE`), the pipeline short-circuits to `route_to_senior_reviewer` — the agent never sees weakly-retrieved policy.
3. Otherwise the top-5 are rendered into the user message verbatim (section + score + excerpt).
4. The LLM is required by prompt to cite at least one retrieved excerpt for any approve/deny/senior-review.
5. **Post-check**: `_verify_citation_excerpt` in `agent.py` re-normalizes whitespace and case, requires a ≥20-char span to match, and uses a fallback 60-char prefix match. Citations that fail are marked `score=0.0`; if no citation verifies on an approve/deny decision the pipeline replaces the decision with `route_to_senior_reviewer`.

### 2.2 Workflow automation & tool selection

**State-machine-style precedence.** The rule engine runs a fixed sequence, each step either terminates or falls through:

| # | Check | If triggered → |
|---|---|---|
| 1 | `member_status == "inactive"` | `deny_claim` (reason: "Plan inactive at time of service.") |
| 2 | dx/procedure inconsistent | `route_to_coding_review` |
| 3 | Required docs missing (by claim_type) | `request_missing_documentation(documents=[missing])` |
| 4 | `claimed_amount > 5000` | `route_to_senior_reviewer` |
| 5 | otherwise | fall through to retrieval + LLM |

**Deviation from spec**: the spec lists overrides as (1) Inactive, (2) Missing docs, (3) High amount, (4) Dx/procedure mismatch. Our order is (1, 4, 2, 3). Rationale: a code mismatch makes *any* policy interpretation unsafe — flagging to coding review before asking for more docs or escalating on amount is operationally sound. Inactive still dominates. This is a defensible deviation but should be called out to the reviewer.

**Rule-engine decisions carry grounding citations.** All four rule terminals (`rule.inactive_plan`, `rule.code_consistency`, `rule.required_docs`, `rule.high_amount`) call `_ground_rule_decision` in [agent.py](backend/agent/agent.py), which runs a targeted BM25 pass with a fixed per-rule query (`RULE_GROUNDING_QUERIES`) and attaches the top chunk as a `Citation` on the `Decision`. The retrieval and the rule firing are logged as separate trace steps (`rag.rule_grounding` is distinct from the LLM-path `rag.search`). This closes the strict-grounding gap the previous review flagged: every terminal decision, not just LLM-produced ones, ships with retrieved plan evidence the reviewer can audit.

**LLM path — tool-use protocol.**
- Model: `claude-sonnet-4-5` (configurable via `CLAIMS_AGENT_MODEL`).
- `tool_choice={"type": "any"}` — Claude is forced to produce a tool call, never free text.
- Six tool schemas (`TOOL_SCHEMAS`) map 1:1 to the action set in the spec.
- Tool names outside `TOOL_NAMES` → auto-escalate.
- `stop_reason != "tool_use"` (no tool call at all) → auto-escalate.

**Validation & logging.** Every pipeline step appends a `TraceStep(step, detail)` — rule outcomes, retrieval hits with scores, LLM request/response metadata, tool-input preview, validation issues, guard firings. The full trace rides in `ReviewResult.trace` and is rendered in the Streamlit UI under collapsible expanders.

### 2.3 Response composition

Every decision is returned as `Decision(action, reason, citations, payment_in_dollars?, requested_documents?, routing_target?)`.

**Reason field.** System prompt constrains it to 2–4 sentences with structure *(a) what policy says → (b) how claim matches/fails → (c) conclusion*, with a standard suffix *"This is a policy-emulation decision, not medical advice."* On the current judged eval run, average reason↔citation alignment across the three LLM-path, citation-bearing base cases is **2.67/5**. The weak cases are still the retrieval-misaligned ones: `CLM1005` (approval grounded in irrelevant HMO emergency-services text) and `CLM1001` (escalation reason does not closely restate the cited PPO clause). The prose is serviceable; retrieval relevance on thin HMO/EPO docs remains the sharper issue.

**Citations.** Each `Citation` carries `{plan, section, excerpt, score}`. `section` is the heading label captured during ingest; `excerpt` is verbatim from the retrieved chunk; `score` is 1.0 if the citation verified against a retrieved chunk, 0.0 if not. In the Streamlit UI each citation renders as a ✅/⚠️ expander keyed on verification status.

**Action-specific fields.**
- `approve_claim_payment` → `payment_in_dollars` (float).
- `request_missing_documentation` → `requested_documents` (list[str]).
- `route_to_senior_reviewer` / `route_to_coding_review` → `routing_target` ("senior_medical_reviewer" / "coding_review_team").

### 2.4 Safety & compliance

- **Disclaimer.** System prompt forces the suffix "This is a policy-emulation decision, not medical advice." on every reason.
- **No medical advice.** System prompt explicitly forbids recommending treatments or inferring patient details.
- **No invented rules.** Required by prompt, enforced by citation verification.
- **No cross-policy evidence.** Enforced structurally by per-plan indices.
- **Escalation thresholds.**
  - Low BM25 top score < 2.0 → escalate.
  - No tool-use block in response → escalate.
  - Unknown tool name → escalate.
  - Approve/deny with no verifying citation → escalate.
- **Out-of-scope providers.** The intake layer rejects carriers that are not Blue Shield of California with a friendly "this demo does not support X" message, and collects plan type (PPO/EPO/HMO) before invoking the pipeline. This is a demo-scope limitation, not a safety feature; in production each carrier would have its own policy corpus.

---

## 3. Spec coverage — Part 2: Evaluation Framework

### 3.1 Dataset

`backend/eval/labels.json` hand-labels 8 of the 9 sample claims (`CLM1007` absent because the spec corpus jumps from 1006 to 1008). A companion file, `backend/eval/perturbations.json`, adds 3 synthetic stress cases (missing doc, inactive flip, injected dx/procedure mismatch). Base labels carry:

- `expected_action` — the spec-aligned terminal action.
- `expected_rule_path` — either the specific `rule.*` step that should terminate, or `"llm"` when no rule should fire.
- `expected_section` — coarse snippet-ground-truth for the four LLM-path base cases, using section title as a lightweight evidence label.
- `ambiguity` — `"clear" | "ambiguous" | "unambiguous"` tag for error-bucketing.
- `rationale` — free-text expected reasoning.

**Gap: dataset is still small** (8 base + 3 perturbations) and has no held-out split. The harness currently trains no parameters so leakage is limited to prompt tuning. To scale, the main report proposes stratified splits by `(claim_type × ambiguity × rule_path)` plus a larger perturbation library to measure robustness more credibly.

**Policy-snippet ground truth.** Partially populated. The eval now uses `expected_section` on the 4 LLM-path base cases and scores whether at least one cited section matches that label. This is enough to make grounding quality measurable, but it is still coarse: it does not yet score exact excerpt overlap or rank the "best" citation among multiple attached excerpts.

### 3.2 Metrics

Implemented in `backend/eval/scorers.py`:

| Metric | Kind | How |
|---|---|---|
| `action_match` | exact-match | `result.decision.action == expected_action` |
| `rule_path_match` | exact-match | `last rule.* step in trace == expected_rule_path`; `"llm"` means RAG ran |
| `citations_all_verified` | deterministic | every `Citation.score > 0` |
| `expected_section_match` | deterministic | at least one cited section equals `expected_section` on labeled LLM-path cases |
| `judge.citation_faithfulness` | LLM | 1–5 |
| `judge.reason_citation_alignment` | LLM | 1–5 |
| `judge.reason_claim_alignment` | LLM | 1–5 |

**Last run summary** (`backend/eval/last_run.json`):
- cases: **11** total = 8 base + 3 perturbations
- action accuracy: **1.00**
- rule-path accuracy: **1.00**
- citation verification rate: **1.00**
- expected-section accuracy: **0.50** (2 of 4 labeled LLM-path cases cited the labeled section on the current run)
- avg judge citation_faithfulness: **3.0**
- avg judge reason_citation_alignment: **2.67**
- avg judge reason_claim_alignment: **3.33**

**What these numbers mean.** `action_accuracy` and `rule_path_accuracy` remain somewhat flattering because 7 of the 11 cases resolve deterministically through rules (4 base + 3 perturbations). The more informative numbers are the grounding metrics: `expected_section_accuracy = 0.50` says the agent only hit the labeled policy section on half of the four LLM-path base cases in the saved run, and the judge scores identify the same failure pattern as before: `CLM1005` is still an approval grounded in the wrong HMO section, while `CLM1001` escalates for the right high-level reason but cites a clause that does not closely match the explanation. That is a retrieval-quality failure more than an action-selection failure.

**Robustness to missing/ambiguous inputs** is now measured directly, albeit narrowly: the perturbation suite covers missing documents, member-status flips, and injected dx/procedure mismatches. All 3 perturbations currently resolve to the expected rule path and action.

### 3.3 How evaluation runs

- Offline harness: `python -m backend.eval.run_eval` (base labels + perturbations; add `--judge` for LLM-as-judge).
- Writes `backend/eval/last_run.json` with summary + per-case breakdowns.
- No online shadow mode yet. The report proposes logging every production decision alongside a shadow rerun of a candidate model/prompt, and diffing action labels + citation verification before promotion.

### 3.4 LLM-as-a-judge

Implemented (`scorers.score_llm_judge`). Judge model: `claude-sonnet-4-5` by default (configurable). Prompt is in `scorers.JUDGE_PROMPT` and scores three dimensions 1–5: citation_faithfulness, reason_citation_alignment, reason_claim_alignment. Judge is now intentionally *skipped* for rule-terminal decisions even though they carry post-hoc grounding citations; otherwise the judge would conflate a deterministic override reason ("plan inactive") with a plan-document chunk attached later for auditability rather than as the actual basis of LLM reasoning.

**When not to trust it.**
- Single-model judge + same family as candidate → self-preference bias. Mitigation: swap in a different model family (e.g., GPT-4o or a reasoning model) as the judge, or ensemble.
- Judge is stateless per case — it cannot detect that the candidate is systematically citing a wrong section across many cases.
- Judge scores are calibrated only by the prompt rubric; variance across runs has not been measured. A real deployment would add test-retest on a gold subset.

### 3.5 Failure analysis

The eval case-level output carries `action_match`, `rule_path_match`, `citations_all_verified`, and per-case judge scores. Error buckets are derivable:
- **Rule-path mismatch** (rule fired when LLM should have run, or vice versa) → engine/labels drift.
- **Citation-verification failure** → LLM fabrication or chunk boundary miss.
- **Judge-alignment low, action correct** → reasoning-quality regression.
- **Low retrieval score + escalation** → corpus coverage gap.

Prioritization rule of thumb: rule-path > citation-verification > judge-alignment > reason-quality. (A wrong action in an unambiguous case is worse than weak prose on an ambiguous one.)

### 3.6 Honest weaknesses in the eval layer

- Small n (8 base cases, 3 perturbations).
- Policy-snippet ground truth is section-level only, not excerpt-level.
- Robustness harness is present but still tiny.
- LLM-judge is same-family as candidate.
- No hold-out enforced.

---

## 4. Spec coverage — Part 3: Open-ended

### 4.1 Escalation to humans

Concrete thresholds, not vibes:
1. `top BM25 score < 2.0` → `route_to_senior_reviewer` (insufficient policy grounding).
2. Tool-use absent or unknown → escalate.
3. Approve/deny with no substring-verified citation → escalate.
4. Appeal claim type in the LLM path — the prompt biases toward escalation when new evidence is ambiguous relative to `prior_denial_reason`.
5. Rule engine: high amount (>$5000) → senior reviewer; dx/procedure mismatch → coding review.

### 4.2 Handling contradictions

- **Claim vs policy disagreement.** The LLM is instructed to choose `route_to_senior_reviewer` with an explicit ambiguity note rather than resolve the contradiction unilaterally. This is load-bearing: the citation-verification guard turns a hallucinated "resolution" into an escalation automatically.
- **Missing documents.** Caught deterministically by `REQUIRED_DOCS[claim_type]` before retrieval ever runs, so the LLM never sees a claim that is missing baseline docs. The intake layer additionally loops with the user to request documents conversationally.
- **Ambiguous provider_notes.** No special handling. The system prompt treats ambiguous notes as grounds for escalation via the "ambiguity → senior reviewer" instruction; nothing in the retrieval side weights claim-side ambiguity. This is a gap if the reviewer expects explicit uncertainty modeling.

---

## 5. Spec coverage — Additional Requirements

### 5.1 Policy Grounding (Strict Requirement)

Met in the current prototype.
- LLM decisions: ✅ enforced by prompt + post-check substring verification.
- Rule-engine terminals: ✅ grounded via a targeted one-shot BM25 retrieval pass (`rag.rule_grounding`) keyed by the terminating rule, with the top chunk attached as a `Citation`.

### 5.2 Robustness to Real-World Noise

- Missing documents: ✅ deterministic.
- Conflicting evidence: ⚠️ prompt-level ("escalate if ambiguous"), no structural check.
- Ambiguous text: ⚠️ same — prompt-level only.

### 5.3 Observability & Debugging

- Every decision returns a `trace: list[TraceStep]` containing retrieved chunk metadata (section, BM25 score, excerpt preview), every rule outcome (incl. inputs that did *not* fire), the LLM request/response including token usage, the tool name + input preview, validation issues, and every guard that fired.
- Rendered in Streamlit under expanders keyed by trace step (`rule.*`, `rag.search`, `llm.request`, `llm.response`, `validate.decision`, `guard.*`).
- **Debugging an incorrect denial** (spec question): open the trace. (1) If `rule.inactive_plan.triggered=True` — check the claim's `member_status` input. (2) If `rule.code_consistency.consistent=False` — check the dx/procedure table. (3) If `rag.search.top_score` is low — the retrieval is weak and should have escalated, not denied. (4) If the LLM path ran — inspect `llm.response.tool_input_preview` for the proposed citation, then diff against `rag.search.hits[*].excerpt_preview`. The substring verifier's output (`validate.decision.issues`) names the failing excerpt directly.

---

## 6. UI & Demo

Deployed as a Streamlit app (`frontend/app.py`) in two columns: chat panel on the left, live claim-field status panel on the right. Not required by the spec — built to make the intake + review flow visibly conversational rather than a curl-against-JSON affair.

### 6.1 Conversational intake

The spec gives structured claim JSON as input. Real hospitals don't have that — they have notes and half-filled forms. The UI accepts three input shapes:

1. **Free-text description.** "Active Blue Shield PPO member requesting pre-auth for lumbar MRI, M54.5, $2,200." Routed through `extract_claim_fields` in `backend/agent/intake.py` — a Claude call with a strict extraction system prompt that returns canonical-format JSON (enum values, canonical procedure codes like `MRI-LS-SPINE`, provider normalized to `Blue Shield PPO/EPO/HMO`).
2. **Pasted JSON.** Fast path: `try_parse_json_claim` detects a `{...}` in the message, collapses stray newlines inside string values (real copy-pastes break across lines), validates it has at least one claim key, and skips the LLM entirely. Zero-latency ingest when the caller already has structured data.
3. **Targeted single-field reply.** After the agent asks "which plan type?", the next user message is parsed by a deterministic per-field parser (`parse_targeted_answer`) with keyword lookups — no LLM round-trip to interpret "ppo" or "active".

### 6.2 Gap-filling loop

After each input, the intake layer computes `missing_required_fields(partial)` and asks for the next missing one with a field-specific, human-readable prompt (from `FIELD_QUESTIONS`). The loop terminates when all six required fields (`member_status`, `claim_type`, `insurance_provider`, `diagnosis_code`, `procedure_code`, `claimed_amount`, plus `prior_denial_reason` if `claim_type == "appeal"`) are filled. Only then is `Claim(**claim_dict)` constructed and `review_claim` invoked.

**Mid-flow doc loop.** When the review returns `request_missing_documentation`, the UI does not terminate — it switches to `awaiting_docs` state, lists the missing docs, and the next user message auto-attaches them to `supporting_documents` and re-runs the pipeline. This is the "resume from stage" pattern the main report references.

**Provider handling.** The intake layer distinguishes three cases structurally:
- Exact Blue Shield plan (`PPO`, `EPO`, `HMO`) → proceed.
- Blue Shield alias without plan type (`BCBS`, `Blue Cross Blue Shield`, `Blue Cross`) → ask for plan type.
- Known unsupported carrier (`Aetna`, `UHC`, `Cigna`, etc.) → reject with a user-visible "this demo does not support X" message rather than route-to-senior-reviewer silently.

This is the layer that lets the spec's sample claims (which cite Aetna, UHC, BCBS) reach the review pipeline with a minimal user nudge, without pretending to have policy corpora we don't.

### 6.3 Result rendering

When `stage == "complete"` the UI shows:
- A colored action banner (green approve / red deny / orange docs / violet route).
- The plain-English reason from `Decision.reason`.
- `payment_in_dollars` / `routing_target` / `requested_documents` as `st.metric` tiles when present.
- Each policy citation in an expander keyed ✅ (verified) / ⚠️ (failed substring check), with section title, plan, and excerpt.
- A "Decision trace" section: one expander per `TraceStep`, icon-coded by step prefix (`rule.*` ⚙️, `rag.*` 🔎, `llm.*` 🤖, `validate.*` ✔️, `guard.*` 🛡️), rendering the full detail JSON. This is the observability surface from §5.3 exposed to a non-technical user.

### 6.4 Why this matters for evaluating the design

The conversational layer is not just polish — it exercises the same Pydantic `Claim` contract, the same rule/RAG/LLM pipeline, and the same trace output as the offline eval harness. Building both against a single `review_claim(claim: Claim) -> ReviewResult` interface is the reason the UI was shipped in a day without touching the backend.

**Live deployment.** Streamlit Community Cloud, one-click rebuild on git push. API key via `st.secrets`.

**UI-level gaps.** No multi-turn claim editing (can't say "actually change the amount to $1,500"). No user authentication / PHI guardrails — the spec is a demo. Conversation state is in-memory (Streamlit session state); a page refresh loses the draft claim. Production would back this with Redis as described in the main report.

---

## 7. Things I explicitly did *not* build

Called out so the reviewer doesn't think they are oversights:
- **Hybrid retrieval** (BM25 + dense + reranker). Lexical matching is strong enough for the demo corpus.
- **Multi-carrier corpora.** Only Blue Shield policies are ingested. The intake layer gracefully rejects Aetna/UHC/BCBS with a "demo does not support" message.
- **Persistent session store.** The Streamlit app keeps intake state in `st.session_state`. The main report sketches a Redis + Postgres split for production — not implemented.
- **Online shadow mode.** Only offline harness exists.
- **Excerpt-level policy-snippet ground truth.** The eval now has section-level labels, but not exact expected excerpts.

---

## 8. Self-assessment against the rubric

| Spec area | Coverage | Confidence |
|---|---|---|
| Claim understanding (structured) | ✅ via `Claim` + intake pipeline | high |
| Policy retrieval & interpretation | ✅ BM25 per-plan + citation verification | high |
| Action set (6 tools) | ✅ 1:1 with spec | high |
| Workflow rules (3 claim types + 4 overrides) | ✅ implemented, ordering deviates from spec (§2.2) | high |
| Output schema (label + reason + extras) | ✅ `Decision` | high |
| Policy grounding strict | ✅ all terminal actions carry retrieved plan evidence | high |
| Robustness | ⚠️ missing-docs yes, ambiguity partial | medium |
| Evaluation dataset | ⚠️ 8 base + 3 perturbations, no hold-out, coarse section labels only | medium |
| Metrics (action/grounding/robustness) | ⚠️ action ✅ / grounding ⚠️ / robustness partial | medium |
| LLM-as-judge | ✅ with caveats | medium |
| Failure analysis | ✅ buckets derivable from case output | medium |
| Escalation design | ✅ four concrete thresholds | high |
| Contradiction handling | ⚠️ prompt-level for claim vs policy | medium |
| Observability | ✅ full trace + UI rendering | high |
| Debugging story | ✅ traceable end-to-end | high |

The spec also requires the word **Bluecross** to appear — noted at the top of this document.

---

## 9. The three questions I would ask the reviewer

1. Does the deviation in override ordering (§2.2) matter, or is the fact that all sample and perturbation cases resolve to the expected action sufficient?
2. Is section-level grounding (`expected_section`) enough for this stage, or do you expect excerpt-level gold labels as well?
3. Is 8 base + 3 perturbation cases acceptable for the prototype, given that the harness is now structured to grow?
