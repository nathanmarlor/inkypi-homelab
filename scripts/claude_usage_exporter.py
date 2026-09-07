#!/usr/bin/env python3
"""Serve Claude Code (and, if present, Codex CLI) token usage as JSON for the InkyPi AI Usage screen.

Claude Code writes every assistant turn, with its token usage, to ~/.claude/projects/**/*.jsonl.
Those files only exist on the machine Claude Code runs on, so this script runs there and exposes
an aggregate over HTTP that the InkyPi (or anything else) can poll:

    GET http://<host>:8765/usage.json

or, so the display device stays standalone, pushed to it over SSH after every rebuild:

    --push pi@inkypi:/home/pi/InkyPi/src/config/usage.json

Costs are API-list-price equivalents (what the same tokens would cost on the Claude API), which is
the useful number when the real bill is a flat subscription.

Usage:
    python3 scripts/claude_usage_exporter.py [--port 8765] [--days 7] [--once]

Files are re-parsed only when their size or mtime changes, so steady-state cost is tiny.
"""
import argparse
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("claude-usage")

CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
CLAUDE_DIR = CLAUDE_HOME / "projects"
CLAUDE_CREDENTIALS = CLAUDE_HOME / ".credentials.json"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
CODEX_DIR = CODEX_HOME / "sessions"
LIMITS_TTL = 120  # seconds between calls to the rate-limit endpoint

# USD per million tokens: (input, output, cache_read, cache_write_5m, cache_write_1h).
# Matched by prefix, longest match wins.
PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 0.25, 12.5, 20.0),
    "claude-mythos-5-1": (10.0, 50.0, 0.25, 12.5, 20.0),
    "claude-fable-5": (10.0, 50.0, 1.0, 12.5, 20.0),
    "claude-opus-5": (5.0, 25.0, 0.5, 6.25, 10.0),
    "claude-opus-4": (5.0, 25.0, 0.5, 6.25, 10.0),
    "claude-sonnet-5": (2.0, 10.0, 0.2, 2.5, 4.0),
    "claude-sonnet-4-6": (3.0, 15.0, 0.3, 3.75, 6.0),
    "claude-sonnet-4": (3.0, 15.0, 0.3, 3.75, 6.0),
    "claude-haiku-4-5": (1.0, 5.0, 0.1, 1.25, 2.0),
    "claude-haiku": (1.0, 5.0, 0.1, 1.25, 2.0),
}
DEFAULT_PRICE = PRICES["claude-opus-5"]

# Codex CLI models (OpenAI list prices, USD per million: input, output, cached input).
CODEX_PRICES = {
    "gpt-5.5": (2.5, 15.0, 0.25),
    "gpt-5": (1.25, 10.0, 0.125),
    "o3": (2.0, 8.0, 0.5),
    "o4-mini": (1.1, 4.4, 0.275),
}
CODEX_DEFAULT = CODEX_PRICES["gpt-5"]


def price_for(model):
    best = None
    for prefix in PRICES:
        if model.startswith(prefix) and (best is None or len(prefix) > len(best)):
            best = prefix
    return PRICES[best] if best else DEFAULT_PRICE


def codex_price_for(model):
    for prefix, price in sorted(CODEX_PRICES.items(), key=lambda kv: -len(kv[0])):
        if model.startswith(prefix):
            return price
    return CODEX_DEFAULT


def local_day(ts_iso):
    """Return the local calendar date (YYYY-MM-DD) for an ISO timestamp."""
    ts = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    return ts.astimezone().strftime("%Y-%m-%d")


def new_bucket():
    return {
        "input": 0, "output": 0, "thinking": 0, "cache_read": 0, "cache_write": 0,
        "messages": 0, "cost_usd": 0.0, "sessions": set(), "by_model": defaultdict(lambda: {"tokens": 0, "cost_usd": 0.0, "messages": 0}),
    }


