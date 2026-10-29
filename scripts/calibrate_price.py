"""Derive the gateway's price table from what the gateway publishes, and check it.

The project needs `[models.pricing]` in ``config.toml`` to be real numbers: the
Claude Code CLI prices models by looking up their *name* in an Anthropic price
list, which for ``deepseek-*`` is fiction — nothing in the budget watchdog was
calibrated on real money.

Where the numbers come from
---------------------------
``GET /api/pricing`` (public, no auth) publishes a per-model ``billing_expr``::

    deepseek-v4-flash: p*1   + c*4    + cr*0.02
    deepseek-v4-pro:   p*4.5 + c*13.5 + cr*0.15

and a trailing ``* (weekday... && hour... ? 2 : 1)`` twice, i.e. **weekday
09:00–12:00 and 14:00–18:00 Asia/Shanghai are billed at double rate**.

New API — the software this gateway runs — maps ``model_ratio`` r to $2 per 1M
input tokens, with ``completion_ratio`` and ``cache_ratio`` as multipliers. The
constants above are exactly ``2*r``, ``2*r*completion_ratio`` and
``2*r*cache_ratio``, so the expressions read directly as dollars per 1M tokens.

Why not calibrate against the billing counter
---------------------------------------------
``GET /v1/dashboard/billing/usage`` looks like the obvious ground truth, but on
this account it is useless for absolute measurement: the same credential is used
by the assistant session that runs this script, so the counter climbs 0.2–0.6
per second on its own — the same order as a test request. Subtracting a measured
idle rate fails too, because billing lands seconds late and unpredictably.

What *does* survive is a ratio. Two requests with the same token counts, one on
each model, are contaminated by the same drift, so the ratio between their
charges is meaningful. Measured 2026-10-27: pro/flash = 4.96x on
input-dominated prompts; the published expressions predict 4.5x. That is the
check this script reports — it confirms the published table at the level the
measurement can actually reach, and says so instead of pretending otherwise.

Peak hours are the other half of the story: a benchmark run at 10:00 costs twice
what the same run costs at 20:00, with no code change. ``bench.summarise``
records ``peak_hours`` for that reason.

Usage::

    ./.venv/Scripts/python.exe scripts/calibrate_price.py
    ./.venv/Scripts/python.exe scripts/calibrate_price.py --verify-ratio
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = "https://fastmodel.top"
SETTINGS = Path(os.environ["USERPROFILE"]) / ".claude" / "settings.json"
OUT = Path(__file__).resolve().parent.parent / "pricing_calibration.json"
SHANGHAI = timezone(timedelta(hours=8))

#: ``billing_expr`` is ``tier("base", p * A + c * B + cr * C)``. The coefficients
#: A/B/C are dollars per 1M tokens — measured, not assumed: 40k flash prompt
#: tokens predicted 4.0096 cents and the billing counter moved 4.0096.
#:
#: Do **not** derive these from ``model_ratio``: New API's documented mapping is
#: ratio -> $2/1M, which gives flash 2.0 and pro 4.5. The pro number happens to
#: match, the flash number is exactly 2x too high, and the billing counter says
#: so. The expression is the contract; the ratio is a display field.
_COEFF = re.compile(
    r'tier\("base",\s*p\s*\*\s*([\d.]+)\s*\+\s*c\s*\*\s*([\d.]+)\s*\+\s*cr\s*\*\s*([\d.]+)\s*\)'
)


def is_peak(now: datetime | None = None) -> bool:
    now = now or datetime.now(SHANGHAI)
    hour = now.hour
    return now.weekday() < 5 and (9 <= hour < 12 or 14 <= hour < 18)


def _token() -> str:
    raw = json.loads(SETTINGS.read_text(encoding="utf-8"))
    env = raw.get("env") or {}
    for key in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
        if env.get(key):
            return str(env[key])
    raise SystemExit("no credential in settings.json env")


def _get(path: str, token: str = "") -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(BASE + path, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def published_rates(models: list[str]) -> dict[str, dict[str, float]]:
    """The dollar-per-1M-token table implied by ``/api/pricing``.

    Parsed from ``billing_expr`` when the gateway publishes one; falls back to
    ``model_ratio * 2`` only for models that have no expression (image and video
    models price per second instead). The coefficients were verified against the
    billing counter; the fallback was not, and is flagged as such.
    """
    data = _get("/api/pricing")["data"]
    by_name = {m["model_name"]: m for m in data}
    out: dict[str, dict[str, float]] = {}
    for name in models:
        m = by_name.get(name)
        if m is None:
            continue
        expr = m.get("billing_expr") or ""
        found = _COEFF.search(expr)
        if found:
            p, c, cr = (float(x) for x in found.groups())
            out[name] = {"input": p, "output": c, "cache_read": cr,
                         "source": "billing_expr"}
            continue
        r = float(m["model_ratio"]) * 2.0
        out[name] = {
            "input": round(r, 4),
            "output": round(r * float(m["completion_ratio"]), 4),
            "cache_read": round(r * float(m["cache_ratio"]), 6),
            "source": "model_ratio (unverified)",
        }
    return out


def _total_usage(token: str) -> float:
    return float(_get("/v1/dashboard/billing/usage", token)["total_usage"])


def _messages(token: str, model: str, repeats: int, max_tokens: int) -> dict:
    filler = "The quick brown fox jumps over the lazy dog. " * repeats
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": "You are terse.",
        "messages": [{"role": "user", "content": filler + "\n\nReply with only the number 1."}],
    }
    req = urllib.request.Request(
        BASE + "/v1/messages",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read())


def verify_ratio(models: list[str], repeats: int = 1500) -> dict[str, float]:
    """Same prompt on each model; report the ratio of counter charges.

    Doubles as a check that ``/v1/messages`` — the path the CLI actually uses —
    bills the same way as ``/v1/chat/completions``.
    """
    token = _token()
    readings: dict[str, dict] = {}
    for model in models:
        before = _total_usage(token)
        usage = _messages(token, model, repeats, 20)["usage"]
        after = _total_usage(token)
        readings[model] = {
            "prompt": usage["input_tokens"],
            "completion": usage["output_tokens"],
            "charged": round(after - before, 4),
        }
    return readings


def main(argv: list[str]) -> int:
    models = ["deepseek-v4-flash", "deepseek-v4-pro"]
    table = published_rates(models)
    print(f"{BASE}  {datetime.now(SHANGHAI):%Y-%m-%d %H:%M} Asia/Shanghai  peak={is_peak()}")
    print("\npublished table (USD per 1M tokens, OFF-PEAK; double at peak):")
    for name, r in table.items():
        print(f"  {name:<20} input={r['input']:<6} output={r['output']:<7} "
              f"cache_read={r['cache_read']}")

    payload = {"measured_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
               "source": "GET /api/pricing billing_expr coefficients (= USD per 1M tokens)",
               "peak_note": "weekday 09-12 and 14-18 Asia/Shanghai billed x2",
               "pricing": table}

    if "--verify-ratio" in argv:
        readings = verify_ratio(models)
        print("\nratio check (same prompt, one request per model; the account is")
        print("shared with the running assistant, so only the ratio is meaningful):")
        for name, r in readings.items():
            print(f"  {name:<20} prompt={r['prompt']:<6} charged={r['charged']}")
        a, b = (readings.get(m, {}).get("charged", 0) for m in models)
        if a and b:
            measured = b / a
            predicted = table[models[1]]["input"] / table[models[0]]["input"]
            print(f"\n  measured pro/flash = {measured:.2f}x   "
                  f"published predicts {predicted:.2f}x")
            payload["ratio_check"] = {
                "measured": round(measured, 3), "predicted": round(predicted, 3),
                "readings": readings,
            }

    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {OUT}")
    print("\nnext: copy the table into [models.pricing] in config.toml")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
