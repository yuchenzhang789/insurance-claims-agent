## Part 1: Agent Design

### 1.1 System Overview

The system takes two inputs: structured claim data and the correct insurer policy documents. It returns one action from the allowed action set, a short explanation, and supporting policy evidence. The design goal is simple: make policy-aligned decisions when the evidence is clear, and escalate safely when the evidence is weak, missing, or conflicting.

At a high level, the pipeline is:

`claim input → input guardrails → deterministic override checks → policy retrieval → decision generation → policy / action guardrails → output guardrails → final action + trace`

![High-level workflow](./diagrams/workflow-overview.png)

This structure separates input safety, rule-based decisions, policy interpretation, and final output validation. Hard workflow rules such as inactive membership or missing required documents are handled directly in code. More open-ended cases, such as whether a claim is supported by the policy language, go through retrieval and model-based reasoning. The guardrail layer makes sure only supported claims enter the pipeline and only valid, grounded actions leave it.

**How it is implemented**
- structured claim data is the main input contract
- policy documents are stored in a plan-scoped retrieval layer
- a single decision agent reasons over retrieved evidence and returns one action through tool calling
- deterministic modules around the model handle validation, overrides, and output safety

**Why this design**
- the system still gets the flexibility of model-based policy reasoning where language matters
- at the same time, it avoids pushing hard workflow rules and safety checks into the prompt alone
- this makes the design easier to audit, easier to debug, and safer than an LLM-only architecture

**Tradeoff**
- this pipeline is more structured than a simple “send everything to one model” design
- the added structure is worth it because claims decisions need stronger control, traceability, and safety than a generic chat workflow

### 1.1.1 Model Strategy

The report assumes one main decision model, but not every step needs the same level of capability. In production, I would use a simple routing strategy rather than sending everything to the same model configuration.

**How it is implemented**
- a lightweight model or extraction service can handle intake normalization when the claim arrives as free text
- the main decision model handles policy-grounded action selection after retrieval
- complex appeals or low-confidence cases can be routed to a stronger model before the system falls back to human escalation

**Why this design**
- not every step needs the cost and latency of the strongest model
- routing lets the system reserve more expensive model capacity for the cases where deeper reasoning is actually needed
- this keeps the architecture simple while still giving room for production optimization

**Tradeoff**
- model routing adds operational complexity because different models may behave differently
- for a prototype, a single main model is the simplest choice; production routing is an optimization layer, not a requirement for the base design

In production, model and prompt configurations should also be versioned explicitly. Each decision trace should record the model version, prompt version, retrieval configuration, and rule-set version used for that claim. This makes it possible to audit behavior, compare releases, and roll back safely if a prompt or model update degrades decision quality.

### 1.1.2 Storage and Serving Architecture

For a production system, I would run this as a small set of focused services rather than one monolithic application. The goal is not to maximize microservice complexity. The goal is to separate the components that scale differently and store different kinds of data.

| Component | Role |
|---|---|
| API / orchestration service | validates input and coordinates the workflow |
| Policy registry and document store | stores source policy documents and versions |
| Retrieval index | stores chunked policy corpus for plan-scoped search |
| Decision service | runs model reasoning and validation |
| Trace and audit store | stores decision traces and system metadata |
| Evaluation and rollout pipeline | runs offline and online validation before release |

**API / orchestration service**
- receives claim requests
- runs input validation and workflow orchestration
- decides whether the case is resolved by deterministic rules or should continue to retrieval and model decisioning

**Policy registry and document store**
- stores the source policy documents, keyed by insurer, plan, and effective date
- tracks document versions so the system knows which policy corpus is active

**Retrieval index**
- stores the chunked policy corpus used at inference time
- supports plan-scoped retrieval so each claim only searches the correct insurer and plan

**Decision service**
- runs the main model call after retrieval
- applies tool calling, policy / action guardrails, and output guardrails before returning a final action

**Trace and audit store**
- stores the workflow trace for each reviewed claim
- records retrieved chunks, ranking scores, selected action, validation results, and system versions used for the decision

