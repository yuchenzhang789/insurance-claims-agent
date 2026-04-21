# Insurance Claims Review Agent Design Report

## Part 1: Agent Design

### 1.1 System Overview

The system takes structured claim data plus the correct insurer policy documents and returns one action, a short explanation, and supporting policy evidence. The core design goal is simple: automate the safe cases, and escalate when evidence is weak, missing, or conflicting.

At a high level, the pipeline is:

`claim input → input guardrails → deterministic override checks → policy retrieval → decision generation → policy / action guardrails → output guardrails → final action + trace`

![High-level workflow](./diagrams/workflow-overview.png)

This separates four jobs that behave differently in production: input safety, hard workflow rules, policy interpretation, and output validation. Clear rules such as inactive membership or missing required documents are handled in code. Open-ended cases, such as whether the policy supports a service, go through retrieval and model reasoning. The result is more structured than an “LLM does everything” design, but much easier to audit and debug.

### 1.1.1 Model Strategy

Not every step needs the same model. In production, I would use a lightweight model or extraction service for intake normalization, a main decision model after retrieval, and optionally a stronger model for complex appeals or low-confidence cases. For the prototype, one main model is enough. Regardless of routing, the system should log model version, prompt version, retrieval version, and rule-set version in every trace so changes can be audited and rolled back safely.

### 1.1.2 Storage and Serving Architecture

For production, I would split the system into a small number of focused services rather than one monolith. The point is not microservice complexity; it is to separate components that scale and change differently.

| Component | Role |
|---|---|
| API / orchestration service | validates input and coordinates the workflow |
| Policy registry and document store | stores source policy documents and versions |
| Retrieval index | stores chunked policy corpus for plan-scoped search |
| Decision service | runs model reasoning and validation |
| Trace and audit store | stores decision traces and system metadata |
| Evaluation and rollout pipeline | runs offline and online validation before release |

This split reflects how the system actually changes over time: policy documents update on their own cadence, retrieval indexes rebuild on document changes, online review traffic scales with claim volume, and traces and evaluation artifacts have different storage and retention needs than the serving path.

### 1.2 Retrieval and Evidence Grounding

Policy retrieval is plan-scoped by design. Each insurer and plan gets its own indexed corpus, and every claim is hard-filtered to the correct corpus before ranking. That prevents one of the worst failure modes in this workflow: returning a plausible answer grounded in the wrong policy.

Documents are parsed into chunks that preserve section labels and text spans. At inference time, retrieval queries are built from the claim type, diagnosis code, procedure code, provider notes, and prior denial reason when relevant. For the prototype, BM25 is sufficient because the policy corpus is small and the vocabulary is often lexically distinctive. In production, I would extend this to hybrid retrieval with reranking.

Grounding is structural, not prompt-only. Final decisions should carry citations from retrieved policy text, and the cited excerpt should be validated against the retrieved chunks before the decision is accepted. This is what turns “do not invent business rules” into an enforceable system property.

### 1.2.1 Data Quality and Policy Freshness

The retrieval system is only as reliable as the policy corpus it reads from. For production, policy documents should be versioned by insurer, plan, and effective date; ingestion should validate parse quality and chunk coverage; failed parses should be quarantined for review; and new policy versions should trigger re-indexing and regression evaluation before promotion.

### 1.3 Decision Workflow and Tool Selection

This is the core of the system: how claims move from input to final action.

#### 1.3.1 General workflow for all claims

At a high level, every claim follows the same pipeline:

`claim input → input validation and guardrails → deterministic override checks → policy retrieval → model decision → policy / action guardrails → output guardrails → final action + trace`

![General workflow steps](./diagrams/general-workflow-steps.png)

The control flow stays the same across claim types; only the reasoning emphasis changes.

