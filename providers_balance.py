#!/usr/bin/env python3
"""Adapters that report a money balance instead of a percentage window.

The panel has two card kinds:

  * `window`  — an envelope that refills on a clock (CommandCode 5h/week/month,
                OpenCode Go rolling/weekly/monthly, z.ai quota limits, Synthetic
                request allowance). A percent is the whole story.
  * `balance` — prepaid money with no cap. There is no honest percentage: "40%
                used" needs a denominator nobody publishes, and inventing one
                (e.g. against a top-up) would be worse than showing nothing.

Everything here returns the `balance` kind. The response each adapter expects is
written down in tests/fixtures/providers/, so a provider that changes its payload
can be diffed against what it used to send before the parser is touched.

Design rules these adapters obey:
  * GET only, never a POST, never the inference path — read-only is the whole point.
  * an unexpected shape is an explicit `unreadable` state, never a guessed 0.
    A fabricated zero on a balance card reads as "you are out of money".
  * providers answer 200 with an error body (z.ai does exactly this), so every
    adapter asserts its own success field rather than trusting the status code.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (  # noqa: E402
    _fail,
    http_get_json,
    num,
    unwrap,
)

# --------------------------------------------------------------------------- config

CHEAPERINFERENCE_BASE = os.environ.get(
    "CHEAPERINFERENCE_API_BASE", "https://api.cheaperinference.com"
).rstrip("/")
OPENROUTER_BASE = os.environ.get("OPENROUTER_API_BASE", "https://openrouter.ai/api").rstrip("/")
DEEPSEEK_BASE = os.environ.get("DEEPSEEK_API_BASE", "https://api.deepseek.com").rstrip("/")
# platform.kimi.ai and platform.kimi.com keys are NOT interchangeable: a key from one
# platform used against the other answers 401. Default to the .ai host, override for .com.
MOONSHOT_BASE = os.environ.get(
    "MOONSHOT_API_BASE", "https://api.moonshot.ai"
).rstrip("/")

# Which currency a multi-currency balance is reported in when the provider lists several.
# DeepSeek returns an array of {currency, total_balance} and the FIRST entry is the account's
# own billing currency — but the search below looks for this value first and only falls back
# to infos[0], so an account billing in CNY that also holds USD is reported in USD. The test
# asserts that order ("picks USD deterministically"); this comment used to claim the reverse.
DEFAULT_CURRENCY = os.environ.get("QUOTA_CURRENCY", "USD").upper()


def _balance(account, *, amount, currency, note=None, secondary=None, resets_at=None, extra=None):
    """One money-balance window, in the `balance` card kind.

    Rendering a balance as a window entry keeps /api/quota shape-stable: consumers
    already iterate accounts[].windows[]. `kind` tells the UI which variant to draw
    and `percent` is deliberately null — there is no cap to divide by.
    """
    entry = {
        "kind": "balance",
        "key": "balance",
        # `label` is a human name, as it is for every window ("Week", "5 h"). Putting the
        # currency here made /api/quota read `label: "USD"` for balances and `label: "Week"`
        # for windows, so any consumer printing the label printed a currency where a name
        # belongs. The currency stays in `currency`, which is what the UI formats with.
        "label": "Balance",
        "percent": None,
        "used": None,
        "cap": None,
        "resets_at": resets_at,
        "note": note,
        "amount": None if amount is None else round(amount, 6),
        "currency": currency or None,
    }
    if secondary:
        entry["secondary"] = secondary
    if extra:
        entry["extra"] = extra
    return entry


# ------------------------------------------------------------------- cheaperinference
#
# GET /v1/account/balance  ->  AccountBalance
#   Balance, what is reserved against requests in flight, and auto-recharge state.
#   Spend against `available_usd`, not `balance_usd`: the difference is `reserved_usd`,
#   money already held against requests that have not settled, so `balance_usd` alone
#   overstates what can still be spent.
#
# The endpoint needs the `account:read` scope on the key. A usage-only key answers 403,
# which is a config mistake and is reported as one rather than as a broken provider.

def fetch_cheaperinference(account):
    headers = {
        "Authorization": "Bearer %s" % account["token"],
        "Accept": "application/json",
        "User-Agent": "quota-panel/1.0",
    }
    status, body, err = http_get_json(CHEAPERINFERENCE_BASE + "/v1/account/balance", headers)

    if err and status in (401, 403):
        detail = (
            "CheaperInference rejected the key (HTTP %s). 401 = invalid or missing key; "
            "403 = the key is valid but lacks the 'account:read' scope this endpoint "
            "requires (a usage-only key cannot read the balance)." % status
        )
        return _fail(account, "auth_error", detail, status=status)
    if err and status is None:
        return _fail(account, "network_error", err, status=status)
    if err and status == 404:
        return _fail(
            account,
            "no_access",
            "CheaperInference /v1/account/balance returned 404 — the route was not found. "
            "A 404 cannot say which of these it is: a base URL pointing at the other vendor "
            "(api.cheaperinference.com and api.cheapestinference.com are different vendors), "
            "a moved or renamed route, or a plan that does not include this endpoint.",
             status=status,
         )
    if body is None:
        return _fail(account, "provider_error", err or "empty response body", status=status)
    if not isinstance(body, dict):
        return _fail(account, "shape_unknown",
                     "CheaperInference returned %s, not a JSON object" % type(body).__name__,
                     status=status)

    # The wallet object sits at the top level, or one `data` envelope deep. A non-object
    # `data` value is not the wallet, so fall back to the body rather than calling .get on it.
    root = unwrap(body, "data")
    if not isinstance(root, dict):
        root = body

    available = num(root.get("available_usd"))
    balance = num(root.get("balance_usd"))
    reserved = num(root.get("reserved_usd"))
    currency = (root.get("currency") or "USD")
    # The published field is `auto_recharge_enabled` (boolean). `auto_recharge` does not
    # exist anywhere in the vendor's OpenAPI for this route, so the old lookup was dead code.
    auto = root.get("auto_recharge_enabled")

    if available is None and balance is None:
        return _fail(
            account,
            "shape_unknown",
            "unrecognised CheaperInference balance response (no available_usd/balance_usd): %s"
            % json.dumps(body)[:200],
             status=status,
         )
    # available_usd is the honest number; fall back to balance_usd only if it is absent.
    shown = available if available is not None else balance
    note = None
    if reserved:
        note = "%s reserved in flight" % _money(reserved, currency)
    if auto is True or str(auto).lower() == "true":
        note = (note + " · " if note else "") + "auto-recharge on"

    extra = {}
    if balance is not None:
        extra["balance_usd"] = balance
    if available is not None:
        extra["available_usd"] = available
    if reserved is not None:
        extra["reserved_usd"] = reserved

    return {
        "id": account["id"],
        "provider": account["provider"],
        "label": account["label"],
        "state": "ok",
        "error": None,
        "plan": "CheaperInference wallet",
        "account_name": None,
        "monthly_remaining": shown,
        "period_end": None,
        "totals": {},
        "currency": currency,
        "windows": [
            _balance(account, amount=shown, currency=currency, note=note, extra=extra)
        ],
        "fetched_at": _now(),
    }


# ------------------------------------------------------------------------ openrouter
#
# GET /api/v1/credits -> {data:{total_credits, total_usage}}      (money)
# GET /api/v1/key     -> {data:{limit, limit_remaining, usage, ...}}  (cap, if set)
#
# A percentage is only honest when the KEY has a `limit` set: without one the credit
# total is a top-up history, not a budget, and OpenRouter happily lets usage exceed it.
# So: percent when limit is present, balance otherwise, both in one card.

def fetch_openrouter(account):
    headers = {
        "Authorization": "Bearer %s" % account["token"],
        "Accept": "application/json",
        "User-Agent": "quota-panel/1.0",
    }
    status, credits, err_c = http_get_json(OPENROUTER_BASE + "/v1/credits", headers)

    if err_c and status is None:
        return _fail(account, "network_error", err_c, status=status)
    # /v1/credits requires a *management* key: an ordinary key is refused with 403 ("Only
    # management keys can perform this operation"). That is not a dead key — /v1/key still
    # answers — so the key reading is kept and the card says what is missing rather than
    # blaming the credential.
    credits_blocked = status in (401, 403)

    _, keyinfo, err_k = http_get_json(OPENROUTER_BASE + "/v1/key", headers)
    if credits is None and keyinfo is None:
        if credits_blocked and status == 401:
            return _fail(
                account,
                "auth_error",
                "OpenRouter rejected the key (HTTP 401). Check the key at "
                "openrouter.ai/settings/keys.",
                 status=status,
             )
        return _fail(account, "provider_error", err_c or err_k or "no data from OpenRouter", status=status)

    cdata = unwrap(credits, "data") or unwrap(credits) or {}
    kdata = unwrap(keyinfo, "data") or unwrap(keyinfo) or {}

    total = num(cdata.get("total_credits"))
    spent = num(cdata.get("total_usage"))
    remaining = None
    if total is not None and spent is not None:
        remaining = total - spent

    limit = num(kdata.get("limit"))
    limit_remaining = num(kdata.get("limit_remaining"))
    key_usage = num(kdata.get("usage"))
    daily = num(kdata.get("usage_daily"))
    weekly = num(kdata.get("usage_weekly"))
    monthly = num(kdata.get("usage_monthly"))

    # A body that parses but carries none of the documented fields is unreadable, not a
    # zero-balance account. Without this check the card renders a valid-looking shape with
    # no amount and no error, which reads as "$0.00" on a panel whose whole job is telling
    # you whether you can still make calls.
    recognised = any(
        value is not None
        for value in (total, spent, limit, limit_remaining, key_usage, daily, weekly, monthly)
    )
    if not recognised:
        return _fail(
            account,
            "shape_unknown",
            "unrecognised OpenRouter response (no known fields in /credits or /key): "
            "credits=%s key=%s"
            % (json.dumps(cdata)[:100], json.dumps(kdata)[:100]),
             status=status,
         )

    windows = []
    if limit and limit > 0:
        used = limit - limit_remaining if limit_remaining is not None else num(kdata.get("usage"))
        percent = None
        if used is not None:
            percent = max(0.0, min(100.0, used / limit * 100.0))
        windows.append(
            {
                "kind": "window",
                "key": "key_limit",
                "label": "Key limit",
                "percent": None if percent is None else round(percent, 2),
                "used": used,
                "cap": limit,
                "resets_at": None,
                "note": (kdata.get("limit_reset") or None),
                "amount": limit_remaining,
                "currency": "USD",
            }
        )
        resets = kdata.get("limit_reset")
        if resets:
            windows[-1]["note"] = "resets %s" % resets
        if credits_blocked:
            # A capped key renders this window, so the branch below never runs — but the
            # wallet reading is still missing, and a card that shows only the cap implies the
            # balance was read. Say it here too.
            windows[-1]["note"] = "; ".join(
                part for part in [windows[-1]["note"], "wallet balance needs a management key"]
                if part
            )
    elif limit == 0:
        # A reported spend limit of 0 is a reading, not the absence of one: calling it "no
        # cap" would show a spendable balance on a key that cannot spend anything.
        windows.append(
            _balance(
                account,
                amount=limit_remaining,
                currency="USD",
                note="spend limit reported as 0 — nothing spendable on this key",
            )
        )
    elif keyinfo is not None:
        note = "no spend cap set on this key"
        if credits_blocked:
            note = "wallet balance needs a management key"
            if key_usage is not None:
                note += "; this key has used %s" % _money(key_usage, "USD")
        windows.append(
            _balance(
                account,
                amount=limit_remaining if credits is None else remaining,
                currency="USD",
                note=note,
            )
        )

    if not windows:
        windows.append(_balance(account, amount=remaining, currency="USD"))

    extra = {}
    if total is not None:
        extra["total_credits"] = total
    if spent is not None:
        extra["total_usage"] = spent
    for name, value in (("usage_daily", daily), ("usage_weekly", weekly), ("usage_monthly", monthly)):
        if value is not None:
            extra[name] = value
    if kdata.get("is_free_tier") is not None:
        extra["is_free_tier"] = bool(kdata.get("is_free_tier"))
    if extra:
        windows[-1]["extra"] = extra

    # The /key response's `label` is USER-SET but defaults to the key's own prefix, so it
    # can contain a fragment of the credential (measured: a default label came back as the
    # live `sk-or-v1-…` prefix). A dashboard must never print part of a secret, and the
    # plan line is rendered on the card, so a credential-shaped label is dropped.
    label = kdata.get("label")
    plan = "OpenRouter"
    if isinstance(label, str) and label.strip() and not _looks_like_credential(label):
        plan = label.strip()

    return {
        "id": account["id"],
        "provider": account["provider"],
        "label": account["label"],
        "state": "ok",
        "error": None,
        "plan": plan,
        "account_name": None,
        "monthly_remaining": remaining,
        "period_end": None,
        "totals": {},
        "currency": "USD",
        "windows": windows,
        "fetched_at": _now(),
    }


# --------------------------------------------------------------------------- deepseek
#
# GET /user/balance -> {is_available, balance_infos:[{currency, total_balance,
#                       granted_balance, topped_up_balance}]}
#
# Amounts are STRINGS, and balance_infos is an array — so a currency has to be chosen
# deterministically or the panel would flip between CNY and USD between polls.

def fetch_deepseek(account):
    headers = {
        "Authorization": "Bearer %s" % account["token"],
        "Accept": "application/json",
        "User-Agent": "quota-panel/1.0",
    }
    status, body, err = http_get_json(DEEPSEEK_BASE + "/user/balance", headers)

    if err and status in (401, 403):
        return _fail(
            account,
            "auth_error",
            "DeepSeek rejected the key (HTTP %s). Note the unauth body is "
            "'Authentication Fails (governor)'." % status,
             status=status,
         )
    if err and status is None:
        return _fail(account, "network_error", err, status=status)
    if body is None:
        return _fail(account, "provider_error", err or "empty response body", status=status)
    if not isinstance(body, dict):
        return _fail(account, "shape_unknown",
                     "DeepSeek returned %s, not a JSON object" % type(body).__name__,
                     status=status)

    infos = body.get("balance_infos")
    if not isinstance(infos, list) or not infos:
        return _fail(
            account,
            "shape_unknown",
            "DeepSeek returned no balance_infos array: %s" % json.dumps(body)[:200],
             status=status,
         )

    chosen = None
    for info in infos:
        if isinstance(info, dict) and str(info.get("currency", "")).upper() == DEFAULT_CURRENCY:
            chosen = info
            break
    if chosen is None:
        chosen = infos[0]
    currency = (chosen.get("currency") or DEFAULT_CURRENCY).upper()

    total = num(chosen.get("total_balance"))
    granted = num(chosen.get("granted_balance"))
    topped = num(chosen.get("topped_up_balance"))
    if total is None:
        return _fail(
            account,
            "shape_unknown",
            "DeepSeek balance entry has no total_balance: %s" % json.dumps(chosen)[:200],
             status=status,
         )

    is_available = body.get("is_available")
    # `is_available` describes the ACCOUNT, not the currency shown here. With a
    # multi-currency balance the two can disagree (CNY empty, USD funded), so the
    # note names the currency it refers to instead of contradicting the figure.
    note = None
    if is_available is False:
        note = "account flagged unavailable — API calls will fail"
    bits = []
    if granted:
        bits.append("%s granted" % _money(granted, currency))
    if topped:
        bits.append("%s topped up" % _money(topped, currency))
    if bits:
        note = (note + " · " if note else "") + ", ".join(bits)

    extra = {}
    # Only when the provider actually said so. `bool(None)` published `"is_available": false`
    # out of a missing field — a confident "this account is flagged unavailable" derived from
    # nothing. The schema requires the field, so this only bites a shape drift, which is
    # exactly when a fabricated negative is worst.
    if is_available is not None:
        extra["is_available"] = bool(is_available)
    if granted is not None:
        extra["granted_balance"] = granted
    if topped is not None:
        extra["topped_up_balance"] = topped
    if len(infos) > 1:
        others = []
        for entry in infos:
            if isinstance(entry, dict) and entry is not chosen:
                others.append(
                    {"currency": entry.get("currency"), "total_balance": entry.get("total_balance")}
                )
        extra["other_currencies"] = others

    return {
        "id": account["id"],
        "provider": account["provider"],
        "label": account["label"],
        "state": "ok",
        "error": None,
        "plan": "DeepSeek balance",
        "account_name": None,
        "monthly_remaining": total,
        "period_end": None,
        "totals": {},
        "currency": currency,
        "windows": [
            _balance(account, amount=total, currency=currency, note=note, extra=extra)
        ],
        "fetched_at": _now(),
    }


# ----------------------------------------------------------------------------- kimi
#
# GET /v1/users/me/balance -> {code:0, status:true, scode:"0x0",
#                              data:{available_balance, voucher_balance, cash_balance}}
#
# Success is `status == True AND code == 0`. `cash_balance` can be
# negative (the user owes money) and `available_balance` is then just the vouchers.

def fetch_kimi(account):
    headers = {
        "Authorization": "Bearer %s" % account["token"],
        "Accept": "application/json",
        "User-Agent": "quota-panel/1.0",
    }
    url = MOONSHOT_BASE + "/v1/users/me/balance"
    status, body, err = http_get_json(url, headers)

    if err and status in (401, 403):
        return _fail(
            account,
            "auth_error",
            "Kimi/Moonshot rejected the key (HTTP %s). platform.kimi.ai and "
            "platform.kimi.com keys are not interchangeable — a key from one platform "
            "answers 401 on the other, so check MOONSHOT_API_BASE." % status,
             status=status,
         )
    if err and status is None:
        return _fail(account, "network_error", err, status=status)
    if body is None:
        return _fail(account, "provider_error", err or "empty response body", status=status)
    if not isinstance(body, dict):
        return _fail(account, "shape_unknown",
                     "Kimi returned %s, not a JSON object" % type(body).__name__, status=status)

    if body.get("status") is not True or body.get("code") not in (0, None):
        return _fail(
            account,
            "provider_error",
            "Kimi balance call did not succeed (code=%s status=%s): %s"
            % (body.get("code"), body.get("status"), json.dumps(body)[:160]),
             status=status,
         )

    data = unwrap(body, "data")
    if not isinstance(data, dict):
        return _fail(
            account,
            "shape_unknown",
            "Kimi returned no balance data object: %s" % json.dumps(body)[:200],
             status=status,
         )

    available = num(data.get("available_balance"))
    voucher = num(data.get("voucher_balance"))
    cash = num(data.get("cash_balance"))
    if available is None:
        return _fail(
            account,
            "shape_unknown",
            "Kimi balance has no available_balance: %s" % json.dumps(data)[:200],
             status=status,
         )

    note = None
    if available <= 0:
        note = "balance exhausted — the inference API returns exceeded_current_quota_error"
    # platform.kimi.com bills in CNY, platform.kimi.ai in USD. The payload carries no currency
    # field of its own, so it is derived from the host that answered rather than assumed: a
    # yuan balance printed as dollars is a wrong number, not a cosmetic bug.
    currency = "CNY" if "kimi.com" in MOONSHOT_BASE else "USD"
    bits = []
    if voucher is not None:
        bits.append("%s voucher" % _money(voucher, currency))
    if cash is not None:
        bits.append("%s cash" % _money(cash, currency))
    if bits:
        note = (note + " · " if note else "") + ", ".join(bits)

    extra = {}
    if voucher is not None:
        extra["voucher_balance"] = voucher
    if cash is not None:
        extra["cash_balance"] = cash

    return {
        "id": account["id"],
        "provider": account["provider"],
        "label": account["label"],
        "state": "ok",
        "error": None,
        "plan": "Kimi balance",
        "account_name": None,
        "monthly_remaining": available,
        "period_end": None,
        "totals": {},
        "currency": currency,
        "windows": [
            _balance(account, amount=available, currency=currency, note=note, extra=extra)
        ],
        "fetched_at": _now(),
    }


# ---------------------------------------------------------------------- z.ai (quota)
#
# GET https://api.z.ai/api/monitor/usage/quota/limit
#   data.limits[]: type (TOKENS_LIMIT | TIME_LIMIT | CREDIT_LIMIT),
#                  unit (1=days, 3=hours, 5=minutes, 6=days-weekly), number,
#                  usage (= TOTAL quota), currentValue, remaining, percentage,
#                  nextResetTime (epoch seconds OR milliseconds), planName.
#
# The live trap, captured in fixtures: this route answers HTTP 200 with
# {"code":1001,"msg":"Authentication parameter not received in Header","success":false}.
# A status-code check reads that as a valid payload, so success/code are asserted.
#
# Contract source: two independent implementations agree field-by-field
# (steipete/CodexBar docs/zai.md, bugwz/AIMeter docs/providers/zai/README.md).

ZAI_BASE = os.environ.get("Z_AI_API_BASE", "https://api.z.ai").rstrip("/")
ZAI_UNIT_MINUTES = {"1": 1440, "3": 60, "5": 1, "6": 1440}
ZAI_UNIT_LABELS = {"3": "Session", "6": "Weekly", "1": "Daily", "5": "Minute"}


def fetch_zai(account):
    headers = {
        "Authorization": "Bearer %s" % account["token"],
        "Accept": "application/json",
        "User-Agent": "quota-panel/1.0",
    }
    url = ZAI_BASE + "/api/monitor/usage/quota/limit"
    status, body, err = http_get_json(url, headers)

    if err and status in (401, 403):
        return _fail(account, "auth_error", "z.ai rejected the key (HTTP %s)." % status, status=status)
    if err and status is None:
        return _fail(account, "network_error", err, status=status)
    if body is None:
        return _fail(account, "provider_error", err or "empty response body", status=status)
    if not isinstance(body, dict):
        return _fail(account, "shape_unknown",
                     "z.ai returned %s, not a JSON object" % type(body).__name__, status=status)

    # The trap: 200 with success=false.
    if body.get("success") is False or body.get("code") not in (200, None):
        return _fail(
            account,
            "auth_error",
            "z.ai answered HTTP 200 but reported failure (code=%s success=%s): %s"
            % (body.get("code"), body.get("success"), body.get("msg") or "no message"),
             status=status,
         )

    container = unwrap(body, "data") or {}
    limits = container.get("limits")
    if limits is None:
        limits = body.get("limits")
    if not isinstance(limits, list) or not limits:
        return _fail(
            account,
            "no_access",
            "z.ai returned no quota limits. Empty limits are what a Coding Plan answers "
            "when the org/project selectors are missing for a team account, so check the "
            "region and the account scope.",
             status=status,
         )

    plan = None
    for field in ("planName", "plan", "planType", "packageName"):
        value = container.get(field)
        if isinstance(value, str) and value.strip():
            plan = value.strip()
            break

    windows = []
    for raw in limits:
        if not isinstance(raw, dict):
            continue
        kind_type = str(raw.get("type") or raw.get("name") or "").upper()
        if kind_type not in ("TOKENS_LIMIT", "CREDIT_LIMIT", "TIME_LIMIT"):
            continue
        unit = str(raw.get("unit")) if raw.get("unit") is not None else ""
        number = num(raw.get("number"))
        minutes = ZAI_UNIT_MINUTES.get(unit)
        if minutes and number:
            minutes = int(minutes * number)

        cap = num(raw.get("usage"))
        remaining = num(raw.get("remaining"))
        current = num(raw.get("currentValue"))
        percent = None
        used = None
        if cap and cap > 0:
            # Both implementations this adapter was built against derive usage the same way:
            # used = max(0, min(cap, max(cap - remaining, currentValue))). Taking only
            # `cap - remaining` under-reports whenever the two counts disagree, which is a
            # card promising more headroom than the account actually has.
            candidates = [
                value
                for value in ((cap - remaining) if remaining is not None else None, current)
                if value is not None
            ]
            if candidates:
                used = max(0.0, min(cap, max(candidates)))
                percent = used / cap * 100.0
        if percent is None:
            direct = num(raw.get("percentage"))
            if direct is not None:
                percent = max(0.0, min(100.0, direct))
                used = None if cap is None else cap * percent / 100.0

        is_time = kind_type == "TIME_LIMIT"
        if is_time:
            label = "Web Searches"
        else:
            label = ZAI_UNIT_LABELS.get(unit, "Tokens")
            if unit == "3":
                label = "Session"
            elif unit == "6":
                label = "Weekly"

        # `number` belongs in the key: two limits of the same type and unit (a 5-hour and a
        # 7-day TOKENS_LIMIT both arrive as unit 3 / unit 6) collided on one key, and the
        # homepage items map silently dropped the second one.
        span = "%g" % number if number else "x"
        windows.append(
            {
                "kind": "window",
                "key": "zai_%s_%s_%s" % (kind_type.lower(), unit or "x", span),
                "label": label,
                "percent": None if percent is None else round(percent, 2),
                "used": used,
                "cap": cap,
                "resets_at": _reset_from(raw.get("nextResetTime")),
                "note": None,
                "window_minutes": minutes,
            }
        )

    if not windows:
        return _fail(account, "shape_unknown", "z.ai limits carried no recognisable window", status=status)

    return {
        "id": account["id"],
        "provider": account["provider"],
        "label": account["label"],
        "state": "ok",
        "error": None,
        "plan": plan or "GLM Coding Plan",
        "account_name": None,
        "monthly_remaining": None,
        "period_end": None,
        "totals": {},
        "windows": windows,
        "fetched_at": _now(),
    }


# ------------------------------------------------------------------ synthetic.new
#
# GET https://api.synthetic.new/v2/quotas -> {subscription:{limit, requests, renewsAt}}
#   percent = requests / limit ; reset = renewsAt. Polling this route does not consume
#   subscription quota, which is what makes it safe for a 60s poller.

SYNTHETIC_BASE = os.environ.get("SYNTHETIC_API_BASE", "https://api.synthetic.new").rstrip("/")


def fetch_synthetic(account):
    headers = {
        "Authorization": "Bearer %s" % account["token"],
        "Accept": "application/json",
        "User-Agent": "quota-panel/1.0",
    }
    status, body, err = http_get_json(SYNTHETIC_BASE + "/v2/quotas", headers)

    if err and status in (401, 403):
        return _fail(
            account,
            "auth_error",
            "Synthetic rejected the key (HTTP %s)." % status,
             status=status,
         )
    if err and status is None:
        return _fail(account, "network_error", err, status=status)
    if body is None:
        return _fail(account, "provider_error", err or "empty response body", status=status)

    sub = unwrap(body, "subscription")
    if not isinstance(sub, dict):
        sub = body if isinstance(body, dict) else {}
    limit = num(sub.get("limit"))
    requests = num(sub.get("requests"))
    renews = sub.get("renewsAt")

    if limit is None or requests is None:
        return _fail(
            account,
            "shape_unknown",
            "Synthetic returned no subscription limit/requests: %s" % json.dumps(body)[:200],
             status=status,
         )

    # Anything else in the payload shaped like another window is named instead of being
    # dropped without a word: one bar drawn while the API reports two is a quiet lie. The
    # extras are undocumented, so they are summarised rather than parsed.
    others = sorted(
        key
        for key, value in (body.items() if isinstance(body, dict) else [])
        if key != "subscription"
        and isinstance(value, dict)
        and any(field in value for field in ("limit", "requests", "used", "remaining"))
    )

    percent = None
    if limit and limit > 0:
        percent = max(0.0, min(100.0, requests / limit * 100.0))

    windows = [
        {
            "kind": "window",
            "key": "subscription",
            "label": "Window",
            "percent": None if percent is None else round(percent, 2),
            "used": requests,
            "cap": limit,
            "resets_at": _reset_from(renews),
            "note": ("also reported: %s" % ", ".join(others)) if others else None,
        }
    ]

    return {
        "id": account["id"],
        "provider": account["provider"],
        "label": account["label"],
        "state": "ok",
        "error": None,
        "plan": "Synthetic subscription",
        "account_name": None,
        "monthly_remaining": None,
        "period_end": None,
        "totals": {},
        "windows": windows,
        "fetched_at": _now(),
    }


# ------------------------------------------------------------------------- helpers


def _money(value, currency):
    if value is None:
        return "—"
    symbol = {"USD": "$", "CNY": "¥", "EUR": "€"}.get(currency)
    if symbol:
        return "%s%.2f" % (symbol, value)
    return "%.2f %s" % (value, currency or "")


# Providers echo back strings that *can* be credentials: OpenRouter's /key returns the
# key's label, which defaults to the key's own prefix (`sk-or-v1-abcd…`), and a label is
# rendered on the card's plan line. Anything credential-shaped is therefore refused
# before it reaches a card — a dashboard must never print part of a secret, and a
# screenshot of one is forever.
CREDENTIAL_PREFIXES = ("sk-", "sk_", "user_", "key-", "ci_live_", "ci_test_", "zk-", "xai-")


def _looks_like_credential(value):
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    low = text.lower()
    if low.startswith(CREDENTIAL_PREFIXES):
        return True
    # A long opaque run with no separators is treated as a secret rather than a name: a label
    # a human typed reads as words ("production-workspace-2024", longest run 11), a key does
    # not. The word-only rule alone was measured dropping legitimate labels.
    if len(text) >= 24 and " " not in text and any(c.isdigit() for c in text):
        parts = [part for part in re.split(r"[-_./]", text) if part]
        if max(len(part) for part in parts) >= 16:
            return True
        # Separators alone do not make a name: a UUID is a key shape that has four of them, and
        # every one of its segments is hex. A segment made of words is not, so this cannot
        # re-break "personal-laptop-key-20260101".
        if len(parts) >= 4 and all(all(c in "0123456789abcdefABCDEF" for c in p) for p in parts):
            return True
    return False


def _now():
    from app import now_iso

    return now_iso()


def _reset_from(value):
    from app import iso_from_reset

    return iso_from_reset(value)


BALANCE_FETCHERS = {
    "cheaperinference": fetch_cheaperinference,
    "openrouter": fetch_openrouter,
    "deepseek": fetch_deepseek,
    "kimi": fetch_kimi,
    "zai": fetch_zai,
    "synthetic": fetch_synthetic,
}