**Evaluation and rollout pipeline**
- runs offline evaluation, shadow mode analysis, and release checks for prompt, model, retrieval, and rule changes

This service split is useful because different parts of the system have different scaling patterns. Retrieval indexes and policy documents change on document-update cycles, while decision traffic scales with live claim volume. Traces and evaluation artifacts also have different storage and retention needs than the online decision path. Keeping these concerns separate makes the production system easier to scale, monitor, and update safely.

### 1.2 Retrieval and Evidence Grounding

Policy documents should be stored as a plan-scoped corpus, not as one mixed document pool. In production, I would maintain one index per insurer and plan, so every claim is matched only against the relevant policy set. During ingestion, each document would be parsed into chunks that preserve useful structure such as section title, section identifier if available, and text span. This makes it possible to retrieve evidence that is both machine-readable and reviewer-friendly.

At inference time, the retrieval query would be built from the key claim fields that matter for coverage reasoning: claim type, diagnosis code, procedure code, provider notes, and prior denial reason for appeals. Retrieval would always be restricted to the correct insurer and plan before any ranking happens. This is important because cross-policy evidence is one of the most serious failure modes in a claims workflow. A correct-looking answer supported by the wrong insurer’s policy is still a bad decision.

For ranking, I would start with lexical retrieval such as BM25 in the prototype because it is simple, fast, and works well when diagnosis and procedure codes appear directly in the policy. In production, I would extend this into a hybrid retrieval stack with lexical search, dense retrieval, and reranking. The reason is that policy documents often mix exact code strings with longer narrative language, so the production system needs both precise string matching and better semantic recall.

Evidence grounding is a structural requirement, not just a prompt instruction. Every final decision should include citations from the retrieved policy text, including decisions that originate from deterministic overrides. Each citation should contain the section title or identifier when available, plus the relevant excerpt. Before a decision is accepted, the system should validate that the cited excerpt actually appears in the retrieved evidence. This prevents the model from inventing policy rules or paraphrasing loosely enough that the support can no longer be verified.

**How it is implemented**
- policy documents are ingested into plan-scoped chunk indexes
- each chunk stores the text span plus useful metadata such as section title
- retrieval queries are built from claim fields that carry coverage meaning
- top-k chunks are passed into the decision prompt
- if a deterministic override fires, a small grounding retrieval step can still attach plan evidence to the final action
- cited excerpts are validated against the retrieved text before the decision is accepted

**Why this design**
- storage by insurer and plan prevents cross-policy evidence leakage
- chunk-level retrieval gives the model focused context rather than forcing it to read full documents
- citation validation makes policy grounding enforceable instead of aspirational

**Tradeoff**
- lexical retrieval is easy to reason about and cheap to run, but weaker on paraphrase and semantic variation
- hybrid retrieval is stronger, but more complex and more expensive
- for both approaches, retrieval quality is part of the main decision path, not just a supporting feature

### 1.2.1 Data Quality and Policy Freshness

The retrieval system is only as reliable as the policy corpus it reads from, so document quality and freshness need to be treated as part of the design.

**How it is implemented**
- policy documents should be versioned by insurer, plan, and effective date
- ingestion should validate that each document parsed successfully and produced a reasonable number of chunks
- low-quality parses, OCR failures, or unexpected document structures should be quarantined for manual review instead of being added silently to the live index
- when a new policy version is added, the system should rebuild the index and run a regression evaluation before the new corpus is promoted

**Why this design**
- a strong decision model cannot recover from stale or badly parsed policy documents
- treating corpus quality as a first-class concern reduces silent retrieval failures that are otherwise hard to detect

**Tradeoff**
- this adds operational work around ingestion and version management
- that tradeoff is justified because policy freshness and parse quality directly affect the correctness of every downstream decision

### 1.3 Decision Workflow and Tool Selection

This is the core of the system: how claims move from input to final action.

#### 1.3.1 General workflow for all claims

At a high level, every claim follows the same pipeline:

