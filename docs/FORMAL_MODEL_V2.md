# Formal model and proof obligations

## Objects

At logical time t, evaluator state is (x_t,p_t,h_t): stored values, pending attempts, and committed operations. The controller sees H_t, the sequence of calls, returns and ticks, and a public contract K. Evidence z_t=phi(H_t,K) is deterministic; it performs computation but has no privileged observations. Actions QUERY, WAIT(d), WRITE(I), COMPLETE and ABORT are the only interface. Every tool call advances one tick, WAIT adds d. WRITE is non-atomic across distinct indices. Pending effects are drained before the resulting observation. A delayed QUERY reads a persisted snapshot.

G means final target values and preservation of control state. H means one committed mutation per intended operation and no other mutations. C means COMPLETE was declared. SCTS=G∧H∧C. Evaluation settles pending writes before computing G and H. A correct final state is insufficient to infer an arbitrary safe history.

## Assumptions for the core safety argument

A1. Each request applies each distinct selected operation at most once; no transport redelivery.
A2. Each attempted effect either settles or is permanently absent within S ticks. Reads lag by at most L ticks. Public B≥S+L is truthful.
A3. No external writers and no operation can undo another operation’s target evidence. Initial and target values are distinct, and under at-most-once execution the query distinguishes an absent from a completed item.
A4. The controller starts from one initial request per item, issues no unobserved writes, and retries an item only after a sufficiently late query shows its initial value.
A5. Queries faithfully expose the relevant state, including preservation of unrelated state; the evaluator’s hidden history is not used by the policy.

## Proposition (safety of supported retries; application of established bounded-verification reasoning)

Under A1–A5, every item has at most one committed effect when the controller obeys A4. Once all items are positively observed at their target values, COMPLETE is sound for the core task contract.

Proof. Induct on WRITE requests. A1 bounds the initial request. Before any retry of item i, a query at q≥w+B after the latest relevant attempt w includes every possible effect of that attempt: q−L≥w+S. Its initial value therefore establishes that the attempt cannot later add an effect, using A2–A3. Retrying i preserves the at-most-once invariant by A1; items not retried keep the invariant. Positive target observations then imply at least one effect each by A3, hence exactly one by the invariant. A3 ensures no later external change; no pending repeated attempt can add a duplicate. Control-state preservation follows from the restricted write interface and A5. This proves the stated task guarantee, not linearizability of arbitrary external services.

Waiting alone never refreshes an old observation. With elapsed e and one tick per call, a direct query suffices if e+1≥B; otherwise WAIT(max(0,B−e−2)) followed by QUERY reaches the bound (when the wait fits the interface cap). This calculation follows simulator semantics, not a production timeout rule.

## Liveness is additional

If a safe retry eventually commits and the remaining calls suffice for waiting, writing and re-querying, completion follows. Arbitrary recurring faults or unknown bounds do not permit a finite completion guarantee. The current seven core schedules allow at most an initial failure plus a committed recovery response timeout, so one supported retry per missing item suffices; this is why a one-retry mechanism can match stronger rules. Ten calls and eight decisions are experimental limits, not universal theoretical bounds.

## Boundary examples

Unknown S permits two histories identical through a query: no commit versus a later commit. Retrying distinguishes neither and can duplicate; this is prior-work territory (LIMBO), not a new theorem. A false B invalidates the inequality above. Untracked writers or a target-equals-initial operation invalidate A3. Repeated assignment may preserve G while violating H. Dependency-based admission in the external extension requires a progress assumption for prerequisite stages, so its liveness is evaluated separately.

Implementation links: flash_recovery.public_input, timing_summary, evidence_summary, control_rule; inherited sqlite_contracts.Service defines effects and snapshots. Offline tests check the finite schedules but are not substitutes for this argument.

## Reference algorithm

The anonymous manuscript Appendix A.2 contains the wait-rule pseudocode and explicitly counts terminal decisions against the eight-decision budget. Implementation freshness is measured from the most recent write, conservatively satisfying the per-item sufficient condition above. Query-first and one-retry verification differ only as documented in the capability table.
