"""DeepSeek-only client; no retries, shared historical USD wallet, stage caps."""
import hashlib
import json
import os
import re
import shlex
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .api_budget import ApiBudget, BudgetLimit

ROOT = Path(__file__).resolve().parents[2]
MODEL = "deepseek-flash"
ENDPOINT = "https://api.deepseek.com/chat/completions"


def load_key(path):
    keys = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            if key.strip().lower().replace("-", "_") == "deepseek_api_key":
                tokens = shlex.split(value, comments=True)
                if len(tokens) != 1:
                    raise ValueError("Malformed DeepSeek credential assignment")
                keys.append(tokens[0])
    if len(keys) != 1 or not keys[0]:
        raise ValueError("Expected one nonempty DeepSeek credential")
    return keys[0]


class FlashBudget(ApiBudget):
    CAPS = {"dev": 3, "main": 25, "cap": 12, "external": 10, "supplement": 5}

    def __init__(self, stage, path=None):
        super().__init__(path or ROOT / "results/tmlr-api-budget/budget.sqlite3",
                         ROOT / "configs/flash-budget.json")
        if stage not in self.CAPS:
            raise ValueError("Unknown stage")
        self.stage = stage
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS flash_requests (id TEXT PRIMARY KEY, stage TEXT, input_upper INTEGER, output_upper INTEGER)")

    def reserve(self, episode, input_upper, output_upper):
        if any(type(x) is not int or x < 0 for x in (input_upper, output_upper)):
            raise ValueError("Invalid reservation")
        amount = self.micro_cost(input_upper, output_upper)
        ident = str(uuid.uuid4())
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute("BEGIN IMMEDIATE")
            total = db.execute("SELECT COALESCE(SUM(charged),0) FROM requests").fetchone()[0]
            stage_total = db.execute("SELECT COALESCE(SUM(r.charged),0) FROM requests r JOIN flash_requests f ON r.id=f.id WHERE f.stage=?", (self.stage,)).fetchone()[0]
            if total + amount > 90_000_000 or stage_total + amount > self.CAPS[self.stage] * 1_000_000:
                raise BudgetLimit("Cumulative or stage ceiling reached")
            db.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?)", (ident, episode, amount, amount, "reserved", datetime.now(timezone.utc).isoformat(), None, None))
            db.execute("INSERT INTO flash_requests VALUES (?,?,?,?)", (ident, self.stage, input_upper, output_upper))
        return ident


class FlashClient:
    def __init__(self, budget, episode_id, journal_dir, max_tokens=1024, prompt=None):
        self.key = load_key(ROOT.parent / ".env")
        self.budget, self.episode_id = budget, episode_id
        self.journal_dir = Path(journal_dir)
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        self.max_tokens = max_tokens
        self.prompt = prompt or (ROOT / "prompts/flash_recovery.md").read_text()
        self.calls = []
        self.local_error = None
        self.fatal = False

    def save(self, entry):
        text = json.dumps(entry, ensure_ascii=True, sort_keys=True) + "\n"
        if self.key in text:
            raise RuntimeError("Credential detected in journal")
        path = self.journal_dir / (entry["budget_request_id"] + ".json")
        temp = path.with_suffix(".tmp")
        temp.write_text(text)
        os.replace(temp, path)

    def ask(self, public, validate_action=True):
        messages = [{"role": "system", "content": self.prompt},
                    {"role": "user", "content": json.dumps(public, sort_keys=True)}]
        payload = {"model": MODEL, "messages": messages, "temperature": 0,
                   "thinking": {"type": "disabled"}, "max_tokens": self.max_tokens,
                   "response_format": {"type": "json_object"}, "stream": False}
        data = json.dumps(payload).encode()
        if len(data) > 24000:
            self.local_error = "input_admission_limit"
            return None
        rid = self.budget.reserve(self.episode_id, len(data) + 2048, self.max_tokens)
        entry = {"budget_request_id": rid, "provider": "deepseek", "stage": self.budget.stage,
                 "endpoint": ENDPOINT, "episode_id": self.episode_id,
                 "request": payload, "payload_sha256": hashlib.sha256(data).hexdigest(),
                 "status": "pending", "started_at": datetime.now(timezone.utc).isoformat()}
        self.calls.append(entry)
        self.save(entry)
        start, usage, error = time.monotonic(), None, None
        try:
            request = urllib.request.Request(ENDPOINT, data=data, headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.key})
            with urllib.request.urlopen(request, timeout=60) as response:
                result = json.load(response)
            raw_usage = result.get("usage", {})
            if all(type(raw_usage.get(k)) is int and raw_usage[k] >= 0 for k in ("prompt_tokens", "completion_tokens")):
                usage = {"input_tokens": raw_usage["prompt_tokens"], "output_tokens": raw_usage["completion_tokens"], "total_tokens": raw_usage["prompt_tokens"] + raw_usage["completion_tokens"]}
            entry.update(response=result, response_model=result.get("model"), usage=usage or {})
            choice = result["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ValueError("non_stop_finish")
            obj = json.loads(choice["message"]["content"])
            if not isinstance(obj, dict):
                raise ValueError("non_object_response")
            if validate_action:
                valid = (set(obj) == {"action", "indices", "extra_ticks", "state_hypothesis", "reason"}
                         and obj["action"] in ("QUERY", "WAIT", "WRITE", "COMPLETE", "ABORT")
                         and type(obj["indices"]) is list
                         and all(type(i) is int for i in obj["indices"])
                         and type(obj["extra_ticks"]) is int
                         and obj["state_hypothesis"] in ("NO_OP", "PARTIAL", "COMMITTED", "UNKNOWN", "MISMATCH")
                         and isinstance(obj["reason"], str))
                if not valid:
                    raise ValueError("invalid_action_schema")
            entry["status"] = "ok"
            return obj
        except Exception as exc:
            error = type(exc).__name__
            entry.update(status="error", error_type=error)
            if isinstance(exc, urllib.error.HTTPError):
                entry["http_status"] = exc.code
                try:
                    body = json.loads(exc.read()).get("error", {})
                    safe = {k: str(body[k])[:1000].replace(self.key, "[REDACTED]") for k in ("type", "code", "message") if k in body}
                    entry["provider_error"] = {k: re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", v) for k, v in safe.items()}
                except Exception:
                    entry["provider_error"] = {"message": "unavailable"}
                self.fatal = exc.code in (401, 402, 403)
            elif isinstance(exc, ValueError):
                entry["validation_error"] = str(exc)[:150]
            return None
        finally:
            entry["latency_seconds"] = time.monotonic() - start
            entry["conservative_charge_micro_usd"] = self.budget.settle(rid, usage, error)
            self.save(entry)