`claim input → input validation and guardrails → deterministic override checks → policy retrieval → model decision → policy / action guardrails → output guardrails → final action + trace`

![General workflow steps](./diagrams/general-workflow-steps.png)

This shared workflow is important because it keeps the system consistent across claim types. The claim type changes what evidence is emphasized and what action is most likely, but the overall control flow stays the same.

**Step 0: Claim intake and input validation**

Before the main decision workflow begins, the system checks that the claim is valid enough to process.

**How it is implemented**
- schema validation on structured fields such as `member_status`, `claim_type`, `diagnosis_code`, `procedure_code`, and `claimed_amount`
- supported insurer / plan checks
- required-field validation
- for conversational intake, a lightweight extraction step can normalize free text into the internal claim schema before review starts

**Why this design**
- this is best handled with deterministic validation, not the LLM
- it is cheaper, faster, and more reliable than asking a model whether the input is valid
- it prevents malformed or unsupported inputs from entering retrieval and decisioning

**Tradeoff**
- deterministic validation is strict, so unsupported or incomplete claims may be rejected or paused earlier
- that is acceptable because the goal of this layer is control, not flexibility

**Step 1: Input guardrails**

After schema validation, the system applies basic safety and scope checks before any decisioning happens.

**How it is implemented**
- checks for unsupported insurers or plans
- checks for malformed or clearly irrelevant free-text inputs
- detection of prompt-injection style text in `provider_notes`
- optional redaction or safe logging rules before free text is persisted

**Why this design**
- input guardrails stop unsafe or out-of-scope requests before they reach the main workflow
- they reduce the chance that downstream components operate on data they were never designed to handle

**Tradeoff**
- input guardrails should stay narrow
- if they become too aggressive, they risk acting like a second hidden decision engine

**Step 2: Deterministic override checks**

This step enforces the spec’s global override rules before any policy reasoning happens.

**How it is implemented**
- a deterministic rules module checks:
  - inactive plan
  - missing essential documentation
  - high claim amount
  - diagnosis / procedure mismatch
- if one of these rules fires, the system short-circuits to the required action
- in production, the returned action can still be attached to a policy citation through a small grounding retrieval call so every decision remains policy-supported

**Why this design**
- these rules are explicitly stated in the spec, so they should be implemented in code rather than left to prompt interpretation
- this reduces cost and latency by avoiding unnecessary LLM calls
- it also improves auditability because reviewers can see exactly which override fired and why

**Tradeoff**
- a rule engine is rigid, so it should only be used for clear, explicit rules
- more nuanced policy interpretation should stay in the retrieval plus model layer

**Step 3: Policy retrieval**

If no override fires, the system retrieves policy evidence from the correct insurer and plan.

**How it is implemented**
- the claim fields are converted into a retrieval query using the diagnosis code, procedure code, claim type, provider notes, and prior denial reason when present
- the retrieval layer searches only within the correct insurer / plan index
- the result is a top-k set of policy chunks, each with section title, excerpt, and score

**Why this design**
- this is a standard RAG step
- restricting retrieval to the correct plan ensures the system cannot cite the wrong insurer’s policy
- returning top-k chunks gives the model enough context to reason while keeping the prompt bounded

**Tradeoff**
- lexical retrieval is fast and interpretable, but it can miss semantic matches
- a production system can improve recall with hybrid retrieval and reranking

**Step 4: Model decision**

Once policy evidence has been retrieved, the system asks the model to choose the next action.

**How it is implemented**
- the retrieved policy chunks, structured claim fields, and claim context are assembled into a decision prompt
- the model is given a fixed action set:
  - `approve_pre_authorization`
  - `approve_claim_payment`
  - `deny_claim`
  - `request_missing_documentation`
  - `route_to_senior_reviewer`
  - `route_to_coding_review`
- the model uses tool calling or function calling to return one action, rather than replying in free text

**Why this design**
- policy interpretation is a natural-language reasoning task, so a model is appropriate here
- tool calling gives a structured, parseable decision that is easier to validate and log
- it avoids the ambiguity of turning long-form prose into an action after the fact