class ClaudeFile:
    """Per-file parsed aggregate keyed by local day, re-parsed only when the file changes."""

    def __init__(self, path):
        self.path = path
        self.sig = None
        self.days = {}

    def refresh(self):
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self.days = {}
            return
        sig = (st.st_size, st.st_mtime_ns)
        if sig == self.sig:
            return
        self.sig = sig
        self.days = self.parse()

    def parse(self):
        days = {}
        seen = set()
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"usage"' not in line or '"assistant"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("type") != "assistant":
                        continue
                    msg = rec.get("message") or {}
                    usage = msg.get("usage")
                    if not usage:
                        continue
                    key = (msg.get("id"), rec.get("requestId"))
                    if key in seen:
                        continue  # streamed messages are written once per content block
                    seen.add(key)

                    model = msg.get("model") or "unknown"
                    if model.startswith("<"):
                        continue  # <synthetic> entries carry no billable tokens
                    ts = rec.get("timestamp")
                    if not ts:
                        continue
                    day = local_day(ts)
                    b = days.setdefault(day, new_bucket())

                    inp = usage.get("input_tokens", 0) or 0
                    out = usage.get("output_tokens", 0) or 0
                    think = ((usage.get("output_tokens_details") or {}).get("thinking_tokens", 0)) or 0
                    cr = usage.get("cache_read_input_tokens", 0) or 0
                    cc = usage.get("cache_creation") or {}
                    cw5 = cc.get("ephemeral_5m_input_tokens")
                    cw1 = cc.get("ephemeral_1h_input_tokens")
                    if cw5 is None and cw1 is None:
                        cw5, cw1 = usage.get("cache_creation_input_tokens", 0) or 0, 0
                    cw5, cw1 = cw5 or 0, cw1 or 0

                    p_in, p_out, p_cr, p_cw5, p_cw1 = price_for(model)
                    cost = (inp * p_in + out * p_out + cr * p_cr + cw5 * p_cw5 + cw1 * p_cw1) / 1_000_000

                    b["input"] += inp
                    b["output"] += out
                    b["thinking"] += think
                    b["cache_read"] += cr
                    b["cache_write"] += cw5 + cw1
                    b["messages"] += 1
                    b["cost_usd"] += cost
                    if rec.get("sessionId"):
                        b["sessions"].add(rec["sessionId"])
                    bm = b["by_model"][model]
                    bm["tokens"] += inp + out + cr + cw5 + cw1
                    bm["cost_usd"] += cost
                    bm["messages"] += 1
        except OSError as e:
            log.warning("could not read %s: %s", self.path, e)
        return days


class CodexFile(ClaudeFile):
    """Codex CLI rollout files: token_count events carry cumulative and last-turn usage."""

    def parse(self):
        days = {}
        model = "gpt-5"
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"token_count"' not in line and '"model"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = rec.get("payload") or {}
                    if payload.get("model"):
                        model = payload["model"]
                    if payload.get("type") != "token_count":
                        continue
                    last = ((payload.get("info") or {}).get("last_token_usage")) or {}
                    if not last:
                        continue
                    ts = rec.get("timestamp")
                    if not ts:
                        continue
                    day = local_day(ts)
                    b = days.setdefault(day, new_bucket())
                    inp = last.get("input_tokens", 0) or 0
                    cached = last.get("cached_input_tokens", 0) or 0
                    out = last.get("output_tokens", 0) or 0
                    think = last.get("reasoning_output_tokens", 0) or 0
                    p_in, p_out, p_cached = codex_price_for(model)
                    cost = ((inp - cached) * p_in + cached * p_cached + out * p_out) / 1_000_000
                    b["input"] += inp - cached
                    b["cache_read"] += cached
                    b["output"] += out
                    b["thinking"] += think
                    b["messages"] += 1
                    b["cost_usd"] += cost
                    b["sessions"].add(self.path.stem)
                    bm = b["by_model"][model]
                    bm["tokens"] += inp + out
                    bm["cost_usd"] += cost
                    bm["messages"] += 1
        except OSError as e:
            log.warning("could not read %s: %s", self.path, e)
        return days


