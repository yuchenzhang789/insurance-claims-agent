## Part 3: Open-Ended Discussion

### 3.1 Escalation to Humans

Escalation is not a failure mode in this system. It is a required safety mechanism. The agent should route a claim to a real claims specialist whenever the case is too risky, too uncertain, or too contradictory to support a reliable automated decision.

There are three main escalation signals.

| Signal type | Examples |
|---|---|
| Risk | high-value claim, complex appeal, higher compliance impact |
| Uncertainty | weak retrieval, vague notes, ungrounded action |
| Conflict | claim facts disagree with policy evidence, policy evidence is internally inconsistent |

The first is **risk**. Some cases deserve human review even if the evidence is not obviously wrong. Examples include high-value claims, complex appeals, and cases with higher operational or compliance impact. In these situations, the system should prefer human review over aggressive automation.

The second is **uncertainty**. The agent should escalate when the evidence is too weak to support a confident decision. In practice, this can happen when retrieval confidence is low, when the retrieved policy excerpts are not clearly relevant, when the claim notes are vague or low quality, or when the model cannot produce a grounded action with valid citations.

The third is **conflict**. The system should escalate when important parts of the case disagree with each other. This includes situations where the claim facts point one way and the policy evidence points another way, or where the retrieved policy evidence itself appears incomplete or internally inconsistent.

These triggers should be implemented as system rules, not just model suggestions. In other words, escalation should happen because the workflow recognizes the case as unsafe to automate, not because the model “feels unsure.” This is important because in a production claims workflow, over-escalation is usually recoverable, while under-escalation can create wrong decisions that look more reliable than they really are.

### 3.2 Handling Contradictions

The system should treat contradictions as first-class workflow events, not edge cases.

The first type of contradiction is **claim data vs. policy evidence disagreement**. For example, provider notes may suggest strong medical need, while the retrieved policy language does not clearly support coverage. In these cases, the system should not force an approval just because the notes are persuasive, and it should not force a denial if the policy evidence is incomplete. The safer behavior is to escalate and surface the conflicting information clearly in the trace and in the explanation.

The second type of contradiction is **missing or low-quality documentation**. If the claim is missing required documents, the correct next step is usually to request the missing information rather than guess. If documents are present but vague, incomplete, or low quality, the system should either request clarification or escalate, depending on whether the missing information is specific and recoverable. This is how the design handles real-world noise without pretending that low-quality input can always be automated safely.

This distinction matters. Missing documentation is often a workflow gap that can be resolved by asking for more material. Contradictory evidence is usually a judgment problem that should be reviewed by a human specialist.

In production, I would make this behavior explicit in the workflow:
- if the missing information is known and specific, request documentation
- if the evidence is present but contradictory or too ambiguous, escalate
- if a final decision cannot be grounded in the retrieved policy text, escalate rather than guess

This approach keeps the system conservative in the right way. The goal is not to maximize automation at all costs. The goal is to automate the safe cases, ask for more information when the gap is clear, and escalate the cases where real human judgment is still needed.
