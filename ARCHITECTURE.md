# Architecture Decision Record

## System shape

```
┌─────────────────────────────────────────────────────────────────┐
│                      Streamlit UI (ui/)                          │
│   Claim picker │ Decision + citation │ Reasoning trace viewer    │
└──────────────────────────┬──────────────────────────────────────┘
                           │ HTTP (JSON)
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                   FastAPI backend (backend/api/)                 │
│                     POST /review_claim                           │
└──────────────────────────┬──────────────────────────────────────┘
                           ▼
              ┌────────────────────────┐
              │  1. Rule Engine        │  deterministic; runs first
              │  (overrides)           │
              └──────────┬─────────────┘
                         │ no hard override → continue
                         ▼
              ┌────────────────────────┐
              │  2. Policy RAG         │  per-plan namespaced index
              │  (retrieval)           │  → top-k chunks w/ section
              └──────────┬─────────────┘
                         ▼
              ┌────────────────────────┐
              │  3. Claude Agent       │  tool-calling: 6 actions
              │  (Sonnet 4.6)          │  + cite_policy validator
              └──────────┬─────────────┘
                         ▼
              ┌────────────────────────┐
              │  4. Output Validator   │  JSON schema + citation
              │                        │  span-in-retrieval check
              └──────────┬─────────────┘
                         ▼
              ┌────────────────────────┐
              │  5. Trace Logger       │  every step → JSON
              └────────────────────────┘
```

---

## Framework 1: Tool architecture — **Hybrid**

**Five most common "question patterns" the agent will face** (derived from sample claims + workflow spec):
1. Is this procedure covered under this plan? → retrieval + classification
2. Does the provider_notes meet medical-necessity criteria stated in the policy? → retrieval + judgment
3. Are required documents attached for this claim_type? → deterministic check
4. Is dx/proc code pair internally consistent? → deterministic lookup
5. Is the prior denial overturned by new appeal evidence? → retrieval + comparison

Patterns 3 + 4 are fully deterministic (never need LLM). Patterns 1, 2, 5 need policy interpretation. Clean split.

**Decision: Hybrid.**
- **Deterministic rule engine** runs *first* and owns the Special Conditions Overrides (inactive member, missing docs, >$5k, dx/proc mismatch). Non-negotiable; the LLM cannot override these.
- **LLM agent** is invoked only when the rule engine does not produce a terminal decision. The LLM chooses from the 6 tool calls.
- The rule engine can also *constrain* the LLM's available actions (e.g., if `claimed_amount > 5000` but other checks pass, the rule engine pre-routes to senior reviewer and skips the LLM — or alternatively allows the LLM to still attach reasoning but forces the action).

**Why not full LLM?** The Overrides are stated as imperatives ("must be denied", "do not approve"). Letting the LLM choose risks policy-violation failures that are easy to eliminate with 20 lines of Python.

**Why not full deterministic?** Medical-necessity judgment against unstructured policy text is the whole reason LLMs are useful here.

---

## Framework 2: Safety layers

Failure modes → layer mapping:

| Failure mode | Layer | Mechanism |
|---|---|---|
| LLM invents a business rule not in policy | Structural | Citation-span verifier: the cited excerpt must literally appear in one of the retrieved chunks. Fail → escalate. |
| LLM uses PPO policy for an EPO claim | Pre-retrieval | Per-plan namespaced index; retrieval query is hard-filtered by `claim.plan`. |
| LLM approves a $15k claim (override violation) | Pre-generation | Rule engine runs before LLM; forces `route_to_senior_reviewer` when `claimed_amount > 5000`. |
| LLM approves an inactive-member claim | Pre-generation | Rule engine terminal decision; LLM never called. |
| LLM hallucinates payment amount | Structural | `approve_claim_payment(payment_in_dollars)` argument must equal `claim.claimed_amount` or be validated against a retrieved policy amount. Mismatch → reject + re-prompt or escalate. |
| LLM gives medical advice | Prompt + post-generation | System prompt explicitly forbids medical advice; output guard regex checks for imperative clinical recommendations. |
| Low retrieval confidence (no good chunks) | Pre-generation | Retrieval top-1 score below threshold → force `route_to_senior_reviewer` with "insufficient policy coverage" reason. |
| Tool-call JSON invalid | Structural | JSON-schema validation on tool args via Anthropic SDK `tools` definitions. |
| Off-topic input (non-claim) | Pre-generation | Input schema validation — must match `Claim` pydantic model. |

