#!/usr/bin/env python3
"""Balance-layer test: the money-balance card kind, the provider registry, and the logos.

No provider credential is needed. Each adapter is pointed at a local stub that replays
the response recorded in tests/fixtures/providers/, so this exercises the parsing and
the card-shape decisions — not the network.

What is asserted here is the behaviour that must hold for every balance provider:
  * a money balance never renders as a percentage (there is no cap to divide by)
  * an unrecognised shape is an explicit error state, never a fabricated 0
  * money reserved in flight is reported, and `available` (not `balance`) is the figure
  * a multi-currency balance picks one currency deterministically
  * every registered provider resolves to a logo file that exists on disk
  * the z.ai HTTP-200-with-success:false trap is caught, not read as a valid payload

Note on imports: this file is meant to be run directly, in a fresh interpreter. Do not
import it from a long-lived process that already has providers_balance imported — a warm
module keeps serving pre-edit code and silently masks a fix (measured while building
this).
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures", "providers")
sys.path.insert(0, ROOT)

import app  # noqa: E402
import providers_balance as pb  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, ("" if ok else " — " + detail)))
    if not ok:
        failures.append(name)


def load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


ROUTES = {}


class Stub(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path not in ROUTES:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps(ROUTES[path]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def serve():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


# Fixtures built in this file (documented shapes the vendors publish but that are not
# worth a file each).
OR_UNCAPPED = {
    "data": {
        "label": "sk-or-v1-uncapped",
        "limit": None,
        "limit_remaining": None,
        "usage": 3.5,
        "usage_daily": 0.4,
        "usage_weekly": 3.5,
        "usage_monthly": 3.5,
        "is_free_tier": False,
    }
}
CI_SETTLED = {
    "object": "account.balance",
    "balance_usd": 0.0,
    "available_usd": 0.0,
    "reserved_usd": 0.0,
    "currency": "USD",
    "auto_recharge": {"enabled": False},
}
CI_RESERVED = {
    "object": "account.balance",
    "balance_usd": 40.0,
    "available_usd": 37.5,
    "reserved_usd": 2.5,
    "currency": "USD",
    "auto_recharge": {"enabled": True},
}


def run(provider, routes, base):
    ROUTES.clear()
    ROUTES.update(routes)
    acct = {"id": "%s-1" % provider, "provider": provider, "label": provider, "token": "t"}
    return app._annotate(acct, pb.BALANCE_FETCHERS[provider](acct))


def window(res, key):
    for win in res["windows"]:
        if win["key"] == key:
            return win
    return None


def main():
    srv, base = serve()
    # Every adapter is pointed at the stub: the base is a bare host, so the relative
    # paths the real APIs use (/v1/credits, /api/v1/key, …) are replayed unchanged.
    pb.OPENROUTER_BASE = base
    pb.CHEAPERINFERENCE_BASE = base
    pb.DEEPSEEK_BASE = base
    pb.MOONSHOT_BASE = base
    pb.SYNTHETIC_BASE = base
    pb.ZAI_BASE = base

    try:
        # ---- registry + logos -------------------------------------------------
        for provider, info in sorted(app.PROVIDERS.items()):
            logo = os.path.join(ROOT, "static", "logos", info["logo"])
            check("logo file exists for %s (%s)" % (provider, info["logo"]), os.path.exists(logo))
        check("an unknown provider falls back to the neutral glyph",
              app.provider_logo("nope") == app.LOGO_FALLBACK)
        check("a broken logo path falls back too",
              app.provider_logo("zai") == "zai.svg")
        check("an unknown provider is treated as a window provider",
              app.provider_kind("nope") == "window")

        # Every account in the shipped example must resolve a logo that exists: this is
        # the check that stops a public screenshot rendering an empty icon box.
        accts = app.load_accounts(os.path.join(ROOT, "accounts.example.json"))
        missing = [a["id"] for a in accts
                   if not os.path.exists(os.path.join(ROOT, "static", "logos", a["logo"]))]
        check("every configured example account resolves an existing logo",
              not missing, "missing for %s" % missing)

        # ---- OpenRouter: uncapped key must be a balance, never a percent ------
        res = run("openrouter",
                  {"/v1/credits": load("openrouter_credits.json"), "/v1/key": OR_UNCAPPED}, base)
        check("OpenRouter uncapped: ok", res["state"] == "ok", res.get("error") or "")
        bal = window(res, "balance")
        check("OpenRouter uncapped: emits a balance window", bal is not None)
        check("OpenRouter uncapped: no invented percentage",
              bal is not None and bal["percent"] is None)
        check("OpenRouter uncapped: reports remaining, not the credit total",
              bal is not None and abs((bal["amount"] or 0) - (40.0 - 38.388786896)) < 1e-6,
              "amount=%s" % (bal or {}).get("amount"))
        check("OpenRouter uncapped: explains why there is no percent",
              bal is not None and "no spend cap" in (bal.get("note") or ""))
        check("OpenRouter uncapped: card kind follows the window",
              res["kind"] == "balance", "kind=%s" % res["kind"])

        # ---- OpenRouter: a capped key DOES have an honest percent -------------
        res = run("openrouter",
                  {"/v1/credits": load("openrouter_credits.json"),
                   "/v1/key": load("openrouter_key.json")}, base)
        win = window(res, "key_limit")
        check("OpenRouter capped: emits a real window", win is not None and win["kind"] == "window")
        check("OpenRouter capped: percent is computed against the key limit",
              win is not None and win["percent"] == 0.0 and win["cap"] == 33.0,
              "pct=%s cap=%s" % ((win or {}).get("percent"), (win or {}).get("cap")))
        check("OpenRouter capped: account kind is window, not balance",
              res["kind"] == "window", "kind=%s" % res["kind"])
        # Regression: /key's `label` defaults to the key's own prefix, and the plan line is
        # rendered on the card. A credential fragment must never reach the panel.
        check("OpenRouter: a credential-shaped key label never becomes the plan line",
              not res["plan"].startswith("sk-") and "sk-or" not in res["plan"],
              "plan=%r" % res["plan"])

        # A user-set, human label IS still shown: the guard must not blank the plan line.
        human = dict(load("openrouter_key.json"))
        human["data"] = dict(human["data"], label="my laptop key")
        res = run("openrouter", {"/v1/credits": load("openrouter_credits.json"),
                                 "/v1/key": human}, base)
        check("OpenRouter: a human key label is still displayed",
              res["plan"] == "my laptop key", "plan=%r" % res["plan"])

        # ---- CheaperInference: reserved money must be visible -----------------
        res = run("cheaperinference", {"/v1/account/balance": CI_SETTLED}, base)
        check("CheaperInference settled: ok", res["state"] == "ok", res.get("error") or "")
        bal = window(res, "balance")
        check("CheaperInference settled: zero balance is rendered as 0, not hidden",
              bal is not None and bal["amount"] == 0.0, "amount=%s" % (bal or {}).get("amount"))
        check("CheaperInference settled: no fabricated percent",
              bal is not None and bal["percent"] is None)

        res = run("cheaperinference", {"/v1/account/balance": CI_RESERVED}, base)
        bal = window(res, "balance")
        check("CheaperInference reserved: shows available, not the gross balance",
              bal is not None and abs(bal["amount"] - 37.5) < 1e-9,
              "amount=%s" % (bal or {}).get("amount"))
        check("CheaperInference reserved: names the reserved amount",
              bal is not None and "reserved" in (bal.get("note") or ""),
              (bal or {}).get("note") or "")
        check("CheaperInference reserved: surfaces auto-recharge",
              bal is not None and "auto-recharge" in (bal.get("note") or ""))
        check("CheaperInference reserved: keeps the raw fields",
              bal is not None and (bal.get("extra") or {}).get("available_usd") == 37.5)

        # ---- DeepSeek: strings, an array, and a deterministic currency --------
        res = run("deepseek", {"/user/balance": load("deepseek_balance_cny.json")}, base)
        bal = window(res, "balance")
        check("DeepSeek CNY: parses a string amount",
              bal is not None and bal["amount"] == 110.0, "amount=%s" % (bal or {}).get("amount"))
        check("DeepSeek CNY: reports the currency it used",
              bal is not None and bal["currency"] == "CNY")

        res = run("deepseek", {"/user/balance": load("deepseek_balance_usd_dual.json")}, base)
        bal = window(res, "balance")
        check("DeepSeek dual-currency: picks USD deterministically",
              bal is not None and bal["currency"] == "USD" and bal["amount"] == 12.34,
              "%s %s" % ((bal or {}).get("currency"), (bal or {}).get("amount")))
        check("DeepSeek dual-currency: keeps the other currency visible",
              bal is not None and (bal.get("extra") or {}).get("other_currencies"),
              str((bal or {}).get("extra")))
        check("DeepSeek dual-currency: the unavailable note names the account, not the currency",
              bal is not None and "account flagged unavailable" in (bal.get("note") or ""),
              (bal or {}).get("note") or "")

        # ---- Kimi: success gate on status/code --------------------------------
        res = run("kimi", {"/v1/users/me/balance": load("moonshot_balance.json")}, base)
        bal = window(res, "balance")
        check("Kimi: ok with status=true and code=0", res["state"] == "ok", res.get("error") or "")
        check("Kimi: uses available_balance as the figure",
              bal is not None and abs(bal["amount"] - 49.58894) < 1e-9,
              "amount=%s" % (bal or {}).get("amount"))
        check("Kimi: breaks out voucher and cash",
              bal is not None and "voucher" in (bal.get("note") or "")
              and "cash" in (bal.get("note") or ""), (bal or {}).get("note") or "")

        res = run("kimi", {"/v1/users/me/balance": {"code": 401, "status": False,
                                                    "error": {"message": "auth"}}}, base)
        check("Kimi: a failed envelope is not read as a balance",
              res["state"] != "ok", "state=%s" % res["state"])

        # ---- z.ai: the HTTP 200 + success:false trap --------------------------
        res = run("zai", {"/api/monitor/usage/quota/limit": load("zai_auth_error_http200.json")}, base)
        check("z.ai: an unauthenticated 200 is caught, not parsed",
              res["state"] == "auth_error", "state=%s" % res["state"])
        check("z.ai: the trap is reported with the provider's own message",
              "Authentication parameter not received" in (res.get("error") or ""),
              (res.get("error") or "")[:90])

        res = run("zai", {"/api/monitor/usage/quota/limit": load("zai_quota_limit.json")}, base)
        check("z.ai: happy path yields windows", res["state"] == "ok", res.get("error") or "")
        labels = [w["label"] for w in res["windows"]]
        check("z.ai: token windows are labelled by duration",
              "Session" in labels and "Weekly" in labels, str(labels))
        check("z.ai: the time window is not mislabelled as a token window",
              labels.count("Session") == 1, str(labels))
        check("z.ai: percentages were derived from usage/currentValue",
              any(w["percent"] == 62.0 for w in res["windows"]),
              str([w["percent"] for w in res["windows"]]))
        check("z.ai: millisecond resets become ISO",
              all(w["resets_at"] is None or w["resets_at"].endswith("Z") for w in res["windows"]))

        # ---- Synthetic --------------------------------------------------------
        res = run("synthetic", {"/v2/quotas": load("synthetic_quotas.json")}, base)
        win = window(res, "subscription")
        check("Synthetic: percent derived from requests/limit",
              win is not None and win["percent"] == 0.0 and win["cap"] == 135.0,
              "pct=%s cap=%s" % ((win or {}).get("percent"), (win or {}).get("cap")))
        check("Synthetic: keeps the renewal stamp",
              win is not None and (win.get("resets_at") or "").startswith("2025-09-21"),
              (win or {}).get("resets_at"))

        # ---- the guard that matters: never fabricate a zero -------------------
        for provider, path, junk, expect in (
            ("deepseek", "/user/balance", {"surprise": "yes"}, "shape_unknown"),
            ("kimi", "/v1/users/me/balance", {"status": True, "code": 0, "data": {"odd": 1}},
             "shape_unknown"),
            ("zai", "/api/monitor/usage/quota/limit",
             {"success": True, "code": 200, "data": {"limits": []}}, "no_access"),
            ("synthetic", "/v2/quotas", {"unexpected": True}, "shape_unknown"),
            ("cheaperinference", "/v1/account/balance", {"nothing": "here"}, "shape_unknown"),
            ("openrouter", "/v1/credits", {"data": {"mystery": 1}}, "shape_unknown"),
        ):
            res = run(provider, {path: junk}, base)
            check("%s: an unrecognised shape is an error, not a zero (%s)"
                  % (provider, expect), res["state"] == expect, "state=%s" % res["state"])
            check("%s: the unreadable card carries no amount" % provider,
                  all(w.get("amount") is None for w in res["windows"]))

        # ---- a provider whose mark is missing must not break the card ---------
        app.PROVIDERS["__ghost__"] = {"kind": "balance", "contract": "documented",
                                      "logo": "does-not-exist.svg", "label": "Ghost"}
        ghost = app._annotate({"id": "g", "provider": "__ghost__", "label": "Ghost"},
                              {"state": "ok", "windows": []})
        check("a provider with no logo file falls back instead of breaking",
              ghost["logo"] == app.LOGO_FALLBACK, ghost["logo"])
        del app.PROVIDERS["__ghost__"]

    finally:
        srv.shutdown()

    print()
    if failures:
        print("%d check(s) FAILED: %s" % (len(failures), ", ".join(failures)))
        return 1
    print("all balance checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