**Tradeoff**
- model reasoning is more flexible than rules, but also less deterministic
- that is why the action must later pass through validation and guardrails

**Step 5: Policy / action guardrails**

After the model proposes an action, the system validates that the action is acceptable before using it.

**How it is implemented**
- validate that the action is from the allowed action set
- validate that the action fits the claim type and claim state
- validate that citations are present when required
- validate that citations match retrieved text
- validate that cited policy comes from the correct insurer and plan
- validate structured outputs such as payment amount

**Why this design**
- this ensures the system does not present an ungrounded or malformed output as valid
- it turns policy grounding into an enforceable system property rather than a soft prompt instruction

**Tradeoff**
- this adds complexity after the model call
- that complexity is justified because claims decisions need a strong validation layer before they are accepted

At this stage the system can also compute a simple confidence signal for escalation. In production, I would treat confidence as a composite signal rather than a single model score. Useful inputs include retrieval strength, whether a valid citation was found, whether the action passed all guardrails cleanly, and whether the case belongs to a known high-risk category such as appeals or high-value claims. If that combined signal is weak, the system should escalate rather than force automation.

**Step 6: Output guardrails**

Before the final response is returned, the system checks that it is safe to present to the user.

**How it is implemented**
- enforce the required disclaimer
- block unsupported medical advice
- downgrade unsafe or invalid outputs to escalation
- ensure only supported policies and supported actions are described in the final response

**Why this design**
- this is the final safety layer before the output reaches a human or downstream system
- it prevents unsafe model outputs from being presented as valid decisions

**Tradeoff**
- output guardrails should stay focused on safety and compliance
- they should not become a second reasoning layer that silently rewrites the decision logic

**Step 7: Final action and trace**

Once the decision passes validation, the system returns the action, explanation, citations, and next-step fields.

**How it is implemented**
- return the final action label
- return the reason and supporting citations
- return structured next-step fields such as payment amount, requested documents, or routing target
- log the full trace, including evaluated rules, retrieved chunks, selected action, citations, and validation results

**Why this design**
- a production system needs not only a decision, but also an audit trail
- the trace is what makes it possible to debug wrong approvals, wrong denials, and unnecessary escalations

**How an incorrect denial would be debugged**
- first check whether a deterministic override fired, such as inactive plan or missing required documents
- if no override fired, inspect the retrieved policy chunks and their ranking scores to see whether the right evidence was surfaced
- then inspect the selected action, attached citations, and validation results to see whether the denial was grounded in the retrieved policy text
- if the right evidence was not retrieved, the fix belongs in retrieval
- if the right evidence was retrieved but misused, the fix belongs in prompting, decision logic, or guardrails

#### 1.3.2 Pre-Approval workflow

The pre-approval path uses the shared pipeline above, but emphasizes coverage criteria and medical necessity.

![Claim-type workflow](./diagrams/claim-type-workflow.png)

**How it is achieved**
- retrieval focuses on diagnosis code, procedure code, provider notes, and pre-approval language in the policy
- the retrieved policy chunks are passed into the decision prompt so the model can compare the clinical context against the policy’s coverage criteria
- the model can choose `approve_pre_authorization`, `deny_claim`, `request_missing_documentation`, or `route_to_senior_reviewer`
- if required documents are missing, the deterministic override layer can short-circuit before the model runs

**Why this design**
- pre-approval decisions depend heavily on natural-language policy criteria such as medical necessity
- retrieval plus model reasoning is a better fit than a purely rules-based system
- the deterministic missing-document check prevents the system from trying to approve or deny a claim that is obviously incomplete

**Tradeoff**
- this approach is flexible, but it relies on retrieval quality
- weak retrieval should therefore result in escalation rather than confident automation

#### 1.3.3 Standard Claim workflow

The standard claim path uses the same shared pipeline, but places more weight on coverage validation and code consistency.