**Principle:** pre-generation > structural > post-generation. Catch before the LLM whenever possible.

---

## Framework 3: Memory and context

**This agent is stateless per-claim** by design. Each claim is a fresh review. No cross-turn memory needed.

**What persists within a single review:**
- Retrieved policy chunks (kept in LLM context alongside the claim).
- Rule-engine verdicts (surfaced as "facts" in the system prompt).
- Reasoning trace (stored server-side, not re-fed to the LLM).

**What does not persist:** prior claims' decisions, user feedback, RAG results from other claims.

**Rationale:** the spec does not describe multi-turn usage. Adding conversation memory would be premature abstraction. If the eventual product adds "the hospital asks a follow-up", a thin session layer can wrap this — but out of scope today.

---

## Key technical choices

| Decision | Choice | Reason |
|---|---|---|
| LLM | Claude Sonnet 4.6 (`claude-sonnet-4-6`) | Strong tool-use + reasoning, right cost-perf point for this task. Opus 4.7 overkill. |
| Tool-calling | Anthropic SDK native tools | Schema-validated, first-class. |
| RAG index | In-memory (embeddings + chunks dict), keyed by `plan_id` | No infrastructure dependency; sample data is small. Pluggable to FAISS / Chroma / pgvector for scaled version. |
| Embedding model | `text-embedding-3-small` (OpenAI) or Voyage AI `voyage-3`; Anthropic has no hosted embedding API as of cutoff. | Cost-efficient, strong on retrieval benchmarks. |
| Chunking | Section-heading aware (regex on Blue Shield doc headings: `General exclusions and limitations`, `Principal Benefits`, etc.) with 400-token window + 80-token overlap. Page number + section preserved as metadata for citations. | Policy docs have natural section structure; semantic boundaries > naive char splits. |
| Retrieval | Hybrid: BM25 (rank_bm25) + dense (cosine) → reciprocal rank fusion → top 5. | Exclusion clauses use exact terminology ("Experimental or Investigational") that BM25 nails; semantic queries ("is lipid screening preventive?") need dense. |
| Prompt caching | Enabled on system prompt + tool definitions + per-plan policy index injection. | Claude API feature; system prompt is static, policy chunks per plan are stable → cache hits reduce cost and latency materially. |
| Observability | Structured JSON logs per request (rules triggered, retrieval scores, tool-call args, final action). Exposed to UI. | Spec explicitly asks "how would you debug an incorrect denial?" — need replayable traces. |

---

## MVP vs scaled

| Dimension | MVP (this submission) | Scaled production |
|---|---|---|
| Index | In-memory dict | pgvector or Pinecone, multi-tenant |
| Insurers | Blue Shield (PPO + EPO + HMO) | Real multi-insurer onboarding, policy ingestion pipeline |
| Code consistency | Hand-curated lookup | CMS NCCI edits / Optum EncoderPro API |
| Eval | Offline harness, 15–20 claims | Shadow mode on live traffic + weekly calibration set |
| HITL | None beyond "route to senior" action | Reviewer queue w/ feedback loop, active-learning sample selection |
| Policy versioning | Filename = version | Doc hash + effective-date index; retrieval filters by date of service |
| PII | Scrubbed from logs | De-id + encryption at rest + audit log |

---

## What I'm explicitly NOT doing

- Fine-tuning anything. Zero-shot Claude + RAG is sufficient for the demo and the spec.
- Agentic loops (multi-step ReAct with its own tool invocations during the decision). One retrieval → one decision is simpler, more predictable, and matches the spec's "single next action" framing.
- UI auth / user management.
- Docker / k8s. A README `make run` is enough.
