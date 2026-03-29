#!/usr/bin/env python3
"""Stripe metrics aggregator for Rephase admin dashboard."""
import os, time
from datetime import datetime, timezone, timedelta
from collections import defaultdict

_cache: dict = {"ts": 0.0, "data": None}
CACHE_TTL = 60  # seconds


def _ts(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def _month_bounds(year: int, month: int):
    """Return (start, end) as aware datetimes for the given month."""
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return start, end


def _months_between(start: datetime, end: datetime) -> list:
    """Return list of (year, month) tuples from start month to end month inclusive."""
    result = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        result.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return result


def _build_monthly_history(launch_date: str, all_subs: list, arpu: float, costs_data: dict) -> dict:
    """Reconstruct monthly MRR and costs from launch_date to current month."""
    from core.costs import phase_for_users, total_monthly_chf
    try:
        launch = datetime.strptime(launch_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return {}

    now = datetime.now(timezone.utc)
    months = _months_between(launch, now)
    if not months:
        return {}

    labels, mrr_series, cost_series = [], [], []

    for (y, m) in months:
        m_start, m_end = _month_bounds(y, m)
        label = f"{y}-{m:02d}"

        # Count subscriptions active during this month:
        # created before month-end AND (still active OR canceled_at >= month_start)
        active_count = 0
        for sub in all_subs:
            created = datetime.fromtimestamp(sub.created, tz=timezone.utc)
            if created >= m_end:
                continue
            if sub.status == "active":
                active_count += 1
            elif sub.canceled_at:
                canceled = datetime.fromtimestamp(sub.canceled_at, tz=timezone.utc)
                if canceled >= m_start:
                    active_count += 1

        month_mrr = round(active_count * arpu, 2)

        # Costs: sum items whose start_date <= last day of month
        month_costs = 0.0
        for item in costs_data.get("items", []):
            try:
                item_start = datetime.strptime(item["start_date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except Exception:
                item_start = launch
            if item_start < m_end:
                month_costs += item.get("amount_chf", 0)

        labels.append(label)
        mrr_series.append(month_mrr)
        cost_series.append(round(month_costs, 2))

    # Cumulative
    cum_mrr, cum_cost = [], []
    acc_m = acc_c = 0.0
    for m, c in zip(mrr_series, cost_series):
        acc_m += m; acc_c += c
        cum_mrr.append(round(acc_m, 2))
        cum_cost.append(round(acc_c, 2))

    return {
        "labels": labels,
        "mrr_monthly": mrr_series,
        "cost_monthly": cost_series,
        "mrr_cumulative": cum_mrr,
        "cost_cumulative": cum_cost,
    }


def get_metrics(fixed_costs_chf: float = 0.0, costs_data: dict = None) -> dict:
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
    week_start   = today - timedelta(days=today.weekday())
    month_start  = today.replace(day=1)
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

        mrr_chf  = mrr_cents / 100.0
        pro_count = len(active_subs)
        arpu      = mrr_chf / pro_count if pro_count > 0 else 0.0

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

        # ── All cancelled subs (for history + daily chart) ─────────────────
        launch_date = (costs_data or {}).get("launch_date", month_start.strftime("%Y-%m-%d"))
        try:
            launch_dt = datetime.strptime(launch_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            launch_dt = month_start

        all_cancelled = []
        params_ca = {"status": "canceled", "limit": 100, "created": {"gte": _ts(launch_dt)}}
        while True:
            page = stripe.Subscription.list(**params_ca)
            all_cancelled.extend(page.data)
            if not page.has_more:
                break
            params_ca["starting_after"] = page.data[-1].id

        cancellations_month = sum(
            1 for s in all_cancelled
            if datetime.fromtimestamp(s.created, tz=timezone.utc) >= month_start
        )

        daily_cancel: dict = defaultdict(int)
        for sub in all_cancelled:
            if sub.canceled_at:
                d = datetime.fromtimestamp(sub.canceled_at, tz=timezone.utc)
                if d >= days28_start:
                    daily_cancel[d.strftime("%Y-%m-%d")] += 1

        # ── 28-day daily series ────────────────────────────────────────────
        labels28, series_new, series_cancel = [], [], []
        for i in range(28):
            d = (days28_start + timedelta(days=i)).strftime("%Y-%m-%d")
            labels28.append(d)
            series_new.append(daily_new.get(d, 0))
            series_cancel.append(daily_cancel.get(d, 0))

        # ── 7-day projection ───────────────────────────────────────────────
        last7_new    = sum(series_new[-7:])
        last7_cancel = sum(series_cancel[-7:])
        daily_net    = (last7_new - last7_cancel) / 7.0
        projected_mrr = (pro_count + daily_net * 30) * arpu if pro_count > 0 else mrr_chf

        # ── Net margin ─────────────────────────────────────────────────────
        stripe_fee  = mrr_chf * 0.029 + pro_count * 0.30
        net_margin  = mrr_chf - stripe_fee - fixed_costs_chf
        net_margin_pct = (net_margin / mrr_chf * 100) if mrr_chf > 0 else 0.0

        # ── Monthly history (costs vs MRR from launch) ─────────────────────
        all_subs_for_history = active_subs + all_cancelled
        monthly_history = {}
        if costs_data:
            monthly_history = _build_monthly_history(
                launch_date=launch_date,
                all_subs=all_subs_for_history,
                arpu=arpu,
                costs_data=costs_data,
            )

        result = {
            "app": "rephase",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mrr_chf": round(mrr_chf, 2),
            "arpu_chf": round(arpu, 2),
            "pro_users_active": pro_count,
            "daily_net": round(daily_net, 3),
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
                "labels": labels28,
                "new_subscribers": series_new,
                "cancellations": series_cancel,
            },
            "monthly_history": monthly_history,
        }

    except Exception as e:
        return {"error": str(e)}

    _cache["ts"] = now
    _cache["data"] = result
    return result
