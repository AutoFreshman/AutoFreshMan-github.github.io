#!/usr/bin/env python3
"""Simple reusable watcher for fixed domestic flight windows.

This script is intentionally config-driven instead of scraping one booking site
end-to-end. For each watched route, you maintain a shortlist of candidate
flight numbers and the script:

1. fetches the configured status page when possible
2. extracts the currently published scheduled departure / arrival times
3. filters by date weekday, departure cutoff and baggage requirement
4. prints a stable report that can be re-run or looped in watch mode

The default config in `flight_watch_config.json` is prefilled for:
- 2026-04-18 WUH -> KMG before 10:00
- 2026-04-18 YIH -> KMG before 10:00
- 2026-04-26 KMG -> WUH before 12:00
- 2026-04-26 KMG -> YIH before 12:00
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import html
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "flight_watch_config.json"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0 Safari/537.36"
)


AIRLINE_RULES: dict[str, dict[str, Any]] = {
    "CA": {
        "name": "中国国际航空",
        "confirmed_checked_baggage_kg": 20,
        "rule_source": "https://www.airchina.us/US/CN/info/checked-baggage/domestic.html",
        "note": "国航自营国内经济舱免费托运行李额 20kg。",
    },
    "MU": {
        "name": "中国东方航空",
        "confirmed_checked_baggage_kg": 20,
        "rule_source": "https://eb.ceair.com/activity/notice/app/index.html",
        "note": "东航自营国内经济舱免费托运行李额 20kg。",
    },
    "CZ": {
        "name": "中国南方航空",
        "confirmed_checked_baggage_kg": 20,
        "rule_source": "https://www.csair.com/cn/tourguide/luggage_service/carryon_luggage/free_luggage/",
        "note": "南航国内经济舱免费托运行李额 20kg。",
    },
    "DR": {
        "name": "瑞丽航空",
        "confirmed_checked_baggage_kg": None,
        "rule_source": "https://pages.c-ctrip.com/flight/h5/hybrid/booking/content/DRPassengerRequirement_Domestic.html?v=1",
        "note": "瑞丽航经济舱按舱位代码分 10/15/20kg，不可默认视为满足 20kg。",
    },
    "8L": {
        "name": "祥鹏航空",
        "confirmed_checked_baggage_kg": None,
        "rule_source": "https://www.luckyair.net/extraProduct/frontend/luggage/luggageIntroduce.jsp",
        "note": "官网仅能确认可单独预购行李，默认免费托运行李额需以票面为准。",
    },
    "KY": {
        "name": "昆明航空",
        "confirmed_checked_baggage_kg": 20,
        "rule_source": "https://b2a.airkunming.com/kyb2c/Passenger_information.html",
        "note": "昆航国内运输经济舱免费行李额 20kg。",
    },
}


@dataclasses.dataclass
class CandidateResult:
    flight_no: str
    airline_code: str
    airline_name: str
    departure: str
    arrival: str
    baggage_kg: int | None
    baggage_ok: bool
    match: bool
    reason: str
    source_url: str
    fetched_live: bool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch configured domestic flights.")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="Path to the JSON config file.",
    )
    parser.add_argument(
        "--repeat-minutes",
        type=int,
        default=0,
        help="Repeat every N minutes. 0 means run once.",
    )
    parser.add_argument(
        "--include-unconfirmed-baggage",
        action="store_true",
        help="Include flights whose free checked baggage cannot be confirmed as 20kg.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON.",
    )
    return parser.parse_args()


def load_config(path: str) -> dict[str, Any]:
    config_path = pathlib.Path(path).expanduser().resolve()
    with config_path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read().decode("utf-8", errors="ignore")
    raw = re.sub(r"<script.*?</script>", " ", raw, flags=re.S | re.I)
    raw = re.sub(r"<style.*?</style>", " ", raw, flags=re.S | re.I)
    raw = re.sub(r"<[^>]+>", "\n", raw)
    raw = html.unescape(raw)
    raw = raw.replace("\xa0", " ")
    return raw


def parse_airportia_schedule(text: str) -> tuple[str | None, str | None]:
    match = re.search(
        r"Departure:.*?Scheduled:\s*(\d{2}:\d{2}).*?Arrival:.*?Scheduled:\s*(\d{2}:\d{2})",
        text,
        flags=re.S,
    )
    if match:
        return match.group(1), match.group(2)

    pairs = re.findall(r"Scheduled:\s*(\d{2}:\d{2})", text)
    if len(pairs) >= 2:
        return pairs[0], pairs[1]
    return None, None


def parse_time(value: str) -> dt.time:
    return dt.datetime.strptime(value, "%H:%M").time()


def weekday_index(date_str: str) -> int:
    return dt.date.fromisoformat(date_str).isoweekday()


def resolve_schedule(candidate: dict[str, Any]) -> tuple[str, str, bool]:
    fallback_dep = candidate["fallback_departure"]
    fallback_arr = candidate["fallback_arrival"]
    source_url = candidate.get("status_url") or candidate.get("source_url", "")
    if not source_url:
        return fallback_dep, fallback_arr, False

    try:
        text = fetch_text(source_url)
    except (urllib.error.URLError, TimeoutError, OSError):
        return fallback_dep, fallback_arr, False

    dep, arr = parse_airportia_schedule(text)
    if dep and arr:
        return dep, arr, True
    return fallback_dep, fallback_arr, False


def evaluate_watch(
    watch: dict[str, Any],
    include_unconfirmed_baggage: bool,
) -> dict[str, Any]:
    required_baggage = int(watch["required_baggage_kg"])
    latest_departure = parse_time(watch["latest_departure"])
    target_weekday = weekday_index(watch["date"])
    results: list[CandidateResult] = []

    for candidate in watch["candidates"]:
        airline_code = candidate["airline_code"]
        airline_rule = AIRLINE_RULES.get(airline_code, {})
        airline_name = airline_rule.get("name", airline_code)
        baggage_kg = airline_rule.get("confirmed_checked_baggage_kg")
        dep, arr, fetched_live = resolve_schedule(candidate)

        reason = []
        match = True

        if target_weekday not in candidate["days"]:
            match = False
            reason.append("目标日期非该航班执飞日")

        if parse_time(dep) > latest_departure:
            match = False
            reason.append(f"起飞时间 {dep} 晚于限制 {watch['latest_departure']}")

        baggage_ok = baggage_kg is not None and baggage_kg >= required_baggage
        if not baggage_ok and not include_unconfirmed_baggage:
            match = False
            if baggage_kg is None:
                reason.append("免费托运行李额未能确认达到 20kg")
            else:
                reason.append(f"免费托运行李额仅 {baggage_kg}kg")
        elif not baggage_ok:
            reason.append("行李额未确认，已按宽松模式保留")

        results.append(
            CandidateResult(
                flight_no=candidate["flight_no"],
                airline_code=airline_code,
                airline_name=airline_name,
                departure=dep,
                arrival=arr,
                baggage_kg=baggage_kg,
                baggage_ok=baggage_ok,
                match=match,
                reason="；".join(reason) if reason else "符合条件",
                source_url=candidate.get("status_url") or candidate.get("source_url", ""),
                fetched_live=fetched_live,
            )
        )

    return {
        "label": watch["label"],
        "date": watch["date"],
        "origin": watch["origin"],
        "destination": watch["destination"],
        "latest_departure": watch["latest_departure"],
        "required_baggage_kg": required_baggage,
        "matches": [dataclasses.asdict(item) for item in results if item.match],
        "rejected": [dataclasses.asdict(item) for item in results if not item.match],
    }


def build_report(config: dict[str, Any], include_unconfirmed_baggage: bool) -> dict[str, Any]:
    watches = [
        evaluate_watch(watch, include_unconfirmed_baggage)
        for watch in config["watches"]
    ]
    return {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "include_unconfirmed_baggage": include_unconfirmed_baggage,
        "watches": watches,
        "airline_rules": AIRLINE_RULES,
    }


def render_text(report: dict[str, Any]) -> str:
    lines = [
        f"Flight watch report @ {report['generated_at']}",
        f"include_unconfirmed_baggage = {report['include_unconfirmed_baggage']}",
        "",
    ]
    for watch in report["watches"]:
        lines.append(
            f"== {watch['label']} | {watch['date']} | depart <= {watch['latest_departure']} | baggage >= {watch['required_baggage_kg']}kg =="
        )
        if watch["matches"]:
            lines.append("Matched flights:")
            for item in watch["matches"]:
                baggage = f"{item['baggage_kg']}kg" if item["baggage_kg"] is not None else "unknown"
                live_flag = "live" if item["fetched_live"] else "fallback"
                lines.append(
                    f"  - {item['flight_no']} {item['airline_name']} {item['departure']}->{item['arrival']} baggage={baggage} [{live_flag}]"
                )
        else:
            lines.append("Matched flights: none")

        if watch["rejected"]:
            lines.append("Rejected candidates:")
            for item in watch["rejected"]:
                baggage = f"{item['baggage_kg']}kg" if item["baggage_kg"] is not None else "unknown"
                live_flag = "live" if item["fetched_live"] else "fallback"
                lines.append(
                    f"  - {item['flight_no']} {item['airline_name']} {item['departure']}->{item['arrival']} baggage={baggage} [{live_flag}] | {item['reason']}"
                )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def report_digest(report: dict[str, Any]) -> str:
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)

    last_digest = None
    while True:
        report = build_report(config, args.include_unconfirmed_baggage)
        digest = report_digest(report)
        if digest != last_digest:
            if args.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                print(render_text(report), end="")
            last_digest = digest

        if args.repeat_minutes <= 0:
            return 0
        time.sleep(args.repeat_minutes * 60)


if __name__ == "__main__":
    sys.exit(main())
