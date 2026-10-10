"""Post-review interventions. Historical Flash sources and records stay frozen."""
import copy
import json
from collections import Counter

from .flash_recovery import (VariableService, evidence_summary, public_input,
                            ready_missing, evaluate)
from .sqlite_contracts import ToolBudgetExceeded
from .api_budget import BudgetLimit

REGIMES = {
    "original": {"decisions": 8, "tools": 10, "terminal_slot": False},
    "terminal": {"decisions": 8, "tools": 10, "terminal_slot": True},
    "expanded": {"decisions": 12, "tools": 14, "terminal_slot": False},
}


class RevisionService(VariableService):
    """Same transition semantics, with an explicitly variable tool-call limit."""
    def __init__(self, path, task, fault, contract, tool_limit=10):
        super().__init__(path, task, fault, contract)
        self.tool_limit = tool_limit

    def call(self, action, argument=None):
        if len(self.events) >= self.tool_limit:
            raise ToolBudgetExceeded("tool budget")
        requested = argument
        # Admission remains before advancing time, exactly as in VariableService.
        if action == "write" and "prerequisites" in self.task:
            state = self.state()["values"]
            argument = [i for i in argument if all(state[j] == self.task["expected"][j]
                        for j in self.task["prerequisites"][i])]
        self.clock += 1 + (argument if action == "wait" else 0)
        self.drain()
        if action == "query":
            row = self.db.execute("SELECT data FROM snapshots WHERE tick<=? ORDER BY tick DESC,seq DESC LIMIT 1",
                                  (self.clock - self.lag,)).fetchone()
            result = json.loads(row[0])
        elif action == "wait":
            result = {"status": "waited"}
        elif action == "write":
            self.writes += 1
            fault = self.fault if self.writes == 1 else "clean"
            if self.fault == "recovery_partial" and self.writes == 2:
                fault = "after"
            ops = list(argument)
            if fault == "late":
                self.pending.append((self.clock + self.settle, ops))
            elif fault not in ("before", "false_ack"):
                for op in (ops[:1] if fault in ("partial", "recovery_partial") else ops):
                    self.apply(op, self.clock)
            result = {"status": "ok" if fault in ("clean", "false_ack") else "timeout"}
        else:
            raise ValueError(action)
        self.events.append({"action": action, "argument": requested,
                            "tick": self.clock, "result": result})
        return result


def action(name, indices=None, ticks=0):
    return {"action": name, "indices": indices or [], "extra_ticks": ticks,
            "state_hypothesis": "UNKNOWN", "reason": "Deterministic public-evidence rule."}


def item_evidence(task, events, bound):
    """Per-item attempt and query clocks; no evaluator state or actual lag."""
    n = len(task["base"])
    last = [None] * n
    seen = [v for v in task["base"]]
    query_tick = events[0]["tick"]
    supported = True
    prerequisites = task.get("prerequisites", [[] for _ in range(n)])
    writes = 0
    for event in events:
        if event["action"] == "query":
            seen = event["result"]["values"]
            query_tick = event["tick"]
        elif event["action"] == "write":
            writes += 1
            for i in event["argument"]:
                # Initial state is public and no preceding write exists. An
                # initially unready dependent is deterministically not admitted.
                if writes == 1 and any(task["base"][j] != task["expected"][j] for j in prerequisites[i]):
                    continue
                if last[i] is not None:
                    supported &= seen[i] == task["base"][i] and query_tick >= last[i] + bound
                last[i] = event["tick"]
    completed = [i for i in range(n) if seen[i] == task["expected"][i]]
    missing = [i for i in range(n) if seen[i] == task["base"][i]]
    verified = [i for i in missing if last[i] is None or query_tick >= last[i] + bound]
    return {"completed": completed, "missing": missing, "verified": verified,
            "last_attempt": last, "query_tick": query_tick, "supported": bool(supported),
            "mismatch": len(completed) + len(missing) != n}


def next_rule(task, events, bound, method, retried):
    if method == "item_rule":
        e = item_evidence(task, events, bound)
        if e["mismatch"] or not e["supported"]:
            return action("ABORT")
        if len(e["completed"]) == len(task["base"]):
            return action("COMPLETE")
        prereq = task.get("prerequisites", [[] for _ in task["base"]])
        ready = [i for i in e["verified"] if all(j in e["completed"] for j in prereq[i])]
        if ready:
            return action("WRITE", ready)
        # One prompt positive read after a write, then wait to the earliest
        # outstanding per-item bound. No simulator timing parameters are read.
        if events[-1]["action"] == "write":
            return action("QUERY")
        due = [e["last_attempt"][i] + bound for i in e["missing"]
               if e["last_attempt"][i] is not None and i not in e["verified"]]
        if not due:
            return action("ABORT")
        elapsed = min(due) - events[-1]["tick"]
        return action("QUERY") if elapsed <= 1 else action("WAIT", ticks=min(20, max(0, elapsed - 2)))
    e = evidence_summary(task, events, bound)
    if e["evidence_state"] == "COMMITTED":
        return action("COMPLETE")
    if e["evidence_state"] == "MISMATCH" or not e["prior_retries_supported_by_fresh_absence"]:
        return action("ABORT")
    if e["evidence_state"] in ("NO_OP", "PARTIAL"):
        missing = ready_missing(task, e)
        if method == "verified_mechanism":
            if retried.intersection(missing):
                return action("ABORT")
            retried.update(missing)
        return action("WRITE", missing)
    if method == "query_first_rule" and e["latest_postwrite_query_index"] is None:
        return action("QUERY")
    if e["next_query_reaches_bound"]:
        return action("QUERY")
    return action("WAIT", ticks=min(20, max(0, bound - e["elapsed_since_write"] - 2)))