1. **Input validation and guardrails.** Validate schema, supported insurer / plan, and basic sanity checks before review starts. This is deterministic and cheap, so there is no reason to spend an LLM call deciding whether the input is even processable.
2. **Deterministic overrides.** Handle the four spec-defined global overrides in code: inactive plan, missing essential documents, high claim amount, and diagnosis / procedure mismatch. I chose code rather than prompt logic here because these are workflow rules, not language-understanding problems. The tradeoff is rigidity, but that is acceptable for explicit rules.
3. **Policy retrieval.** If no override fires, build a retrieval query from the claim fields and search only the correct plan corpus. The query should be assembled from diagnosis, procedure, claim type, provider notes, and prior denial reason when relevant. I prefer retrieving before the model, not as a model-invoked tool, because the same retrieved evidence then becomes the fixed grounding surface for validation.
4. **Model decision.** Pass the claim plus retrieved evidence to the model and force one tool call from the fixed action set. Tool calling is a deliberate choice: it makes the output structured, constrains the action space, and avoids having to interpret free-form prose after the fact. The tradeoff is less flexibility, but in a claims workflow parseability matters more than stylistic freedom.
5. **Policy / action guardrails.** Validate action type, claim-type compatibility, citations, policy source, and structured fields such as payment amount. This layer exists because even a good model can return an invalid or weakly grounded action. In production, I would also treat confidence as a composite signal here, combining retrieval strength, citation validity, guardrail outcomes, and case risk.
6. **Output guardrails.** Enforce the disclaimer, block unsupported medical advice, and downgrade unsafe or unsupported output. This should be the final safety layer, not a second reasoning engine.
7. **Final action and trace.** Return the action, explanation, citations, and a trace of rules, retrieval, validation, and guardrail outcomes. The trace is not an optional debugging extra; it is what makes wrong denials and unnecessary escalations diagnosable.

An incorrect denial should be debugged by walking this trace backwards: check whether a rule fired, whether retrieval surfaced the right evidence, whether the cited policy text actually supports the action, and whether any guardrail changed the result.

#### 1.3.2 Pre-Approval workflow

The pre-approval path emphasizes coverage criteria and medical necessity.

![Claim-type workflow](./diagrams/claim-type-workflow.png)

Retrieval focuses on diagnosis, procedure, provider notes, and pre-approval language. The model then chooses among approval, denial, missing-document request, or escalation. I would not try to encode medical-necessity criteria entirely as rules, because these criteria are usually written in natural language and vary by service. The tradeoff is higher dependence on retrieval quality, which is why low-confidence retrieval should escalate rather than force automation.

#### 1.3.3 Standard Claim workflow

The standard-claim path puts more weight on coverage validation and code consistency. Diagnosis / procedure mismatch is checked early by the rule layer because it is a hard workflow gate. If that passes, retrieval focuses on coverage and exclusion language, and the model chooses approval, denial, or escalation. If approved, `payment_in_dollars` is returned as a structured field so it can be validated directly instead of being buried in prose. This split keeps hard coding rules explicit while leaving coverage interpretation in the retrieval-plus-model layer.

#### 1.3.4 Denial Appeal workflow

Appeals use the same pipeline, but retrieval and prompting both incorporate `prior_denial_reason`. The model is asked to judge whether the new documentation is new, sufficient, and relevant to the original denial. This is the weakest part of the current design structurally, because the comparison is still mostly prompt-driven. In production, I would add a dedicated appeal-comparison module to compare four things explicitly: the prior denial reason, the prior policy basis, the new evidence, and whether that evidence actually overcomes the denial. The tradeoff is more workflow complexity, but appeals are one of the few places where extra structure is worth it.

### 1.4 Response Composition

Each output should contain four things: a primary action label, a short plain-English reason, supporting citations, and any next-step fields such as payment amount, requested documents, or routing target. The reason should be concise and follow a simple pattern: what the policy says, how the claim matches or fails it, and what the system is doing next. This is intentionally more structured than open-ended prose because operators need clarity and downstream systems need parseable fields.

### 1.5 Safety and Compliance Boundaries

The system must not invent business rules, give medical advice, cite the wrong insurer’s policy, or return an automatic decision when evidence is weak, missing, or contradictory. These boundaries should be enforced across the pipeline, not left to a single prompt. Input guardrails block unsupported or malformed claims; deterministic overrides enforce hard rules; retrieval scoping and citation validation enforce grounding; and output guardrails block unsafe final responses.

The main escalation triggers are low retrieval confidence, conflicting evidence, failed citation validation, high-risk cases, and appeals with weak new evidence. The system should prefer safe escalation over brittle automation. Over-escalation creates reviewer work; under-escalation creates grounded-looking wrong answers. The same layered design also supports debugging: the system should log retrieved chunks and scores, evaluated rules, selected tool calls, and final validation outcomes. These traces should be PHI-aware in production.

### 1.6 Production Notes and Prototype Scope