**How it is achieved**
- diagnosis and procedure fields are validated early by the deterministic override layer
- if no mismatch rule fires, retrieval focuses on coverage and exclusion language for the requested service
- the decision prompt asks the model whether the service should be paid, denied, or escalated
- the model can choose `approve_claim_payment`, `deny_claim`, or `route_to_senior_reviewer`
- if the claim is approved, the response must also include `payment_in_dollars`, which is checked by the policy / action guardrails

**Why this design**
- code consistency is a hard workflow rule and should not depend on model judgment
- coverage validation still requires policy interpretation, so it is handled in the retrieval plus model layer
- keeping payment as a structured output field makes it easier to validate than if it were embedded in prose

**Tradeoff**
- the split between deterministic code checks and model-based coverage checks adds some architectural complexity
- that complexity is worth it because it keeps hard rules explicit and policy interpretation flexible

#### 1.3.4 Denial Appeal workflow

The appeal path uses the same shared pipeline, but adds more weight to the prior denial reason and the new supporting evidence.

**How it is achieved**
- the structured claim includes `prior_denial_reason`
- that field is included in both the retrieval query and the decision prompt
- retrieval still searches only within the correct insurer and plan corpus
- the prompt asks the model to consider whether the new documentation is new, sufficient, and relevant to the prior denial reason
- the model can choose `approve_claim_payment`, `approve_pre_authorization`, `deny_claim`, `request_missing_documentation`, or `route_to_senior_reviewer`

**Why this design**
- appeals are different from standard claims because the prior denial reason changes what evidence matters
- including the prior denial reason directly in retrieval and prompting makes the system reason about the appeal in context instead of treating it like a new claim
- keeping appeals inside the same action framework makes the system easier to evaluate and operate

**Tradeoff**
- this is the weakest part of the current workflow structurally
- in the current design, the comparison between prior denial reason and new evidence is handled mainly through prompting rather than a dedicated appeal-comparison module
- for a prototype this is acceptable, but in production I would likely add a more explicit appeal-specific comparison step

In production, I would make this more explicit with a dedicated appeal-comparison module. That module would compare four things: the prior denial reason, the prior policy basis for the denial if available, the newly submitted evidence, and whether the new evidence is actually new, sufficient, and relevant. If the module cannot make that comparison clearly, the case should escalate rather than forcing the model to make a brittle overturn or uphold decision.

### 1.4 Response Composition

Each output should contain four things:
- a primary action label
- a short plain-English reason
- supporting policy citations
- any next-step fields such as payment amount, requested documents, or routing target

The reason should be concise and easy for a claims operator to read. A simple structure works best:
- what the policy says
- how the claim matches or fails it
- what the system is doing next

In a production workflow, the goal is not to generate the most detailed essay. The goal is to return a decision that is clear, traceable, and easy to challenge or confirm.

Citations should always be attached in a reviewer-friendly format. That means showing the section title or identifier when available, plus the supporting excerpt itself. The explanation and the citations should agree with each other. If the explanation says the service is excluded, the citation should show the exclusion language. If the explanation says the claim is being escalated because the policy is ambiguous, the cited text should reflect that ambiguity or incompleteness.

**How it is implemented**
- the model returns a structured action through tool calling
- each action includes a short free-text reason
- citations are attached as structured fields rather than embedded loosely in the narrative
- next-step information such as payment amount, requested documents, or routing target is returned as separate structured outputs

**Why this design**
- structured outputs are easier to validate and log than open-ended prose
- the reason format is short enough for operators to scan quickly
- separating the narrative from the structured fields makes the response easier for both people and downstream systems to consume

**Tradeoff**
- this format is less flexible than free-form explanation
- that is acceptable because operational clarity matters more than stylistic richness in a claims workflow

The audience here is not only the pipeline. It is also the hospital staff member, the reviewer, and anyone auditing the decision later.

### 1.5 Safety and Compliance Boundaries

The system must not invent business rules. It must not give medical advice. It must not cite the wrong insurer’s policy. It must not produce an automatic decision when the evidence is weak, missing, or contradictory.

