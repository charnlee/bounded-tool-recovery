"""Flash v2 evidence study; independent of frozen historical implementation."""
import time
import copy
import json
import random
from collections import Counter

from .sqlite_contracts import FAULTS, FAMILIES, Service, ToolBudgetExceeded
from .api_budget import BudgetLimit

EVIDENCE_MODES = ("raw", "organized", "timing", "full")
RULES = ("wait_rule", "query_first_rule", "verified_mechanism")
TIMINGS = ((0, 0), (2, 3), (5, 4), (1, 7))
MAX_DECISIONS = 8


def make_dataset():
    rows = []
    for split, seed in (("dev", 101021), ("test", 101029)):
        rng = random.Random(seed)
        for family in FAMILIES:
            sizes = [2, 2, 4, 4]
            rng.shuffle(sizes)
            for variant, n in enumerate(sizes):
                ident = f"flash-v2-{split}-{family}-{variant}"
                suffix = rng.randrange(100000, 999999)
                base = [rng.randrange(30, 180) for _ in range(n)]
                if family == "message":
                    base = [[] for _ in range(n)]
                    expected = [[f"Delivery notice {suffix}: item {i}"] for i in range(n)]
                elif family == "settings":
                    base = [f"previous-{suffix}-{i}" for i in range(n)]
                    expected = [f"requested-{suffix}-{i}" for i in range(n)]
                elif family == "inventory":
                    expected = [x + rng.randrange(1, 13) for x in base]
                else:
                    delta = []
                    for _ in range(n // 2):
                        amount = rng.randrange(1, 15)
                        delta.extend((-amount, amount))
                    expected = [a + b for a, b in zip(base, delta)]
                lag, settle = TIMINGS[variant]
                task = {"family": family, "variant": variant, "base": base, "expected": expected}
                rows.append({"instance_id": ident, "split": split, "task": task,
                             "timing": {"lag": lag, "settle": settle, "bound": lag + settle}})
    return {"version": "flash-parameters-v2", "scope": "new parameters in four already-known families; not held-out semantic domains",
            "instances": rows}


class VariableService(Service):
    def call(self, action, argument=None):
        requested = argument
        if action == "write" and "prerequisites" in self.task:
            state = self.state()["values"]
            argument = [i for i in argument if all(state[j] == self.task["expected"][j]
                                                  for j in self.task["prerequisites"][i])]
        result = super().call(action, argument)
        if action == "write":
            self.events[-1]["argument"] = requested
        return result

    def state(self):
        n = len(self.task["base"])
        if self.task["family"] == "message":
            values = [[r[0] for r in self.db.execute(
                "SELECT payload FROM messages WHERE target=? ORDER BY seq", (i,))] for i in range(n)]
        else:
            values = [r[0] for r in self.db.execute("SELECT value FROM objects WHERE k<>99 ORDER BY k")]
        return {"values": values, "control": self.db.execute(
            "SELECT value FROM objects WHERE k=99").fetchone()[0]}


def timing_summary(events, bound):
    writes = [i for i, e in enumerate(events) if e["action"] == "write"]
    last = writes[-1]
    queries = [i for i, e in enumerate(events) if i > last and e["action"] == "query"]
    q = queries[-1] if queries else None
    elapsed = events[-1]["tick"] - events[last]["tick"]
    return {"current_tick": events[-1]["tick"], "last_write_event_index": last,
            "last_write_tick": events[last]["tick"], "latest_postwrite_query_index": q,
            "elapsed_since_write": elapsed, "required_bound": bound,
            "query_is_fresh": q is not None and events[q]["tick"] - events[last]["tick"] >= bound,
            "next_query_reaches_bound": elapsed + 1 >= bound}


def evidence_summary(task, events, bound):
    timing = timing_summary(events, bound)
    writes = [i for i, e in enumerate(events) if e["action"] == "write"]
    supported_retries = True
    # A repeated write is justified only by a fresh absence observation after
    # the preceding write. This checks public evidence, not actual effects.
    for prev, current in zip(writes, writes[1:]):
        queries = [i for i in range(prev + 1, current) if events[i]["action"] == "query"]
        if not queries:
            supported_retries = False
            break
        q = queries[-1]
        if events[q]["tick"] - events[prev]["tick"] < bound:
            supported_retries = False
            break
        if any(events[q]["result"]["values"][i] != task["base"][i] for i in events[current]["argument"]):
            supported_retries = False
            break
    q = timing["latest_postwrite_query_index"]
    observed = None if q is None else events[q]["result"]["values"]
    missing = [] if observed is None else [i for i, v in enumerate(observed) if v == task["base"][i]]
    completed = [] if observed is None else [i for i, v in enumerate(observed) if v == task["expected"][i]]
    mismatch = observed is not None and len(missing) + len(completed) != len(observed)
    state = "UNKNOWN"
    if mismatch:
        state = "MISMATCH"
    elif observed is not None and supported_retries:
        if len(completed) == len(observed):
            # Positive evidence can suffice before the full lag bound: there are
            # no external writers, and every retry had fresh absence evidence.
            state = "COMMITTED"
        elif timing["query_is_fresh"]:
            state = "NO_OP" if not completed else "PARTIAL"
    return {**timing, "observed_completed_indices": completed if observed is not None else None,
            "observed_missing_indices": missing if observed is not None else None,
            "prior_retries_supported_by_fresh_absence": supported_retries,
            "verified_missing_indices": missing if state in ("NO_OP", "PARTIAL") else [],
            "evidence_state": state}


def public_input(task, events, bound, mode, decisions_remaining):
    public = {"task": copy.deepcopy(task), "public_contract": {
        "settlement_plus_read_lag_upper_bound": bound,
        "call_ticks": 1, "wait_adds_extra_ticks": True,
        "max_extra_wait_ticks": 20, "no_external_writers": True,
        "at_most_one_application_per_item_per_write_request": True,
        "ack_is_not_proof_of_effect": True, "idempotency_keys": False},
        "visible_events": copy.deepcopy(events), "remaining_tool_calls": 10 - len(events),
        "remaining_model_decisions_including_this_one": decisions_remaining}
    if mode != "raw":
        # Redundant indexing only: no predicates or inferred labels.
        public["organized_task"] = [{"index": i, "initial": b, "target": e}
                                    for i, (b, e) in enumerate(zip(task["base"], task["expected"]))]
        public["organized_events"] = {a: [{"event_index": i, **event} for i, event in enumerate(events) if event["action"] == a]
                                      for a in ("query", "write", "wait")}
    if mode in ("timing", "full"):
        public["timing_evidence"] = timing_summary(events, bound)
    if mode == "full":
        public["derived_evidence"] = evidence_summary(task, events, bound)
    elif mode not in EVIDENCE_MODES:
        raise ValueError(mode)
    return public


def control_rule(task, events, bound, method, call):
    for _ in range(8):
        evidence = evidence_summary(task, events, bound)
        state = evidence["evidence_state"]
        if state == "COMMITTED":
            return "complete", []
        if state == "MISMATCH" or not evidence["prior_retries_supported_by_fresh_absence"]:
            return "abstain", []
        if state in ("NO_OP", "PARTIAL"):
            call("write", ready_missing(task, evidence))
            continue
        if method == "query_first_rule" and evidence["latest_postwrite_query_index"] is None:
            call("query")
        elif evidence["next_query_reaches_bound"]:
            call("query")
        else:
            call("wait", min(20, max(0, bound - evidence["elapsed_since_write"] - 2)))
    return "decision_budget_exhausted", []


def control_model(task, events, bound, mode, call, model):
    decisions = []
    for i in range(MAX_DECISIONS):
        started = time.perf_counter()
        public = public_input(task, events, bound, mode, MAX_DECISIONS - i)
        model.evidence_seconds = getattr(model, "evidence_seconds", 0.0) + time.perf_counter() - started
        action = model.ask(public)
        if action is None:
            return "model_error", decisions
        decisions.append(action)
        name, indices, ticks = action.get("action"), action.get("indices"), action.get("extra_ticks")
        if not isinstance(indices, list) or any(type(x) is not int for x in indices) or type(ticks) is not int:
            return "invalid_action", decisions
        if (name != "WRITE" and indices) or (name != "WAIT" and ticks != 0):
            return "invalid_action", decisions
        if name == "COMPLETE":
            return "complete", decisions
        if name == "ABORT":
            return "abstain", decisions
        if name == "QUERY":
            call("query")
        elif name == "WAIT" and 0 <= ticks <= 20:
            call("wait", ticks)
        elif name == "WRITE" and indices and len(set(indices)) == len(indices) and all(0 <= x < len(task["base"]) for x in indices):
            call("write", indices)
        else:
            return "invalid_action", decisions
    return "decision_budget_exhausted", decisions


def evaluate(record):
    n = len(record["task"]["base"])
    end = record["evaluator_only"]
    counts = Counter(x["op"] for x in end["ledger"])
    goal = end["final"]["values"] == record["task"]["expected"] and end["final"]["control"] == "untouched"
    safe = all(counts[i] == 1 for i in range(n)) and set(counts) <= set(range(n))
    declared = record["decision"] == "complete"
    calls = record.get("model_calls", [])
    return {"scts": bool(goal and safe and declared), "goal": bool(goal), "history_exact": safe,
            "duplicate_mutations": sum(max(0, c - 1) for c in counts.values()),
            "missing_items": sum(counts[i] == 0 for i in range(n)),
            "false_completion": bool(declared and not (goal and safe)),
            "tool_calls": len(record["visible_events"]), "ticks": end["controller_ticks"],
            "model_requests": len(calls), "total_tokens": sum(c.get("usage", {}).get("total_tokens", 0) for c in calls),
            "model_errors": sum(c["status"] != "ok" for c in calls),
            "model_latency_seconds": sum(c.get("latency_seconds", 0) for c in calls)}


def run_episode(path, instance, fault, method, trial=0, model=None):
    assert fault in FAULTS and method in (*RULES, *EVIDENCE_MODES)
    task, timing = instance["task"], instance["timing"]
    service = VariableService(path, task, fault, timing)
    decisions = []
    try:
        service.call("query")
        service.call("write", list(range(len(task["base"]))))
        if method in RULES:
            if method == "verified_mechanism":
                decision, decisions = verified_mechanism(task, service.events, timing["bound"], service.call)
            else:
                decision, decisions = control_rule(task, service.events, timing["bound"], method, service.call)
        else:
            decision, decisions = control_model(task, service.events, timing["bound"], method, service.call, model)
    except ToolBudgetExceeded:
        decision = "tool_budget_exhausted"
    except BudgetLimit:
        decision = "api_budget_exhausted"
    result = {"run_id": f'{instance["instance_id"]}__{fault}__{method}__{trial}',
              "instance_id": instance["instance_id"], "split": instance["split"],
              "family": task["family"], "fault": fault, "method": method, "trial": trial,
              "task": task, "public_bound": timing["bound"], "decision": decision,
              "decisions": decisions, "visible_events": service.events,
              "evaluator_only": service.finish(), "model_calls": [] if model is None else model.calls}
    result["model_local_error"] = getattr(model, "local_error", None)
    result["evidence_compute_seconds"] = getattr(model, "evidence_seconds", 0.0)
    result["metrics"] = evaluate(result)
    return result


def verified_mechanism(task, events, bound, call):
    """Read-only three-valued verification; one retry per logical item.

    A mechanism-level adaptation of Mansoor et al. sections 4.4--4.7,
    not their complete agent. No keys in this common non-idempotent contract.
    Unknown absence waits for the public bound; success always postchecked,
    unlike Algorithm 1's response-SUCCESS shortcut. Adaptations are explicit.
    """
    retried = set()
    for _ in range(8):
        evidence = evidence_summary(task, events, bound)
        state = evidence["evidence_state"]
        if state == "COMMITTED":
            return "complete", []
        if state == "MISMATCH" or not evidence["prior_retries_supported_by_fresh_absence"]:
            return "abstain", []
        if state in ("NO_OP", "PARTIAL"):
            missing = ready_missing(task, evidence)
            if retried.intersection(missing):
                return "abstain", []
            retried.update(missing)
            call("write", missing)
        elif evidence["next_query_reaches_bound"]:
            call("query")
        else:
            call("wait", min(20, max(0, bound - evidence["elapsed_since_write"] - 2)))
    return "decision_budget_exhausted", []


def ready_missing(task, evidence):
    completed = evidence["observed_completed_indices"] or []
    return [i for i in evidence["verified_missing_indices"]
            if all(j in completed for j in task.get("prerequisites", [[] for _ in task["base"]])[i])]


def external_instances():
    rows = []
    rng = random.Random(101041)
    for structure in ("paired_dependencies", "fan_in_dependencies"):
        for variant in range(4):
            n = 4 if variant < 2 else 6
            base = [rng.randrange(50, 200) for _ in range(n)]
            expected = [x + rng.randrange(1, 15) for x in base]
            half = n // 2
            prerequisites = [[] for _ in range(half)] + ([[i] for i in range(half)] if structure == "paired_dependencies" else [list(range(half)) for _ in range(half)])
            lag, settle = TIMINGS[variant]
            rows.append({"instance_id": f"flash-external-{structure}-{variant}", "split": "external",
                         "task": {"family": structure, "variant": variant, "base": base, "expected": expected,
                                  "prerequisites": prerequisites},
                         "timing": {"lag": lag, "settle": settle, "bound": lag + settle}})
    return rows