The prototype is intentionally simpler than the full production design. In the current implementation, the policy corpus is limited to Blue Shield of California PPO, EPO, and HMO documents, retrieval is BM25-only, and the UI focuses on demonstrating the end-to-end workflow rather than full production operations. That is acceptable as long as the report is explicit about what is implemented now and what would be added later.

The production version would add broader carrier coverage, stronger retrieval and reranking, durable audit logs, reviewer feedback loops, rollout controls such as shadow mode and canaries, and caching for frequent plan / procedure patterns. I would still keep the core system as a single decision agent inside a structured pipeline. If scope later grows, some independent subtasks could be parallelized, but I would treat multi-agent patterns as a scaling optimization rather than the default design.

## Part 2: Evaluation Framework

The goal of evaluation is not only to check whether the agent gets the final action right. It also needs to show that decisions are policy-grounded, that the system behaves safely when inputs are incomplete or conflicting, and that regressions can be caught before deployment.

### 2.1 Evaluation Dataset

I would build the evaluation set as a labeled corpus of claims, policy evidence, and expected outcomes. Each case would contain:
- the structured claim input
- the correct insurer / plan
- the expected action
- one or more acceptable policy excerpts from the correct document
- tags such as claim type, ambiguity level, missing-document condition, or contradiction type

The dataset would come from three sources: a hand-labeled seed set from the sample claims and policy PDFs, a robustness set created by perturbing those seed cases, and a reviewer-feedback set built from production disagreements or overrides.

The sample claims reference insurers including Aetna, UHC, and Bluecross. In the current prototype, those scenarios are remapped into a Blue Shield of California corpus so the workflow can run end to end. In production, each carrier would have its own indexed corpus.

Useful perturbations include:
- remove a required document
- flip `member_status` from active to inactive
- inject a diagnosis / procedure mismatch
- replace clear provider notes with vague or low-quality text
- add provider notes that conflict with the policy evidence

To reduce leakage, I would keep three splits:
- `development set` for prompt and threshold tuning
- `validation set` for model and configuration selection
- `held-out set` for final evaluation only

This matters because a claims agent can look stronger than it really is if it is tuned on the same small set it is later scored on.

### 2.2 Metrics

I would track three headline metrics: **action accuracy**, **grounding quality**, and **robustness on perturbed cases**. Action accuracy measures whether the predicted action matches the expected action. Grounding quality measures whether the evidence is the right evidence; in production I would score citation precision and recall against gold spans, while in the current prototype this is approximated with section-level matching such as `expected_section`. Robustness measures whether the system behaves safely when inputs are missing, conflicting, or ambiguous.

Supporting diagnostics include an action confusion matrix, over- and under-escalation rates, and latency / cost per reviewed claim.

### 2.3 How Evaluation Runs

I would run evaluation through the full agent pipeline, not a separate mock path. For each labeled case, the harness would:
1. load the structured claim and the correct policy corpus
2. run the full review pipeline end to end
3. collect the final action, citations, and trace
4. compare the action against the gold label
5. compare the citations against the gold policy excerpts
6. score the case under the metrics above
7. aggregate results by slice, such as claim type, insurer / plan, override condition, ambiguity level, and perturbation type

The harness should produce both a machine-readable JSON report and a human-readable report. Case-level outputs should preserve enough trace information to support debugging: at minimum the retrieved chunks and scores, the selected tool call, and the validation outcome.

### 2.4 LLM-as-a-Judge

I would use LLM-as-a-judge only as a secondary tool. The main metrics should be scored deterministically whenever possible. The judge is useful for checking explanation quality. It would receive:
- the structured claim
- the policy excerpts shown to the agent
- the agent’s action
- the agent’s explanation
- the agent’s citations

It would then score questions like:
- do the cited excerpts actually appear in the retrieved evidence
- does the explanation accurately restate what the policy says
- does the explanation connect the claim facts to the cited policy correctly

This is helpful for explanation quality, but it should not be treated as the source of truth for final correctness or as the only gate for release.

### 2.5 Online Evaluation and Safe Rollout

Offline evaluation is necessary, but not enough. Real traffic will contain edge cases that a static benchmark misses, so I would roll out changes in stages:
- `shadow mode`: run the new system in parallel without affecting real decisions
- `canary release`: send a small amount of low-risk traffic to the new system
- `A/B testing`: compare the candidate and the baseline on action quality, grounding quality, escalation behavior, latency, and cost
- `progressive rollout`: increase traffic only if the new system stays safe and stable

