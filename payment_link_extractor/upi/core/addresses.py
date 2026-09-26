# -*- coding: utf-8 -*-
"""印度真实账单地址池。

数据来自印度邮政官方接口 `https://api.postalpincode.in/pincode/{pin}`，
已抓取落盘为 `data/in_pincodes.json`（真实 PIN / 邮局名 / 县 / 邦）。

为什么必须真实且唯一：
  1. 印度 UPI 的 setup_intent 会做地址校验，编造的街区名会被 `generic_decline`；
  2. 同一条地址反复建单同样会被拒，所以要尽量不重样；
  3. 同一批并发里出现重复地址会被判死。
"""
from __future__ import annotations

import json
import random
import threading
from pathlib import Path

_DATA = Path(__file__).with_name("data") / "in_pincodes.json"
_LOCK = threading.RLock()
_USED: set[str] = set()

# 印度邮政给的邦名 → Stripe 下拉里的规范名
_STATE_FIX = {
    "Jammu & Kashmir": "Jammu & Kashmir",
    "Jammu and Kashmir": "Jammu & Kashmir",
    "Andaman & Nicobar Islands": "Andaman & Nicobar",
    "Dadra & Nagar Haveli": "Dadra & Nagar Haveli & Daman & Diu",
    "Daman & Diu": "Dadra & Nagar Haveli & Daman & Diu",
    "Orissa": "Odisha",
    "Pondicherry": "Puducherry",
    "Uttaranchal": "Uttarakhand",
}

_BUILDINGS = [
    "Flat {n}, {name} Apartments", "House No. {n}, {name} Enclave",
    "{n}/{m}, {name} Residency", "Flat {n}{L}, {name} Heights",
    "Plot {n}, {name} Layout", "Door No. {n}-{m}, {name} Nagar",
    "{o} Floor, {name} Towers", "Flat {n}, {name} Block",
    "{n}{L}, {name} CHS", "Shop {n}, {name} Complex",
    "Flat {n}, {name} Society", "H.No. {n}/{m}, {name} Vihar",
]
_BLOCK_NAMES = ["Sunrise", "Green", "Lake", "Palm", "Silver", "Royal", "Crystal", "Golden",
                "Prestige", "Brigade", "Sobha", "Godrej", "Puravankara", "Mahindra",
                "Amrapali", "Lodha", "Shree", "Vasant", "Anand", "Sai", "Krishna", "Ganga",
                "Nirmal", "Aakriti", "Vishwa", "Rohan", "Kolte", "Kalpataru", "Om",
                "Sagar", "Meera", "Vrindavan", "Shanti", "Ashoka", "Neelkanth"]
_FIRST = ["Rahul", "Amit", "Priya", "Sneha", "Vikram", "Ananya", "Rohan", "Kavya", "Arjun",
          "Meera", "Siddharth", "Neha", "Karthik", "Divya", "Manish", "Pooja", "Aditya",
          "Ishita", "Nikhil", "Shreya", "Varun", "Anjali", "Harish", "Deepa", "Rajesh",
          "Sunita", "Mahesh", "Lakshmi", "Ganesh", "Ritu"]
_LAST = ["Sharma", "Patel", "Reddy", "Nair", "Iyer", "Gupta", "Singh", "Kumar", "Menon",
         "Joshi", "Desai", "Rao", "Verma", "Pillai", "Chauhan", "Mehta", "Bose", "Kulkarni",
         "Naidu", "Shetty", "Banerjee", "Mishra", "Chopra", "Fernandes", "Agarwal", "Yadav"]


def _load() -> list[tuple[str, str, str, str]]:
    try:
        raw = json.loads(_DATA.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for pin, value in raw.items():
        if not isinstance(value, dict) or not value.get("ok"):
            continue
        districts = value.get("districts") or []
        states = value.get("states") or []
        if not districts or not states:
            continue
        state = _STATE_FIX.get(states[0], states[0])
        for name in value.get("names") or []:
            clean = str(name or "").split("(")[0].strip()
            if len(clean) >= 3:
                out.append((pin, clean, districts[0], state))
    return out


_REAL = _load()


def _ordinal(number: int) -> str:
    suffix = "st" if number == 1 else "nd" if number == 2 else "rd" if number == 3 else "th"
    return "%d%s" % (number, suffix)


def _generate() -> dict:
    if not _REAL:
        raise RuntimeError("地址库为空：确认 data/in_pincodes.json 存在")
    pin, area, district, state = random.choice(_REAL)
    house = random.choice(_BUILDINGS).format(
        n=random.randint(1, 640), m=random.randint(1, 99), L=random.choice("ABC"),
        name=random.choice(_BLOCK_NAMES), o=_ordinal(random.randint(1, 14)))
    return {
        "name": "%s %s" % (random.choice(_FIRST), random.choice(_LAST)),
        "line1": "%s, %s" % (house, area),
        "city": district,
        "state": state,
        "postal_code": pin,
        "country": "IN",
        "locality": area,
    }


def _key(address: dict) -> str:
    return "|".join([address["line1"], address["city"], address["state"],
                     address["postal_code"]]).lower()


def next_address(*, max_tries: int = 80) -> dict:
    """取一条本次进程内没用过的真实印度地址。"""
    with _LOCK:
        for _ in range(max_tries):
            address = _generate()
            key = _key(address)
            if key in _USED:
                continue
            _USED.add(key)
            address["key"] = key
            return address
        import time
        address = _generate()
        address["line1"] = "%s, Unit %d-%d" % (address["line1"], int(time.time()) % 100000,
                                               random.randint(1, 999))
        address["key"] = _key(address)
        _USED.add(address["key"])
        return address
