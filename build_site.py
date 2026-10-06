#!/usr/bin/env python3
"""Pull Charleston Sales by User and publish an encrypted snapshot.

Reads ORDERTRAC_STORAGE_STATE and DASHBOARD_PASSWORD from the environment.
Writes site/index.html and site/data.json. The JSON is AES-GCM ciphertext.
The page password decrypts it in the browser. Nothing here prints the password
or the OrderTrac cookies.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import re
import time
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

BASE = "https://app.ordertracinventory.com"
EASTERN = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"
CENTS = Decimal("0.01")
TENTH = Decimal("0.1")
MAX_DAYS = 365
KDF_ITERATIONS = 120_000


class BuildError(Exception):
    pass


def money(value: str) -> Decimal:
    text = (value or "").strip().replace(",", "").replace("$", "")
    return Decimal(text or "0")


def format_money(value: Decimal) -> str:
    rounded = value.quantize(CENTS, rounding=ROUND_HALF_UP)
    sign = "-" if rounded < 0 else ""
    whole, frac = f"{abs(rounded):.2f}".split(".")
    return f"{sign}${int(whole):,}.{frac}"


def display_name(first: str, last: str) -> str:
    def tidy(part: str) -> str:
        part = re.sub(r"\s+", " ", (part or "").strip())
        if part.isupper():
            return part.title()
        return part

    first_name = tidy(first)
    last_name = tidy(last)
    if first_name.casefold() == last_name.casefold():
        return first_name or last_name
    return f"{first_name} {last_name}".strip()


def session_from_env() -> requests.Session:
    raw = os.environ.get("ORDERTRAC_STORAGE_STATE", "").strip()
    if not raw:
        raise BuildError("ORDERTRAC_STORAGE_STATE is empty.")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BuildError("ORDERTRAC_STORAGE_STATE is not JSON.") from exc
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html, text/csv, */*",
            "Referer": BASE + "/reports",
        }
    )
    found = False
    for cookie in data.get("cookies") or []:
        domain = cookie.get("domain") or ""
        if "ordertracinventory.com" not in domain:
            continue
        name = cookie.get("name")
        value = cookie.get("value")
        if not name or value is None:
            continue
        session.cookies.set(name, value, domain=domain, path=cookie.get("path") or "/")
        if name == "ot_user_guid":
            found = True
    if not found:
        raise BuildError("OrderTrac session has no user cookie.")
    return session


def ordertrac_get(session: requests.Session, path: str, params: dict | None = None) -> requests.Response:
    last = "OrderTrac did not respond."
    for attempt in range(4):
        try:
            response = session.get(BASE + path, params=params, timeout=(20, 120))
        except requests.RequestException as exc:
            last = exc.__class__.__name__
            time.sleep(2 + attempt)
            continue
        if "/Account/Login" in response.url or "<title>login" in response.text[:800].lower():
            raise BuildError("OrderTrac session expired. Refresh the GitHub secret from this Mac.")
        if response.status_code == 200:
            return response
        last = f"HTTP {response.status_code}"
        time.sleep(2 + attempt)
    raise BuildError(f"OrderTrac failed ({last}).")


def select_block(page: str, name: str) -> str:
    match = re.search(rf'<select\b[^>]*name="{re.escape(name)}"[^>]*>(.*?)</select>', page, re.I | re.S)
    if not match:
        raise BuildError(f"Report form has no {name} field.")
    return match.group(1)


def options(select_html: str) -> list[dict]:
    found = []
    for match in re.finditer(r"<option\b([^>]*)>(.*?)</option>", select_html, re.I | re.S):
        attrs, inner = match.group(1), match.group(2)
        value_match = re.search(r'value="([^"]*)"', attrs)
        label = unescape(re.sub(r"<[^>]+>", "", inner))
        label = re.sub(r"\s+", " ", label).strip()
        found.append({"id": value_match.group(1) if value_match else "", "name": label or "ALL"})
    return found


def hidden(page: str, name: str) -> str:
    match = re.search(rf'name="{re.escape(name)}"[^>]*value="([^"]*)"', page)
    if not match:
        match = re.search(rf'value="([^"]*)"[^>]*name="{re.escape(name)}"', page)
    if not match:
        raise BuildError("Report page did not include its id.")
    return match.group(1)


def load_form(session: requests.Session) -> dict:
    page = ordertrac_get(session, "/reports").text
    card = re.search(
        r'data-quickreportguid="([a-f0-9-]{36})"[\s\S]{0,700}?Sales by User \(including Split Sales\)',
        page,
        re.I,
    )
    if not card or "Split sales attribute 50%" not in page[card.start() : card.start() + 900]:
        raise BuildError("Sales by User (including Split Sales) was not on the reports page.")
    form = ordertrac_get(
        session,
        "/reports/report",
        {"report": "QuickReport", "quickReportGUID": card.group(1)},
    ).text
    if "Sales by User (including Split Sales)" not in form:
        raise BuildError("OrderTrac opened a different report.")
    max_days = MAX_DAYS
    max_match = re.search(r'id="date-range-1-max-days"[^>]*value="(\d+)"', form)
    if max_match:
        max_days = int(max_match.group(1))
    return {
        "max_days": max_days,
        "account_guid": hidden(form, "request.AccountGUID"),
        "quick_report_guid": hidden(form, "request.QuickReportGUID"),
        "categories": options(select_block(form, "request.Select2")),
        "date_types": options(select_block(form, "request.Select1")),
        "locations": options(select_block(form, "request.LocationGUID")),
    }


def ranges(today: date, max_days: int) -> dict[str, tuple[date, date]]:
    month_start = today.replace(day=1)
    last_end = month_start - timedelta(days=1)
    last_start = last_end.replace(day=1)
    ytd_start = date(today.year, 1, 1)
    if (today - ytd_start).days > max_days:
        ytd_start = today - timedelta(days=max_days)
    return {
        "today": (today, today),
        "month": (month_start, today),
        "last": (last_start, last_end),
        "ytd": (ytd_start, today),
    }


def pull_csv(session: requests.Session, form: dict, start: date, end: date, category: str) -> list[dict]:
    params = {
        "request.AccountGUID": form["account_guid"],
        "request.QuickReportGUID": form["quick_report_guid"],
        "request.Report": "QuickReport",
        "request.DateRange1Start": f"{start.month}/{start.day}/{start.year}",
        "request.DateRange1End": f"{end.month}/{end.day}/{end.year}",
        "request.LocationGUID": "",
        "request.Select1": "DATE",
        "request.Select2": category,
        "request.ReportExportFormat": "CSV",
    }
    response = ordertrac_get(session, "/Reports/Download", params)
    if not response.content.strip():
        return []
    if "csv" not in (response.headers.get("Content-Type") or "").lower() and not response.content.startswith(b"Primary First"):
        raise BuildError("OrderTrac did not return the sales CSV.")
    reader = csv.DictReader(io.StringIO(response.content.decode("utf-8-sig", "replace")))
    fieldnames = [name.strip() for name in (reader.fieldnames or [])]
    expected = ["Primary First", "Primary Last", "All Sales", "Commissionable Sales"]
    if fieldnames[:4] != expected:
        raise BuildError("OrderTrac changed the Sales by User columns.")
    rows = []
    for index, raw in enumerate(reader):
        if not any((raw.get(key) or "").strip() for key in expected[:3]):
            continue
        all_sales = money(raw.get("All Sales") or "0")
        rows.append(
            {
                "order": index,
                "name": display_name(raw.get("Primary First") or "", raw.get("Primary Last") or ""),
                "all_sales": format(all_sales, "f"),
                "all_display": format_money(all_sales),
            }
        )
    return rows


def report_payload(rows: list[dict], start: date, end: date, category_name: str, pulled_at: str) -> dict:
    total = sum((money(row["all_sales"]) for row in rows), Decimal(0))
    for row in rows:
        if total == 0:
            share = Decimal(0)
        else:
            share = (money(row["all_sales"]) / total * Decimal(100)).quantize(TENTH, rounding=ROUND_HALF_UP)
        row["share"] = f"{share:.1f}"
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "location": "All locations",
        "date_type": "Sales Order Date",
        "category": category_name or "ALL",
        "pulled_at": pulled_at,
        "total_all_display": format_money(total),
        "rows": rows,
    }


def encrypt(payload: dict, password: str) -> dict:
    salt = os.urandom(16)
    iv = os.urandom(12)
    key = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=KDF_ITERATIONS,
    ).derive(password.encode("utf-8"))
    ciphertext = AESGCM(key).encrypt(iv, json.dumps(payload).encode("utf-8"), None)
    return {
        "v": 1,
        "iter": KDF_ITERATIONS,
        "salt": base64.b64encode(salt).decode(),
        "iv": base64.b64encode(iv).decode(),
        "ct": base64.b64encode(ciphertext).decode(),
    }


def main() -> None:
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if not password:
        raise BuildError("DASHBOARD_PASSWORD is empty.")
    session = session_from_env()
    form = load_form(session)
    if not any(item["id"] == "DATE" for item in form["date_types"]):
        raise BuildError("OrderTrac did not offer a sales order date.")
    if not any(item["id"] == "" for item in form["locations"]):
        raise BuildError("OrderTrac did not offer all locations.")
    if not any(item["id"] == "" for item in form["categories"]):
        raise BuildError("OrderTrac did not offer all categories.")
    today = datetime.now(EASTERN).date()
    windows = ranges(today, int(form["max_days"]))
    pulled_at = datetime.now(EASTERN).strftime("%b %-d, %Y · %-I:%M %p ET")
    reports: dict[str, dict] = {}
    total = len(windows)
    done = 0
    for preset, (start, end) in windows.items():
        done += 1
        print(f"[{done}/{total}] {preset} all categories", flush=True)
        rows = pull_csv(session, form, start, end, "")
        reports[preset] = report_payload(rows, start, end, "All categories", pulled_at)
        time.sleep(0.2)
    snapshot = {
        "pulled_at": pulled_at,
        "reports": reports,
    }
    stable = {
        key: {field: value for field, value in report.items() if field != "pulled_at"}
        for key, report in reports.items()
    }
    plain = json.dumps(stable, sort_keys=True)
    SITE.mkdir(parents=True, exist_ok=True)
    (SITE / "plain-hash.txt").write_text(hashlib.sha256(plain.encode()).hexdigest() + "\n", encoding="utf-8")
    stamp = datetime.now(EASTERN).strftime("%Y%m%d%H%M%S")
    html = (ROOT / "index.html").read_text(encoding="utf-8").replace("data.json", f"data.json?v={stamp}")
    (SITE / "index.html").write_text(html, encoding="utf-8")
    (SITE / "data.json").write_text(json.dumps(encrypt(snapshot, password)), encoding="utf-8")
    print(f"Wrote {total} reports.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except BuildError as exc:
        raise SystemExit(str(exc)) from exc
