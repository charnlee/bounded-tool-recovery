"""Disk-backed contract checks. Timing is simulated; SQL effects are committed."""

import json
import sqlite3
from collections import Counter

FAMILIES = ("message", "inventory", "settings", "transfer")
FAULTS = ("clean", "before", "partial", "after", "late", "false_ack", "recovery_partial")
CONTRACTS = {
    "fresh": {"lag": 0, "settle": 3, "bound": 3},
    "stale": {"lag": 3, "settle": 3, "bound": 6},
    "unknown": {"lag": 3, "settle": 3, "bound": None},
    "violated": {"lag": 3, "settle": 3, "bound": 0},
}
METHODS = ("ack_only", "retry_all", "bounded_verify")


class ToolBudgetExceeded(Exception):
    pass


def make_task(family, variant):
    base = [100 + variant, 80 + variant]
    if family == "message":
        base = [[], []]
        expected = [[f"message-{variant}-{i}"] for i in range(2)]
    elif family == "settings":
        base = ["old-A", "old-B"]
        expected = [f"new-{variant}-A", f"new-{variant}-B"]
    elif family == "inventory":
        expected = [x + 3 + variant for x in base]
    elif family == "transfer":
        expected = [base[0] - 7 - variant, base[1] + 7 + variant]
    else:
        raise ValueError(family)
    return {"family": family, "variant": variant, "base": base, "expected": expected}


