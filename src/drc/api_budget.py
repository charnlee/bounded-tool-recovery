"""Durable cross-process API reservations, denominated in integer micro-USD."""
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path


class BudgetLimit(Exception):
    pass


class ApiBudget:
    def __init__(self, path, config_path):
        self.path = Path(path)
        self.config_path = Path(config_path)
        self.config = json.loads(self.config_path.read_text())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY, episode TEXT, reserved INTEGER, charged INTEGER,
                phase TEXT, created TEXT, usage TEXT, error TEXT)""")

    def micro_cost(self, input_tokens, output_tokens):
        rates = self.config["conservative_usd_per_million"]
        # USD per million tokens equals micro-USD per token.
        return int((Decimal(str(rates["all_input"])) * input_tokens
                    + Decimal(str(rates["all_output"])) * output_tokens).to_integral_value(rounding=ROUND_CEILING))

    def reserve(self, episode, input_upper, output_upper):
        if type(input_upper) is not int or type(output_upper) is not int or min(input_upper, output_upper) < 0:
            raise ValueError("Invalid token upper bound")
        amount = self.micro_cost(input_upper, output_upper)
        threshold = int(Decimal(str(self.config["local_stop_threshold_usd"])) * 1_000_000)
        if threshold > int(Decimal(str(self.config["limit"])) * 1_000_000):
            raise ValueError("Local threshold exceeds authorized limit")
        request_id = str(uuid.uuid4())
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute("BEGIN IMMEDIATE")
            used = db.execute("SELECT COALESCE(SUM(charged),0) FROM requests").fetchone()[0]
            if used + amount > threshold:
                raise BudgetLimit("Cumulative API budget reservation would exceed local stop threshold")
            db.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?)", (
                request_id, episode, amount, amount, "reserved",
                datetime.now(timezone.utc).isoformat(), None, None))
        return request_id

    def settle(self, request_id, usage=None, error=None):
        valid = isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0
                                               for k in ("input_tokens", "output_tokens"))
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT reserved,phase FROM requests WHERE id=?", (request_id,)).fetchone()
            if row is None or row[1] != "reserved":
                raise ValueError("Unknown or already-settled request")
            charge = self.micro_cost(usage["input_tokens"], usage["output_tokens"]) if valid else row[0]
            db.execute("UPDATE requests SET charged=?,phase=?,usage=?,error=? WHERE id=?", (
                charge, "usage_recorded" if valid else "reservation_retained",
                json.dumps(usage) if usage is not None else None, error, request_id))
        return charge

    def report(self):
        with sqlite3.connect(self.path, timeout=30) as db:
            records = [dict(zip(("id", "episode", "reserved_micro_usd", "charged_micro_usd", "phase", "created_at", "usage", "error"), row))
                       for row in db.execute("SELECT * FROM requests ORDER BY created,id")]
        for r in records:
            if r["usage"] is not None:
                r["usage"] = json.loads(r["usage"])
        charged = sum(r["charged_micro_usd"] for r in records)
        return {"currency": "USD", "authorized_limit": self.config["limit"],
                "local_stop_threshold": self.config["local_stop_threshold_usd"],
                "conservative_charged_or_reserved_usd": charged / 1_000_000,
                "requests": len(records), "unresolved_reservations": sum(r["phase"] == "reserved" for r in records),
                "note": "Conservative local estimate, not a provider invoice; failures without usage retain their full reservation.",
                "records": records}
