#!/usr/bin/env python3
"""Stripe metrics aggregator for Rephase admin dashboard."""
import os, time, math
from datetime import datetime, timezone, timedelta
from collections import defaultdict

_cache: dict = {"ts": 0.0, "data": None}
CACHE_TTL = 60  # seconds


def _ts(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def get_metrics(fixed_costs_chf: float = 0.0) -> dict:
    now = time.time()
    if now - _cache["ts"] < CACHE_TTL and _cache["data"] is not None:
        return _cache["data"]

    try:
        import stripe
    except ImportError:
        return {"error": "stripe SDK not installed"}

    api_key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not api_key:
        return {"error": "STRIPE_SECRET_KEY not configured"}

    stripe.api_key = api_key

    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    week_start  = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    days28_start = today - timedelta(days=27)

    try:
        # ── Active subscriptions ───────────────────────────────────────────
        active_subs = []
        params = {"status": "active", "limit": 100, "expand": ["data.items.data.price"]}
        while True:
            page = stripe.Subscription.list(**params)
            active_subs.extend(page.data)
            if not page.has_more:
                break
            params["starting_after"] = page.data[-1].id

        # MRR calculation (normalise annual plans to monthly)
        mrr_cents = 0
        for sub in active_subs:
            for item in sub["items"]["data"]:
                price = item["price"]
                amt   = price.get("unit_amount") or 0
                qty   = item.get("quantity") or 1
                interval       = price.get("recurring", {}).get("interval", "month")
                interval_count = price.get("recurring", {}).get("interval_count", 1)
                if interval == "year":
                    monthly = amt * qty / (12 * interval_count)
                elif interval == "month":
                    monthly = amt * qty / interval_count
                elif interval == "week":
                    monthly = amt * qty * 4.33 / interval_count
                elif interval == "day":
                    monthly = amt * qty * 30 / interval_count
                else:
                    monthly = amt * qty
                mrr_cents += monthly

        mrr_chf = mrr_cents / 100.0

        # New subscribers bucketing
        new_today = new_week = new_month = 0
        daily_new: dict = defaultdict(int)

        for sub in active_subs:
            created = datetime.fromtimestamp(sub.created, tz=timezone.utc)
            day_key = created.strftime("%Y-%m-%d")
            if created >= days28_start:
                daily_new[day_key] += 1
            if created >= today:
                new_today += 1
            if created >= week_start:
                new_week += 1
            if created >= month_start:
                new_month += 1

        # ── Cancelled this month ───────────────────────────────────────────
        cancelled_subs = []
        params_c = {"status": "canceled", "limit": 100,
                    "created": {"gte": _ts(month_start)}}
        while True:
            page = stripe.Subscription.list(**params_c)
            cancelled_subs.extend(page.data)
            if not page.has_more:
                break
            params_c["starting_after"] = page.data[-1].id

        cancellations_month = len(cancelled_subs)

        # ── Cancelled in last 28 days for daily chart ──────────────────────
        daily_cancel: dict = defaultdict(int)
        for sub in cancelled_subs:
            if sub.canceled_at:
                day_key = datetime.fromtimestamp(sub.canceled_at, tz=timezone.utc).strftime("%Y-%m-%d")
                if datetime.fromtimestamp(sub.canceled_at, tz=timezone.utc) >= days28_start:
                    daily_cancel[day_key] += 1
        # Also fetch older cancellations within 28-day window not caught above
        params_c28 = {"status": "canceled", "limit": 100,
                      "created": {"gte": _ts(days28_start)}}
        page28 = stripe.Subscription.list(**params_c28)
        for sub in page28.data:
            if sub.canceled_at:
                d = datetime.fromtimestamp(sub.canceled_at, tz=timezone.utc)
                if d >= days28_start:
                    daily_cancel[d.strftime("%Y-%m-%d")] += 1

        # ── Build ordered 28-day series ────────────────────────────────────
        labels, series_new, series_cancel = [], [], []
        for i in range(28):
            d = (days28_start + timedelta(days=i)).strftime("%Y-%m-%d")
            labels.append(d)
            series_new.append(daily_new.get(d, 0))
            series_cancel.append(daily_cancel.get(d, 0))

        # ── 7-day projection ───────────────────────────────────────────────
        last7_new    = sum(series_new[-7:])
        last7_cancel = sum(series_cancel[-7:])
        daily_net    = (last7_new - last7_cancel) / 7.0
        pro_count    = len(active_subs)
        projected_mrr = 0.0
        if pro_count > 0:
            arpu = mrr_chf / pro_count
            projected_mrr = (pro_count + daily_net * 30) * arpu
        else:
            projected_mrr = mrr_chf

        # ── Net margin ─────────────────────────────────────────────────────
        # Stripe fee: 2.9% + CHF 0.30 per active subscriber (monthly charge)
        stripe_fee = mrr_chf * 0.029 + pro_count * 0.30
        net_margin = mrr_chf - stripe_fee - fixed_costs_chf
        net_margin_pct = (net_margin / mrr_chf * 100) if mrr_chf > 0 else 0.0

        result = {
            "app": "rephase",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mrr_chf": round(mrr_chf, 2),
            "pro_users_active": pro_count,
            "new_today": new_today,
            "new_this_week": new_week,
            "new_this_month": new_month,
            "cancellations_month": cancellations_month,
            "projected_mrr_chf": round(projected_mrr, 2),
            "stripe_fee_chf": round(stripe_fee, 2),
            "fixed_costs_chf": round(fixed_costs_chf, 2),
            "net_margin_chf": round(net_margin, 2),
            "net_margin_pct": round(net_margin_pct, 1),
            "chart": {
                "labels": labels,
                "new_subscribers": series_new,
                "cancellations": series_cancel,
            },
        }

    except Exception as e:
        return {"error": str(e)}

    _cache["ts"] = now
    _cache["data"] = result
    return result