Claims errors are not symmetric. A small drop in grounding quality or a rise in under-escalation is more serious than a small latency increase, so rollout should optimize for safety first.

### 2.6 Failure Analysis and Fix Prioritization

Evaluation should not stop at a score. It should tell us what to fix next. I would bucket failures into four groups:
- `action error`: wrong next action
- `grounding error`: missing, wrong, or irrelevant citation
- `robustness error`: failure under missing, conflicting, or ambiguous inputs
- `escalation error`: escalated when it should have acted, or acted when it should have escalated

I would prioritize fixes in this order:
1. `action error`, because wrong approvals or denials are the most serious
2. `grounding error`, because unsupported decisions violate the spec and reduce trust
3. `escalation error`, because this controls how safely the system handles uncertainty
4. `robustness error`, because these failures show where the system needs more data or better retrieval and prompting

This keeps the system focused first on safety and correctness, then on coverage and smoother automation.

## Part 3: Open-Ended Discussion

### 3.1 Escalation to Humans

Escalation is not a failure mode in this system. It is a required safety mechanism. The agent should route a claim to a real specialist whenever the case is too risky, too uncertain, or too contradictory to support a reliable automated decision.

There are three main escalation signals.

| Signal type | Examples |
|---|---|
| Risk | high-value claim, complex appeal, higher compliance impact |
| Uncertainty | weak retrieval, vague notes, ungrounded action |
| Conflict | claim facts disagree with policy evidence, policy evidence is internally inconsistent |

Risk means cases that deserve human review even when the evidence is not obviously wrong, such as high-value claims or complex appeals. Uncertainty means the evidence is too weak to support a grounded action, such as low retrieval confidence or vague notes. Conflict means the claim facts and policy evidence disagree, or the evidence itself is internally inconsistent. These triggers should be implemented as workflow rules, not just as model suggestions.

### 3.2 Handling Contradictions

The system should treat contradictions as first-class workflow events. When claim data and policy evidence disagree, the system should surface the conflict and escalate rather than force a brittle approve/deny decision. When documentation is missing, low quality, or vague, the system should either request clarification or escalate depending on whether the gap is specific and recoverable.

In production, I would make this behavior explicit:
- if the missing information is known and specific, request documentation
- if the evidence is present but contradictory or too ambiguous, escalate
- if a final decision cannot be grounded in the retrieved policy text, escalate rather than guess

The goal is not to maximize automation at all costs. The goal is to automate the safe cases, ask for more information when the gap is clear, and escalate cases where real human judgment is still needed.

## Part 4: Prototype and Demo

### 4.1 Why the prototype exists

The first three parts of this report describe the system I would build in production. The prototype serves a different purpose. It is not meant to prove production readiness, broad insurer coverage, or enterprise-scale reliability. It is meant to show that the main design choices in this report can be combined into a working end-to-end system.

That distinction matters. A design document can describe a rule engine, plan-scoped retrieval, constrained tool calls, and trace-based debugging, but it does not prove that these pieces actually work together. The prototype does. It gives the reviewer something concrete to inspect: a claim goes in, the pipeline runs, a single structured action comes out, and the decision is accompanied by citations, confidence, and a trace.

The prototype should therefore be read as an architectural proof of concept. It is the smallest implementation that demonstrates the core system shape in a way that a reviewer or interviewer can actually exercise.

### 4.2 What the prototype implements

The current prototype implements the main control points from the production design.

- a conversational intake flow in Streamlit, with support for free-text claim descriptions and structured JSON input
- deterministic override checks for inactive member status, missing required documents, diagnosis / procedure mismatch, and high-amount routing
- plan-scoped retrieval over a Blue Shield of California policy corpus
- model-based action selection through a fixed tool set
- citation verification against retrieved policy text
- input and output guardrails
- structured traces, displayed directly in the UI
- a compact confidence and decision-summary layer to make the pipeline easier to understand during a demo

This is enough to demonstrate the most important architectural claim in the report: the system is not just a chat interface around an LLM. It is a structured pipeline in which deterministic checks, retrieval, model reasoning, validation, and escalation each play a different role.

### 4.3 What the demo is meant to prove

The demo is meant to make four design claims visible.

