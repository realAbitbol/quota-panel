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
import socket
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
    """Replays one route table. A route is a body (answered 200) or (status, body).

    The status was hardcoded to 200 before, which made every 401/403/404 branch in every
    adapter structurally unreachable from this file.
    """

    def do_GET(self):
        path = self.path.split("?")[0]
        if path not in ROUTES:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        status, payload = ROUTES[path]
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


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
    # The published field, plus the shape the adapter used to look for as a decoy: the two
    # disagree on purpose, so this fixture can tell which one is actually read.
    "auto_recharge_enabled": False,
    "auto_recharge": {"enabled": True},
}
CI_RESERVED = {
    "object": "account.balance",
    "balance_usd": 40.0,
    "available_usd": 37.5,
    "reserved_usd": 2.5,
    "currency": "USD",
    "auto_recharge_enabled": True,
    "auto_recharge": {"enabled": False},
}


def run(provider, routes, base, token="t"):
    """Poll one account through the SERVER's own path: registry -> adapter -> annotate -> scrub.

    This used to call `pb.BALANCE_FETCHERS[provider](acct)` directly, which skipped
    `poll_account`, `load_balance_fetchers()` and the HTTP layer — so a server in which the
    registry never loaded (six providers dead, every card `unsupported`) left this file 61/61
    green. `poll_account` is the one function both the window and the balance adapters go
    through, so it is the only honest thing to test.
    """
    ROUTES.clear()
    for path, value in routes.items():
        ROUTES[path] = value if isinstance(value, tuple) else (200, value)
    acct = {"id": "%s-1" % provider, "provider": provider, "label": provider, "token": token}
    return app.poll_account(acct)


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
    app.CC_API_BASE = base
    # A base that is pointed anywhere else is a call to a real vendor: after the assignments
    # above, every `*_BASE` either module exposes must be the stub. `app` is checked too — its
    # CommandCode base was left pointing at the live API, so a test that reached a window
    # adapter would have polled the real vendor with the stub's fake token.
    strays = sorted(k for k in dir(pb) if k.endswith("_BASE") and getattr(pb, k) != base)
    check("no balance-provider base still points at a real host", not strays,
          "%s -> %s" % (strays, [getattr(pb, k) for k in strays]))
    app_strays = sorted(k for k in dir(app) if k.endswith("_BASE") and getattr(app, k) != base)
    check("no app-level provider base still points at a real host", not app_strays,
          "%s -> %s" % (app_strays, [getattr(app, k) for k in app_strays]))

    try:
        # ---- registry + logos -------------------------------------------------
        for provider, info in sorted(app.PROVIDERS.items()):
            logo = os.path.join(ROOT, "static", "logos", info["logo"])
            check("logo file exists for %s (%s)" % (provider, info["logo"]), os.path.exists(logo))
        # The registry and the adapters must stay in step: a provider with no adapter renders
        # an error card on a working install, and an adapter for an unregistered provider is
        # unreachable code. Neither was asserted anywhere.
        check("every provider in the registry is served by an adapter",
              set(app.PROVIDERS) == set(app.FETCHERS) | set(pb.BALANCE_FETCHERS),
              "registry-only=%s adapter-only=%s"
              % (sorted(set(app.PROVIDERS) - set(app.FETCHERS) - set(pb.BALANCE_FETCHERS)),
                 sorted((set(app.FETCHERS) | set(pb.BALANCE_FETCHERS)) - set(app.PROVIDERS))))
        # Every `*_BASE` in the module must have been pointed at the stub below. A seventh
        # adapter with a new base name would otherwise call the real vendor with token "t",
        # and nothing in this file would notice.
        check("every advertised provider has a base pointed at the stub",
              len([k for k in dir(pb) if k.endswith("_BASE")]) == len(pb.BALANCE_FETCHERS),
              str(sorted(k for k in dir(pb) if k.endswith("_BASE"))))
        check("an unknown provider falls back to the neutral glyph",
              app.provider_logo("nope") == app.LOGO_FALLBACK)
        # The fallback case itself is further down (a provider whose mark is missing); this
        # line asserts the opposite path, and used to claim the fallback in its name.
        check("a registered provider's own mark is returned",
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

        # The guard is a heuristic, so both directions stay pinned. The audit's probe measured
        # the old rule dropping a dated human label while keeping a UUID-shaped token: wrong
        # both ways. A label reads as words; a key does not.
        dated = dict(load("openrouter_key.json"))
        dated["data"] = dict(dated["data"], label="personal-laptop-key-20260101")
        res = run("openrouter", {"/v1/credits": load("openrouter_credits.json"),
                                 "/v1/key": dated}, base)
        check("OpenRouter: a dated human label survives the credential guard",
              res["plan"] == "personal-laptop-key-20260101", "plan=%r" % res["plan"])

        for label, is_secret in (("my laptop key", False),
                                 ("personal-laptop-key-20260101", False),
                                 ("Production-Workspace-2024", False),
                                 ("team-prod-2026-01-02", False),
                                 ("prod-20260101", False),
                                 ("123e4567-e89b-12d3-a456-426614174000", True),
                                 ("aB3xK9mQ7pR2tV5wY8zL1nH4", True),
                                 ("sk-or-v1-9f2c", True)):
            check("credential guard reads %r as a %s" % (label, "secret" if is_secret else "name"),
                  pb._looks_like_credential(label) is is_secret,
                  "dropped=%s" % pb._looks_like_credential(label))

        uuidish = dict(load("openrouter_key.json"))
        uuidish["data"] = dict(uuidish["data"], label="123e4567-e89b-12d3-a456-426614174000")
        res = run("openrouter", {"/v1/credits": load("openrouter_credits.json"),
                                 "/v1/key": uuidish}, base)
        check("OpenRouter: a UUID-shaped label never becomes the plan line",
              res["plan"] != "123e4567-e89b-12d3-a456-426614174000", "plan=%r" % res["plan"])

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
        # `label` is a name in every other window ("Week", "5 h"): a consumer printing the
        # label must not get a currency, and the UI formats the amount from `currency`.
        check("a balance window is labelled with a name, not a currency",
              bal is not None and bal["label"] == "Balance",
              "label=%r currency=%r" % ((bal or {}).get("label"), (bal or {}).get("currency")))

        # ---- a 404 is a route that was not found, not a guessed cause ---------
        res = run("cheaperinference", {"/v1/account/balance": (404, {"error": "not found"})}, base)
        check("CheaperInference 404: state is no_access", res["state"] == "no_access",
              str(res.get("state")))
        err = res.get("error") or ""
        check("CheaperInference 404: names every cause it cannot tell apart",
              "route was not found" in err and "base URL" in err and "moved or renamed" in err
              and "plan" in err, err)

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

        # `is_available` is required by the schema, so its absence means shape drift — and
        # drift is where a fabricated `false` is worst: "unknown" published as a confident
        # "this account is flagged unavailable".
        absent = load("deepseek_balance_usd_dual.json")
        absent.pop("is_available", None)
        res = run("deepseek", {"/user/balance": absent}, base)
        bal = window(res, "balance")
        check("DeepSeek without is_available still reads the balance",
              bal is not None and bal["amount"] == 12.34, "amount=%s" % (bal or {}).get("amount"))
        check("DeepSeek without is_available publishes no verdict on the account",
              bal is not None and "is_available" not in (bal.get("extra") or {}),
              str((bal or {}).get("extra")))
        check("DeepSeek without is_available does not claim the account is unavailable",
              bal is not None and "unavailable" not in (bal.get("note") or ""),
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
        # `requests` is 41 of 135 on purpose: with a 0 in the fixture the one asserted value
        # was the fabricated zero this suite exists to prevent, and `percent = 0.0` (a
        # hardcoded literal) passed it. 30.37 % cannot be produced by accident.
        res = run("synthetic", {"/v2/quotas": load("synthetic_quotas.json")}, base)
        win = window(res, "subscription")
        check("Synthetic: percent derived from requests/limit",
              win is not None and abs(win["percent"] - 41.0 / 135.0 * 100.0) < 0.01
              and win["cap"] == 135.0,
              "pct=%s cap=%s" % ((win or {}).get("percent"), (win or {}).get("cap")))
        check("Synthetic: keeps the renewal stamp",
              win is not None and (win.get("resets_at") or "").startswith("2025-09-21"),
              (win or {}).get("resets_at"))

        # ---- the failure branches: 401, 403 and a dead host --------------------
        # Every auth_error message, every `state` string and the "never fatal" promise were
        # unverified for six of the eight adapters: the stub could only answer 200, so ~30
        # auth/network/404 branches were structurally unreachable from this file.
        for provider, path in (
            ("cheaperinference", "/v1/account/balance"),
            ("deepseek", "/user/balance"),
            ("kimi", "/v1/users/me/balance"),
            ("zai", "/api/monitor/usage/quota/limit"),
            ("synthetic", "/v2/quotas"),
        ):
            res = run(provider, {path: (401, {"error": "invalid api key"})}, base)
            check("%s: a 401 is an auth_error, not a parsed body" % provider,
                  res["state"] == "auth_error", "state=%s error=%r" % (res["state"], res["error"]))
            check("%s: the 401's own status reaches the card" % provider,
                  res.get("http_status") == 401, repr(res.get("http_status")))
            check("%s: an auth_error carries no windows" % provider,
                  res["windows"] == [], str(res["windows"]))

        res = run("openrouter", {"/v1/credits": (401, {"error": "invalid api key"}),
                                 "/v1/key": (401, {"error": "invalid api key"})}, base)
        check("openrouter: a 401 on both routes is an auth_error",
              res["state"] == "auth_error", "state=%s error=%r" % (res["state"], res["error"]))
        check("openrouter: the 401's own status reaches the card",
              res.get("http_status") == 401, repr(res.get("http_status")))

        # 403 on /v1/credits means "a management key is required", not "the key is dead":
        # /v1/key still answers. Blaming the credential was the bug; this keeps it fixed.
        res = run("openrouter",
                  {"/v1/credits": (403, {"error": {"message": "Only management keys can perform this operation"}}),
                   "/v1/key": load("openrouter_key.json")}, base)
        check("openrouter: a 403 on /credits is not reported as a bad key",
              res["state"] == "ok", "state=%s error=%r" % (res["state"], res["error"]))
        check("openrouter: the card says the wallet needs a management key",
              "management key" in json.dumps(res), json.dumps(res)[:200])

        # A host that refuses the connection must be a network_error: not a traceback, and
        # not a card that quietly reads as fine.
        saved_base = pb.DEEPSEEK_BASE
        pb.DEEPSEEK_BASE = "http://127.0.0.1:%d" % free_port()
        try:
            res = run("deepseek", {}, base)
            check("a refused connection is a network_error, not a crash",
                  res["state"] == "network_error",
                  "state=%s error=%r" % (res["state"], res["error"]))
        finally:
            pb.DEEPSEEK_BASE = saved_base

        # A provider that quotes the credential back in a body it also fails to understand:
        # the body is dumped into the served error text, so only the total scrub inside
        # poll_account stops the key from being published. The token is deliberately NOT
        # key-shaped (no sk-/user-/cmd- prefix), so the shape rule cannot be what saves it.
        KEY = "opaque-TOKEN-abcdefghijklmnopqrstuvwxyz"
        res = run("deepseek", {"/user/balance": {"error": "invalid api key %s" % KEY,
                                                 "detail": "no balance_infos array here"}}, base,
                  token=KEY)
        check("deepseek: a body quoting the key is an error, not a zero",
              res["state"] == "shape_unknown", "state=%s" % res["state"])
        check("a credential echoed by the provider never reaches the result",
              KEY not in json.dumps(res), json.dumps(res)[:200])
        check("a key-shaped echo is redacted too",
              not app.CREDENTIAL_SHAPE.search(json.dumps(res)), json.dumps(res)[:200])
        # A provider that echoes a TRUNCATED key must not leak the remainder: the scrub has to
        # cover every prefix/suffix length, not just the sizes a previous author happened to list.
        LONG_KEY = "opaque-" + "A" * 57          # 64 chars, deliberately not key-shaped
        leaked = []
        for size in range(6, len(LONG_KEY) + 1):
            for echo in (LONG_KEY[:size], LONG_KEY[-size:]):
                out = app._redact("key: " + echo, LONG_KEY)
                if out.replace("key: ", "").replace("***", ""):
                    leaked.append(size)
        check("every truncated echo of a key is fully redacted", not leaked,
              "lengths with residual: %s" % sorted(set(leaked))[:10])
        scrubbed_key = list(app._scrub_result({LONG_KEY: "x"}, LONG_KEY))[0]
        check("a credential-shaped dict key is scrubbed too", scrubbed_key == "***",
              repr(scrubbed_key))
        # A provider can send any epoch it likes. An out-of-range stamp must read as "no window",
        # not raise inside the adapter and degrade the whole account to internal_error.
        for bad in (1000000000000, 1700000000000000, 253402300800):
            try:
                got = app.iso_from_reset(bad)
            except Exception as e:                    # noqa: BLE001 - the failure is the point
                got = "%s: %s" % (type(e).__name__, e)
            check("iso_from_reset(%s) is None, not an exception" % bad, got is None, repr(got))
        check("iso_from_reset normalises an offset stamp to UTC Z",
              app.iso_from_reset("2026-01-01T00:00:00+02:00") == "2025-12-31T22:00:00Z",
              repr(app.iso_from_reset("2026-01-01T00:00:00+02:00")))
        check("iso_from_reset still accepts a plain Z stamp",
              app.iso_from_reset("2026-01-01T00:00:00Z") == "2026-01-01T00:00:00Z",
              repr(app.iso_from_reset("2026-01-01T00:00:00Z")))
        check("a query string is stripped from a logged request path",
              app.scrub_path("/api/history?token=secret&x=1") == "/api/history",
              repr(app.scrub_path("/api/history?token=secret&x=1")))
        pb_src = open(os.path.join(ROOT, "providers_balance.py"), encoding="utf-8").read()
        check("providers_balance adds its own directory, not its parent, to sys.path",
              "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))" in pb_src
              and "os.path.dirname(os.path.dirname(os.path.abspath(__file__)))" not in pb_src,
              "the extra dirname put the project's parent on sys.path")
        # A provider's `currency` is remote text: a list/dict made the symbol lookup raise and the
        # whole /api/homepage 500 until the next poll. It must degrade to a plain string, not crash.
        saved_state = app.STATE
        app.STATE = {
            "generated_at": "2026-01-01T00:00:00Z",
            "accounts": [{
                "id": "x-1", "label": "X", "state": "ok", "error": None,
                "windows": [{"key": "bal", "label": "Balance", "kind": "balance", "percent": None,
                             "amount": 12.5, "currency": ["USD"], "resets_at": ""}],
            }],
        }
        try:
            widgets = app.homepage_widgets()["widgets"]
            ok = any(w.get("value") == "$12.50" for w in widgets)
            detail = str(widgets)[:120]
        except Exception as e:                        # noqa: BLE001 - the failure is the point
            ok, detail = False, "%s: %s" % (type(e).__name__, e)
        finally:
            app.STATE = saved_state
        check("a non-string currency does not 500 /api/homepage", ok, detail)
        # An empty explicit id falls back to the positional default, and must not be rejected by
        # the charset rule for a provider whose name carries an underscore.
        import tempfile
        fd, cfg_path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"accounts": [{"id": "", "provider": "opencode_go", "token": "t"}]}, fh)
            loaded = app.load_accounts(cfg_path)
            check("an empty explicit id falls back instead of being rejected",
                  bool(loaded) and loaded[0]["id"] == "opencode_go-1", str(loaded))
        finally:
            os.unlink(cfg_path)

        # ---- a 404 is not a diagnosis the adapter is entitled to make ---------
        res = run("commandcode", {"/alpha/whoami": (404, {"error": "not found"})}, base)
        check("CommandCode 404: state is no_access", res["state"] == "no_access",
              str(res.get("state")))
        err = res.get("error") or ""
        check("CommandCode 404: does not assert a plan cause from a 404",
              "route was not found" in err and "plan" in err and "moved or renamed" in err, err)
        # ---- the window adapters' SUCCESS paths ------------------------------
        # Before this, only the 404 branch was driven: a wrong percent formula or a dropped window
        # shipped green, because the success shape lived only in the screenshot stub.
        cc = run("commandcode", {
            "/alpha/whoami": {"success": True, "user": {"id": "u", "name": "Ada"}, "org": None},
            "/alpha/billing/credits": {
                "credits": {"belowThreshold": False, "creditThreshold": 0, "monthlyCredits": 62.13,
                            "purchasedCredits": 0, "freeCredits": 0},
                "windowLimits": {"limited": True, "exceeded": None,
                                 "fiveHour": {"used": 0.35, "cap": 14, "exceeded": False,
                                              "resetAt": 1790004492475},
                                 "weekly": {"used": 7.87, "cap": 35, "exceeded": False,
                                            "resetAt": 1790352668025}}},
            "/alpha/billing/subscriptions": {"success": True, "data": {
                "status": "active", "planId": "individual-goat", "quantity": 1,
                "currentPeriodStart": "2026-09-18T09:30:47.000Z",
                "currentPeriodEnd": "2026-10-18T09:30:47.000Z"}},
            "/alpha/usage/summary": {"totalCount": 3008, "totalCost": 7.73, "successRate": 100,
                                     "completedCount": 3008, "failedCount": 0,
                                     "totalTokensIn": 379798851, "totalTokensOut": 4718163,
                                     "totalCredits": 7.73, "periodBasis": "billing-period"},
        }, base)
        cc_percents = sorted(round(w["percent"], 2) for w in cc.get("windows") or []
                             if w.get("percent") is not None)
        check("commandcode success: state is ok with the three windows",
              cc["state"] == "ok" and len(cc.get("windows") or []) == 3,
              "%s %s" % (cc.get("state"), cc.get("windows")))
        check("commandcode success: the window percentages come from used/cap",
              2.5 in cc_percents and 22.49 in cc_percents, str(cc_percents))

        # OpenCode Go's base is a full URL, not a `*_BASE`, so it is redirected explicitly.
        saved_go_url = app.OG_USAGE_URL
        app.OG_USAGE_URL = base + "/zen/go/v1/usage"
        try:
            go = run("opencode_go", {"/zen/go/v1/usage": {"usage": {
                "rolling": {"percent": 2, "resetsAt": 1790004492},
                "weekly": {"percent": 22, "resetsAt": 1790352668},
                "monthly": {"percent": 84, "resetsAt": 1792320000}}}}, base)
        finally:
            app.OG_USAGE_URL = saved_go_url
        go_percents = sorted(w["percent"] for w in go.get("windows") or []
                             if w.get("percent") is not None)
        check("opencode_go success: the three window percentages are read verbatim",
              go["state"] == "ok" and go_percents == [2, 22, 84],
              "%s %s" % (go.get("state"), go_percents))
        # ---- the HTTP boundary and the response cap ---------------------------
        # An exception inside a route must become a 500 with a JSON body, not a dropped
        # connection. Exercised in-process: no HTTP-surface input can make a route raise on demand.
        saved_log = app.log
        app.log = lambda msg: None
        try:
            handler = object.__new__(app.Handler)
            handler.path = "/boom"

            def boom():
                raise RuntimeError("boom")

            handler._route = boom
            seen = []
            handler._json = lambda status, obj: seen.append((status, obj))
            handler.do_GET()
        finally:
            app.log = saved_log
        check("a handler exception becomes a 500, not a dropped connection",
              seen == [(500, {"error": "internal error"})], str(seen))

        # A response larger than the cap must be refused, not buffered whole.
        ROUTES.clear()
        ROUTES["/huge"] = (200, b"x" * (app.MAX_RESPONSE_BYTES + 1024))
        cap_status, cap_body, cap_err = app.http_get_json(base + "/huge", {})
        check("a response over the cap is refused, not buffered",
              cap_body is None and cap_err and "exceeded" in cap_err,
              "%s %s %s" % (cap_status, cap_body, cap_err))

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
            # A body that is not an object at all: the adapters must answer shape_unknown rather
            # than call .get on a list/string/number and raise AttributeError.
            ("deepseek", "/user/balance", ["not", "an", "object"], "shape_unknown"),
            ("kimi", "/v1/users/me/balance", "just a string", "shape_unknown"),
            ("zai", "/api/monitor/usage/quota/limit", 42, "shape_unknown"),
            ("cheaperinference", "/v1/account/balance", [1, 2, 3], "shape_unknown"),
        ):
            res = run(provider, {path: junk}, base)
            check("%s: an unrecognised shape is an error, not a zero (%s)"
                  % (provider, expect), res["state"] == expect, "state=%s" % res["state"])
            check("%s: the unreadable card carries no amount" % provider,
                  all(w.get("amount") is None for w in res["windows"]))

        # ---- a provider whose mark is missing must not break the card ---------
        app.PROVIDERS["__ghost__"] = {"kind": "balance",
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