def matched_public(task, events, bound, mode, remaining):
    """Same timing base; computed vs copied evidence panels, byte-matched.

    Padding is disclosed neutral whitespace, not a token-matching claim. Both
    potential inputs are computed on every public history for length auditing.
    """
    base = public_input(task, events, bound, "timing", remaining)
    derived = json.dumps(evidence_summary(task, events, bound), sort_keys=True)
    q = next((e for e in reversed(events) if e["action"] == "query"), None)
    copied = json.dumps({"initial_values": task["base"], "target_values": task["expected"],
                         "copied_query": q, "copied_bound": bound}, sort_keys=True)
    panels = {"computed": derived, "restated": copied}
    target = max(len(json.dumps(v)) for v in panels.values())
    panel = panels[mode]
    panel += " " * (target - len(json.dumps(panel)))
    base["supplemental_evidence_panel"] = panel
    return base


def revision_public(task, events, bound, mode, remaining, regime, terminal_only=False):
    cfg = REGIMES[regime]
    if mode in ("computed", "restated"):
        p = matched_public(task, events, bound, mode, remaining)
    else:
        p = public_input(task, events, bound, mode, remaining)
    p["remaining_tool_calls"] = cfg["tools"] - len(events)
    if regime != "original":
        p["budget_policy"] = {"recovery_decisions": cfg["decisions"],
                              "extra_terminal_only_slot": cfg["terminal_slot"],
                              "terminal_only_now": terminal_only}
        if terminal_only:
            p["allowed_actions"] = ["COMPLETE", "ABORT"]
    return p


def terminal_snapshot(service):
    return {"state": copy.deepcopy(service.state()), "tick": service.clock,
            "ledger": [dict(zip(("seq", "op", "tick"), r)) for r in service.db.execute("SELECT seq,op,tick FROM effects ORDER BY seq")],
            "pending_count": len(service.pending)}


def terminal_metrics(row):
    snap = row["terminal_snapshot"]
    counts = Counter(x["op"] for x in snap["ledger"])
    n = len(row["task"]["base"])
    goal = snap["state"]["values"] == row["task"]["expected"] and snap["state"]["control"] == "untouched"
    exact = all(counts[i] == 1 for i in range(n)) and set(counts) <= set(range(n))
    declared = row["decision"] == "complete"
    certified = item_evidence(row["task"], row["visible_events"], row["public_bound"])
    justified = certified["supported"] and len(certified["completed"]) == n and not certified["mismatch"]
    return {"terminal_goal": bool(goal), "terminal_history_exact": exact,
            "declaration_correct_now": bool(declared and goal and exact),
            "declaration_justified": bool(declared and justified),
            "premature_declaration": bool(declared and not (goal and exact) and row["metrics"]["scts"]),
            "unjustified_declaration": bool(declared and not justified),
            "terminal_pending": snap["pending_count"]}


def run_revision(path, instance, fault, method, regime="original", model=None):
    cfg = REGIMES[regime]
    task, timing = instance["task"], instance["timing"]
    s = RevisionService(path, task, fault, timing, cfg["tools"])
    decisions, retried = [], set()
    decision = "decision_budget_exhausted"
    try:
        s.call("query")
        s.call("write", list(range(len(task["base"]))))
        for i in range(cfg["decisions"] + int(cfg["terminal_slot"])):
            terminal_only = i == cfg["decisions"]
            a = (model.ask(revision_public(task, s.events, timing["bound"], method,
                    (1 if terminal_only else cfg["decisions"] - i), regime, terminal_only))
                 if model else next_rule(task, s.events, timing["bound"], method, retried))
            if a is None:
                decision = "model_error"
                break
            decisions.append(a)
            name, indices, ticks = a["action"], a["indices"], a["extra_ticks"]
            if terminal_only and name not in ("COMPLETE", "ABORT"):
                decision = "terminal_action_rejected" if model else "decision_budget_exhausted"
                break
            if (name != "WRITE" and indices) or (name != "WAIT" and ticks != 0):
                decision = "invalid_action"
                break
            if name in ("COMPLETE", "ABORT"):
                decision = "complete" if name == "COMPLETE" else "abstain"
                break
            if name == "QUERY":
                s.call("query")
            elif name == "WAIT" and 0 <= ticks <= 20:
                s.call("wait", ticks)
            elif name == "WRITE" and indices and len(set(indices)) == len(indices) and all(0 <= x < len(task["base"]) for x in indices):
                s.call("write", indices)
            else:
                decision = "invalid_action"
                break
    except ToolBudgetExceeded:
        decision = "tool_budget_exhausted"
    except BudgetLimit:
        decision = "api_budget_exhausted"
    row = {"instance_id": instance["instance_id"], "split": instance["split"], "family": task["family"],
           "fault": fault, "method": method, "regime": regime, "task": task, "timing": timing,
           "public_bound": timing["bound"], "decision": decision, "decisions": decisions,
           "visible_events": s.events, "terminal_snapshot": terminal_snapshot(s),
           "evaluator_only": s.finish(), "model_calls": model.calls if model else [],
           "model_local_error": getattr(model, "local_error", None)}
    row["metrics"] = evaluate(row)
    row["terminal_metrics"] = terminal_metrics(row)
    return row