First, deterministic rules really do short-circuit the model when they should. If a member is inactive, if required documents are missing, or if the claim amount exceeds the review threshold, the system does not waste a model call trying to “reason” about a case that the workflow has already resolved.

Second, retrieval is scoped to the correct plan. The model is not searching across mixed insurer documents. The prototype uses a per-plan retrieval namespace so that the evidence shown in the UI comes only from the relevant policy corpus.

Third, the model returns structured actions rather than free-form prose. The prototype forces the decision through a fixed action set, which makes the output easier to validate, log, and debug.

Fourth, decisions are inspectable. The point of the citations, confidence summary, and trace panel is not just UI polish. Together they make it possible to answer the most important reviewer question: why did the system take this action, and what evidence supported it?

These are the behaviors I want the interviewer to notice. The prototype is successful if it makes those properties easy to see.

### 4.4 Scope limits of the prototype

The prototype is intentionally narrower than the production design.

- It only supports Blue Shield of California plans: PPO, EPO, and HMO.
- It uses a small static policy corpus rather than a versioned document store.
- It uses BM25 retrieval only; there is no dense retrieval or reranker.
- It uses a single-model setup rather than model routing.
- It uses in-memory UI session state rather than durable production storage.
- It does not include authentication, PHI-safe infrastructure, or production compliance controls.

These are not accidental omissions. They are scope choices made to keep the prototype focused on demonstrating the system shape. In other words, the prototype is designed to answer “does this architecture hang together?” rather than “is this ready to deploy at enterprise scale?”

### 4.5 How to read the demo

I would encourage the interviewer to look at the demo in three layers.

The first layer is the final action itself: approve, deny, request documentation, route to coding review, or escalate to a senior reviewer.

The second layer is the decision summary: which path the pipeline took, what the main reason was, and which policy section mattered most.

The third layer is the trace. This is where the architecture becomes visible. The trace shows whether the rule engine fired, which policy chunks were retrieved, whether the model was called, what validation ran afterward, and whether any guardrail changed the outcome.

That is the main reason the UI exists. It is not there to look like a finished product. It is there to make the architecture legible.

### 4.6 Recommended demo flow

If I were walking an interviewer through the prototype, I would use three scenarios.

The first is an inactive-member claim. This demonstrates the deterministic rule path. The reviewer can see that the system denies the claim quickly, attaches policy evidence, and never calls the model. This is the clearest demonstration that hard workflow rules are enforced structurally rather than left to prompt behavior.

The second is a pre-authorization approval scenario. This demonstrates the full retrieval-plus-model path. The reviewer can see the system extract claim details, retrieve plan-specific evidence, choose a structured action, and attach citations that can be inspected in the UI.

The third is an appeal scenario with new evidence. This demonstrates the most reasoning-heavy path in the current prototype. It shows that the system is not just matching obvious rules; it can also compare prior denial context, new evidence, and policy language before taking an action.

Together, these three scenarios cover the main story of the report: deterministic short-circuits where they should exist, retrieval-grounded reasoning where language matters, and escalation when the system should not guess.

### 4.7 Current prototype results

The prototype also includes a small offline evaluation harness. This is not large enough to establish broad reliability, but it is enough to show where the prototype is already working and where it is still weak.

On the latest run, the harness evaluated 11 cases in total: 8 base cases and 3 perturbations. The prototype achieved:

- `action_accuracy = 0.909`
- `rule_path_accuracy = 1.0`
- `citation_verification_rate = 1.0`
- `expected_section_accuracy = 0.75`

The main takeaway is that the deterministic paths are stable, and the system is consistently returning verifiable citations. The main remaining weakness is not structural failure of the pipeline; it is decision quality on the LLM-driven cases, where one base-case pre-authorization scenario was approved when the current labels expected escalation. This is exactly the kind of gap the production evaluation framework in Part 2 is meant to catch and improve.

### 4.8 What the interviewer should take away

The main thing I want the interviewer to understand is that the prototype is not separate from the report. It is the runnable companion to the report.

Parts 1 through 3 explain how I would design the full system. Part 4 shows that the key architectural ideas in those sections are concrete enough to implement and inspect: rules before LLMs, plan-scoped retrieval, structured action selection, grounding checks, and trace-based debugging.

So the prototype should not be judged as a full product. It should be judged on whether it demonstrates the right system behaviors clearly and honestly. I believe it does, and that is why I chose to include it alongside the production design.
