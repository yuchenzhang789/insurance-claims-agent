## Part 2: Evaluation Framework

The goal of evaluation is not only to check whether the agent gets the final action right. It also needs to show that every final decision is supported by the correct policy evidence, that the system behaves safely when inputs are incomplete, conflicting, or unclear, and that we can detect regressions before deployment. In production, I would treat evaluation as an ongoing pipeline, not a one-time benchmark.

### 2.1 Evaluation Dataset

I would build the evaluation set as a labeled corpus of claims, policy evidence, and expected outcomes. Each case would contain:
- the structured claim input
- the correct insurer / plan
- the expected action
- one or more acceptable policy excerpts from the correct document
- tags such as claim type, ambiguity level, missing-document condition, or contradiction type

The dataset would come from three sources.

First, I would create a small hand-labeled seed set from the sample claims and policy PDFs. The sample claims reference insurers including Aetna, UHC, and Bluecross. In the current prototype, those scenarios are remapped into a Blue Shield of California policy corpus so the workflow can be implemented end to end, while the production design supports one indexed corpus per carrier and plan. This seed set gives a trusted starting point and makes sure every major workflow is covered.

Second, I would create a robustness set by perturbing the seed cases. For example:
- remove a required document
- flip `member_status` from active to inactive
- inject a diagnosis / procedure mismatch
- replace clear provider notes with vague or low-quality text
- add provider notes that conflict with the policy evidence

These perturbations are important because the spec explicitly requires robustness to missing documents, conflicting evidence, and ambiguous or low-quality text. A benchmark that only measures clean cases would overstate real-world reliability.

Third, in production I would grow the dataset from real failures. Cases where the agent disagrees with human reviewers, or where reviewers override the agent, should be added back into the evaluation set after labeling.

This reviewer feedback loop should be explicit. Reviewer outcomes can be used in three ways: to add new labeled cases to the eval set, to identify weak prompts or retrieval patterns, and to highlight rules that are too strict or too permissive. This is one of the main ways the system improves after deployment.

To reduce leakage, I would keep three splits:
- `development set` for prompt and threshold tuning
- `validation set` for model and configuration selection
- `held-out set` for final evaluation only

This matters because a claims agent can look stronger than it really is if it is tuned on the same small set it is later scored on.

### 2.2 Metrics

I would track three headline metrics and a few supporting diagnostics.

| Category | Measures |
|---|---|
| Headline metrics | action accuracy, citation precision / recall, robustness on perturbed cases |
| Supporting diagnostics | confusion matrix, over / under-escalation, latency / cost |

The first headline metric is **action accuracy**. This measures whether the predicted action matches the expected action. It is the main task metric because the system ultimately needs to choose the correct next step.

The second headline metric is **grounding quality**. In a full production evaluation, I would measure citation precision and recall against gold policy spans. In the current prototype, this is approximated with section-level matching such as `expected_section`, because the dataset does not yet contain excerpt-level span annotations. Together, these metrics answer the most important grounding question: did the system not only act, but act with the right evidence.

The third headline metric is **robustness on perturbed cases**. This measures whether the system still behaves safely when documents are missing, when notes conflict with policy evidence, or when text is vague or low quality. For this project, robustness matters because the spec explicitly requires the system to handle real-world noise rather than only clean inputs.

I would still keep a few supporting diagnostics:
- an **action confusion matrix** to show which errors are the most serious
- **over-escalation** and **under-escalation** rates to understand whether the system is calibrated too aggressively or too conservatively
- **latency and cost per reviewed claim** to make sure the system is practical to run in production

### 2.3 How Evaluation Runs

I would run evaluation through the full agent pipeline, not a separate mock path. For each labeled case, the harness would:
1. load the structured claim and the correct policy corpus
2. run the full review pipeline end to end
3. collect the final action, citations, and trace
4. compare the action against the gold label
5. compare the citations against the gold policy excerpts
6. score the case under the metrics above
7. aggregate results by slice, such as claim type, insurer / plan, override condition, ambiguity level, and perturbation type

The harness should produce both:
- a machine-readable JSON report for regression tracking
- a human-readable markdown report for inspection

This makes the evaluation useful both for regression checks and for manual review.

The case-level report should also preserve enough trace information to support debugging. At minimum, it should record the retrieved policy chunks and ranking scores, the selected tool or function call, and the validation outcome. This makes it possible to trace an incorrect denial back to the correct layer: rules, retrieval, decisioning, or guardrails.

### 2.4 LLM-as-a-Judge

I would use LLM-as-a-judge only as a secondary tool. The main metrics such as action correctness and citation overlap should be scored deterministically whenever possible.

The judge is useful for checking the quality of the free-text explanation. It would receive:
- the structured claim
- the policy excerpts shown to the agent
- the agent’s action
- the agent’s explanation
- the agent’s citations

It would then score questions like:
- do the cited excerpts actually appear in the retrieved evidence
- does the explanation accurately restate what the policy says
- does the explanation connect the claim facts to the cited policy correctly

This is helpful for explanation quality, but it should not be treated as the source of truth for final correctness. I would not trust it for rule-based checks, numeric checks, or as the only gate for release. I would also avoid using the same model family as both the actor and the judge without calibration against a human-labeled subset.

### 2.5 Online Evaluation and Safe Rollout

Offline evaluation is necessary, but it is not enough for a production claims system. Real traffic will contain edge cases that a static benchmark misses.

I would roll out changes in stages:
- `shadow mode`: run the new system in parallel without affecting real decisions
- `canary release`: send a small amount of low-risk traffic to the new system
- `A/B testing`: compare the candidate and the baseline on action quality, grounding quality, escalation behavior, latency, and cost
- `progressive rollout`: increase traffic only if the new system stays safe and stable

The main reason for this staged rollout is that claims errors are not symmetric. A small drop in grounding quality or a rise in under-escalation is more serious than a small latency increase, so rollout should optimize for safety first. In practice, I would not release a new prompt, model, or retrieval configuration unless it holds or improves on the baseline across held-out evaluation and shadow traffic, especially on grounding and robustness slices.

### 2.6 Failure Analysis and Fix Prioritization

Evaluation should not stop at a score. It should tell us what to fix next.

I would bucket failures into four groups:
- `action error`: wrong next action
- `grounding error`: missing, wrong, or irrelevant citation
- `robustness error`: failure under missing, conflicting, or ambiguous inputs
- `escalation error`: escalated when it should have acted, or acted when it should have escalated

I would prioritize fixes in this order:
1. `action error`, because wrong approvals or denials are the most serious
2. `grounding error`, because unsupported decisions violate the spec and reduce trust
3. `escalation error`, because this controls how safely the system handles uncertainty
4. `robustness error`, because these failures show where the system needs more data or better retrieval and prompting

This order keeps the system focused first on safety and correctness, then on better coverage and smoother automation.