def fetch_claude_limits():
    """Return the same plan limits Claude Code's /usage screen shows, using the local login token.

    The credentials file is re-read on every call because Claude Code rewrites it when it
    refreshes the token. If the token has expired and Claude Code has not run since, the
    endpoint returns 401 and we report the limits as unavailable rather than guessing.
    """
    import urllib.error
    import urllib.request

    try:
        creds = json.loads(CLAUDE_CREDENTIALS.read_text())["claudeAiOauth"]
    except (OSError, KeyError, json.JSONDecodeError) as e:
        return {"available": False, "reason": f"no credentials ({e.__class__.__name__})"}

    req = urllib.request.Request(
        CLAUDE_USAGE_URL,
        headers={
            "Authorization": f"Bearer {creds.get('accessToken', '')}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "inkypi-claude-usage-exporter",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        return {"available": False, "reason": f"HTTP {e.code}"}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        return {"available": False, "reason": str(e)[:80]}

    limits = []
    for lim in data.get("limits") or []:
        scope = lim.get("scope") or {}
        model = (scope.get("model") or {}).get("display_name")
        kind = lim.get("kind")
        if kind == "session":
            label = "Session"
        elif kind == "weekly_all":
            label = "Weekly · all models"
        elif model:
            label = f"Weekly · {model}"
        else:
            label = kind or "Limit"
        limits.append({
            "kind": kind,
            "label": label,
            "percent": lim.get("percent"),
            "severity": lim.get("severity"),
            "resets_at": lim.get("resets_at"),
            "is_active": lim.get("is_active"),
            "model": model,
        })

    tier = creds.get("rateLimitTier") or ""
    plan = (creds.get("subscriptionType") or "").title()
    if "20x" in tier:
        plan += " 20x"
    elif "5x" in tier:
        plan += " 5x"
    extra = data.get("extra_usage") or {}
    return {
        "available": True,
        "plan": plan.strip(),
        "limits": limits,
        "extra_usage_enabled": bool(extra.get("is_enabled")),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


class UsageStore:
    def __init__(self, days):
        self.days = days
        self.claude_files = {}
        self.codex_files = {}
        self.lock = threading.Lock()
        self.snapshot = None
        self.limits = None
        self.limits_at = 0.0

    def _scan(self, root, registry, cls):
        if not root.exists():
            return
        cutoff = time.time() - (self.days + 1) * 86400
        live = set()
        for path in root.rglob("*.jsonl"):
            try:
                if path.stat().st_mtime < cutoff:
                    continue
            except FileNotFoundError:
                continue
            live.add(path)
            registry.setdefault(path, cls(path)).refresh()
        for gone in set(registry) - live:
            del registry[gone]

    def _merge(self, registry, wanted_days):
        totals = {d: new_bucket() for d in wanted_days}
        for f in registry.values():
            for day, b in f.days.items():
                if day not in totals:
                    continue
                t = totals[day]
                for k in ("input", "output", "thinking", "cache_read", "cache_write", "messages", "cost_usd"):
                    t[k] += b[k]
                t["sessions"] |= b["sessions"]
                for model, m in b["by_model"].items():
                    tm = t["by_model"][model]
                    for k in m:
                        tm[k] += m[k]
        return totals

    @staticmethod
    def _serialise(b):
        by_model = sorted(
            ({"model": m, **v, "cost_usd": round(v["cost_usd"], 4)} for m, v in b["by_model"].items()),
            key=lambda x: -x["cost_usd"],
        )
        return {
            "input": b["input"], "output": b["output"], "thinking": b["thinking"],
            "cache_read": b["cache_read"], "cache_write": b["cache_write"],
            "total": b["input"] + b["output"] + b["cache_read"] + b["cache_write"],
            "messages": b["messages"], "sessions": len(b["sessions"]),
            "cost_usd": round(b["cost_usd"], 4), "by_model": by_model,
        }

    def rebuild(self):
        started = time.time()
        today = datetime.now().date()
        wanted = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(self.days)][::-1]

        self._scan(CLAUDE_DIR, self.claude_files, ClaudeFile)
        self._scan(CODEX_DIR, self.codex_files, CodexFile)
        claude = self._merge(self.claude_files, wanted)
        codex = self._merge(self.codex_files, wanted)
        codex_available = bool(self.codex_files) and any(b["messages"] for b in codex.values())

        if time.time() - self.limits_at > LIMITS_TTL:
            self.limits = fetch_claude_limits()
            self.limits_at = time.time()
            if not self.limits.get("available"):
                log.warning("claude limits unavailable: %s", self.limits.get("reason"))

        snap = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "host": os.uname().nodename,
            "days": wanted,
            "claude": {
                "available": bool(self.claude_files),
                "limits": self.limits,
                "today": self._serialise(claude[wanted[-1]]),
                "daily": [{"date": d, **self._serialise(claude[d])} for d in wanted],
            },
            "codex": {
                "available": codex_available,
                "limits": {"available": False, "reason": "no Codex login on this host"},
                "today": self._serialise(codex[wanted[-1]]),
                "daily": [{"date": d, **self._serialise(codex[d])} for d in wanted],
            },
        }
        with self.lock:
            self.snapshot = snap
        log.info("rebuilt in %.1fs: %d claude files, %d codex files, today $%.2f",
                 time.time() - started, len(self.claude_files), len(self.codex_files), snap["claude"]["today"]["cost_usd"])
        return snap

    def get(self):
        with self.lock:
            return self.snapshot


def make_handler(store):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] not in ("/", "/usage.json"):
                self.send_error(404)
                return
            snap = store.get() or store.rebuild()
            body = json.dumps(snap).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--interval", type=int, default=120, help="seconds between rescans")
    ap.add_argument("--once", action="store_true", help="print JSON and exit")
    ap.add_argument("--push", action="append", default=[], metavar="USER@HOST:PATH", help="scp the JSON to this destination after every rebuild (repeatable)")
    args = ap.parse_args()

    store = UsageStore(args.days)
    if args.once:
        print(json.dumps(store.rebuild(), indent=2))
        return

    def push(snapshot):
        for dest in args.push:
            try:
                with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
                    json.dump(snapshot, fh)
                    tmp = fh.name
                subprocess.run(["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", tmp, dest], check=True, timeout=30)
                log.info("pushed usage to %s", dest)
            except Exception as e:
                log.warning("push to %s failed: %s", dest, e)
            finally:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass

    def loop():
        while True:
            try:
                push(store.rebuild())
            except Exception:
                log.exception("rebuild failed")
            time.sleep(args.interval)

    threading.Thread(target=loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(store))
    log.info("serving on :%d", args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