class Service:
    """Evaluator-owned service; controllers receive call(), not its private state."""

    def __init__(self, path, task, fault, contract):
        self.path, self.task, self.fault = path, task, fault
        self.lag, self.settle = contract["lag"], contract["settle"]
        self.clock = self.writes = 0
        self.pending, self.events = [], []
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE objects (k INTEGER PRIMARY KEY, value);
            CREATE TABLE messages (seq INTEGER PRIMARY KEY, target INTEGER, payload TEXT);
            CREATE TABLE effects (seq INTEGER PRIMARY KEY, op INTEGER, tick INTEGER);
            CREATE TABLE snapshots (seq INTEGER PRIMARY KEY, tick INTEGER, data TEXT);
        """)
        self.db.executemany("INSERT INTO objects VALUES (?, ?)",
                            [(i, v if not isinstance(v, list) else "unused")
                             for i, v in enumerate(task["base"])])
        self.db.execute("INSERT INTO objects VALUES (99, 'untouched')")
        self.snapshot(-100)
        self.db.commit()

    def state(self):
        if self.task["family"] == "message":
            values = [[r[0] for r in self.db.execute(
                "SELECT payload FROM messages WHERE target=? ORDER BY seq", (i,))]
                for i in range(2)]
        else:
            values = [r[0] for r in self.db.execute(
                "SELECT value FROM objects WHERE k IN (0,1) ORDER BY k")]
        return {"values": values, "control": self.db.execute(
            "SELECT value FROM objects WHERE k=99").fetchone()[0]}

    def snapshot(self, tick):
        self.db.execute("INSERT INTO snapshots(tick,data) VALUES (?,?)",
                        (tick, json.dumps(self.state(), sort_keys=True)))

    def apply(self, op, tick):
        t = self.task
        with self.db:
            if t["family"] == "message":
                self.db.execute("INSERT INTO messages(target,payload) VALUES (?,?)",
                                (op, t["expected"][op][0]))
            elif t["family"] == "settings":
                self.db.execute("UPDATE objects SET value=? WHERE k=?",
                                (t["expected"][op], op))
            else:
                self.db.execute("UPDATE objects SET value=value+? WHERE k=?",
                                (t["expected"][op] - t["base"][op], op))
            self.db.execute("INSERT INTO effects(op,tick) VALUES (?,?)", (op, tick))
            self.snapshot(tick)

    def drain(self):
        due = sorted((x for x in self.pending if x[0] <= self.clock), key=lambda x: x[0])
        self.pending = [x for x in self.pending if x[0] > self.clock]
        for tick, ops in due:
            for op in ops:
                self.apply(op, tick)

    def call(self, action, argument=None):
        if len(self.events) >= 10:
            raise ToolBudgetExceeded("tool budget")
        self.clock += 1 + (argument if action == "wait" else 0)
        self.drain()
        if action == "query":
            row = self.db.execute(
                "SELECT data FROM snapshots WHERE tick<=? ORDER BY tick DESC,seq DESC LIMIT 1",
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
                selected = ops[:1] if fault in ("partial", "recovery_partial") else ops
                for op in selected:
                    self.apply(op, self.clock)
            result = {"status": "ok" if fault in ("clean", "false_ack") else "timeout"}
        else:
            raise ValueError(action)
        self.events.append({"action": action, "argument": argument,
                            "tick": self.clock, "result": result})
        return result

    def finish(self):
        controller_ticks = self.clock
        if self.pending:
            self.clock = max(x[0] for x in self.pending)
            self.drain()
        final = self.state()
        ledger = [dict(zip(("seq", "op", "tick"), r)) for r in self.db.execute(
            "SELECT seq,op,tick FROM effects ORDER BY seq")]
        self.db.close()
        self.db = sqlite3.connect(self.path)
        reopened = self.state()
        reopened_ledger = [dict(zip(("seq", "op", "tick"), r)) for r in self.db.execute(
            "SELECT seq,op,tick FROM effects ORDER BY seq")]
        self.db.close()
        assert reopened == final and reopened_ledger == ledger
        return {"final": final, "ledger": ledger, "pending_after": len(self.pending),
                "durable_reopen_equal": True, "controller_ticks": controller_ticks}


def control(task, bound, method, call):
    """No fault labels, actual timings or hidden effects cross this interface."""
    call("query")
    ack = call("write", [0, 1])
    if method == "ack_only":
        return "complete" if ack["status"] == "ok" else "abstain"
    if method == "retry_all":
        if ack["status"] != "ok":
            ack = call("write", [0, 1])
        return "complete" if ack["status"] == "ok" else "abstain"
    if method != "bounded_verify":
        raise ValueError(method)
    if bound is None:
        return "abstain"
    # Each round starts immediately after a write. Query itself costs one tick.
    for _ in range(3):
        if bound > 1:
            call("wait", max(0, bound - 2))
        observed = call("query")
        missing = []
        for i, value in enumerate(observed["values"]):
            if value == task["expected"][i]:
                continue
            if value != task["base"][i]:
                return "abstain"
            missing.append(i)
        if not missing:
            return "complete"
        call("write", missing)
    return "budget_exhausted"


def evaluate(record):
    """Recompute from persisted state and effects, independently of policy evidence."""
    end = record["evaluator_only"]
    counts = Counter(x["op"] for x in end["ledger"])
    goal = end["final"]["values"] == record["task"]["expected"]
    goal = goal and end["final"]["control"] == "untouched"
    safe = all(counts[i] == 1 for i in range(2)) and set(counts) <= {0, 1}
    declared = record["decision"] == "complete"
    duplicates = sum(max(0, c - 1) for c in counts.values())
    return {"scts": bool(goal and safe and declared), "goal": bool(goal),
            "exact_effects": safe, "duplicate_effects": duplicates,
            "missing_items": sum(counts[i] == 0 for i in range(2)),
            "false_completion": bool(declared and not (goal and safe)),
            "goal_but_unsafe": bool(goal and not safe),
            "abstain": record["decision"] == "abstain",
            "tool_calls": len(record["visible_events"]),
            "ticks": end["controller_ticks"]}


def run_episode(path, family, variant, fault, contract_name, method):
    task, contract = make_task(family, variant), CONTRACTS[contract_name]
    service = Service(path, task, fault, contract)
    try:
        decision = control(task, contract["bound"], method, service.call)
    except ToolBudgetExceeded:
        decision = "budget_exhausted"
    record = {"run_id": f"{family}-{variant}-{fault}-{contract_name}-{method}",
              "family": family, "variant": variant, "fault": fault,
              "contract": contract_name, "method": method, "task": task,
              "public_bound": contract["bound"], "decision": decision,
              "visible_events": service.events, "evaluator_only": service.finish()}
    record["metrics"] = evaluate(record)
    return record