These boundaries should be enforced in multiple places, not left to a single prompt. Input guardrails should stop unsupported or malformed claims early. Rule-based conditions should be enforced by code. Policy grounding should be enforced by retrieval scoping and citation validation. Output guardrails should block invalid or unsafe responses before they reach the user. Unsupported or contradictory cases should be handled by escalation, not by confident-sounding model output.

The main escalation triggers are:
- a deterministic rule requires human review
- retrieval confidence is too low
- the policy evidence is conflicting
- required citations are missing or fail validation
- an appeal includes unclear or insufficient new evidence

**How it is implemented**
- input guardrails block unsupported or malformed claims early
- deterministic overrides enforce clear rules in code
- retrieval scoping prevents wrong-policy evidence
- citation validation and action validation prevent unsupported decisions from being accepted
- output guardrails block unsafe or invalid final responses

**Why this design**
- safety in this system is not just a prompt concern
- it needs to be enforced across the pipeline because different risks appear at different stages
- this layered design makes it less likely that one missed check turns into an unsafe decision

**Tradeoff**
- layered safety checks add latency and implementation complexity
- that tradeoff is justified because under-escalation is riskier than modest over-escalation in a claims workflow

The system should prefer safe escalation over brittle automation. Over-escalation creates extra reviewer work, but under-escalation is riskier because it can produce a wrong decision that appears grounded and trustworthy even when it is not. In production, this means safety is not only about preventing harmful text output. It is also about making sure the system knows when not to act automatically.

The same design also supports observability and debugging. The system should log the retrieved documents and ranking scores, the rule checks that were evaluated, the selected tool or function call, and the final validation outcome. This creates an intermediate workflow trace without requiring raw chain-of-thought logging. In practice, this trace is what allows the team to explain why a claim was denied, escalated, or sent for documentation review. In a production setting, these traces should be PHI-aware: structured fields can be stored directly, while free-text notes should be redacted, minimized, or stored behind stricter access controls rather than copied broadly into logs.

### 1.6 Production Notes and Prototype Scope

The prototype for this take-home should be simpler than the full production design. It may use a smaller policy corpus, a simpler retrieval method, and a lighter user interface. In the current implementation, the policy corpus is limited to Blue Shield of California PPO, EPO, and HMO documents. That is acceptable as long as the report is clear about what is implemented now and what the production system would add.

The production version of this design would extend in a few predictable ways:
- support more insurers and plan types
- use stronger retrieval and reranking
- keep durable audit logs and reviewer feedback loops
- roll out changes through shadow mode, canary release, and staged evaluation
- cache repeated retrieval results for common plan and procedure patterns to reduce latency on frequent queries

**How it is implemented in the prototype**
- a smaller policy corpus can be indexed
- retrieval can stay simple and plan-scoped
- the UI can focus on demonstrating the end-to-end workflow rather than full production operations
- some production modules such as shadow mode, richer calibration, and more advanced retrieval can remain design-only

**Why this scope is appropriate**
- the take-home should prove the system shape, not reproduce a full enterprise platform
- it is more important to show a clean end-to-end design with the right control points than to overbuild every production feature

**Tradeoff**
- the prototype will not fully represent production scale, coverage, or retrieval quality
- the report therefore needs to be explicit about what is implemented now and what would be added later

I would still keep the core system as a single decision agent inside a structured pipeline. That is the simplest design to debug, validate, and evaluate. If the system later grows in scope, some independent subtasks could be parallelized, such as document extraction, retrieval planning, or evidence critique. A multi-agent pattern could reduce latency in those cases, but I would treat it as a scaling optimization rather than the default architecture.

The important point is that the core system shape does not need to change. The same design still applies: retrieve from the correct policy, validate the evidence, choose from a fixed action set, and escalate when the case is not safe to automate. Reviewer feedback can improve this system over time by feeding back into labels, prompts, rules, and retrieval tuning. That is the main reason this architecture is a good fit for the problem. It is simple enough to prototype, but it has a clear path to a production-ready system.
