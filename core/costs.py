#!/usr/bin/env python3
"""Costs persistence and computation for Rephase admin."""
import json, os, uuid
from datetime import datetime, timezone

COSTS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "costi.json")

_DEFAULT = {
    "launch_date": datetime.now(timezone.utc).strftime("%Y-%m-01"),
    "items": [],
    "phases": [
        {"name": "Fase 1", "min_users": 0,    "max_users": 100,    "fixed_chf": 17},
        {"name": "Fase 2", "min_users": 101,  "max_users": 500,    "fixed_chf": 17},
        {"name": "Fase 3", "min_users": 501,  "max_users": 2000,   "fixed_chf": 180},
        {"name": "Fase 4", "min_users": 2001, "max_users": 999999, "fixed_chf": 417},
    ],
}


def load_costs() -> dict:
    if os.path.exists(COSTS_FILE):
        with open(COSTS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return _DEFAULT.copy()


def save_costs(data: dict) -> None:
    with open(COSTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def total_monthly_chf(data: dict) -> float:
    """Sum of all active cost items."""
    return sum(item.get("amount_chf", 0) for item in data.get("items", []))


def phase_for_users(data: dict, n_users: int) -> dict:
    for p in data.get("phases", []):
        if p["min_users"] <= n_users <= p["max_users"]:
            return p
    phases = data.get("phases", [])
    return phases[-1] if phases else {"name": "Fase 4", "fixed_chf": 417}


def current_phase(data: dict, n_users: int) -> str:
    return phase_for_users(data, n_users)["name"]
