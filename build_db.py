# -*- coding: utf-8 -*-
"""
build_db.py - Goi API that lay SMDT ma + nganh cho toan bo 191 ma hien hanh,
luu vao SQLite (data.db) de app.py doc ra tuc thi, khong can goi API moi lan bam.

Chay: python build_db.py
Nen chay lai dinh ky (VD: cron/scheduled task moi sang sau khi co du lieu phien
moi nhat) de cap nhat db.

Doc lap voi code trong chatbotgpt / stocktraders-mcp da chot - khong dung code
tu 2 repo do, chi goi thang API cong khai https://stocktradersai.vn.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from datetime import date

import requests

for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass

API_BASE = "https://stocktradersai.vn"
REQUEST_TIMEOUT = 20
HISTORY_SESSIONS = 70  # ~3 thang + it buffer
DB_PATH = "data.db"

# 191 ma hien hanh (whitelist)
TICKERS = """
FIT TLH PVS PGB PVT IDJ SHS IDI TTF VCB IDC SZC VTP VCG VCI L14 VCK HT1 TCH LPB
SHB PNJ AAA BSR TCM APS HHS TCB HHV TLG SAB C4G JVC MWG SIP POW SAM QNS FRT BSI
DTD GEG GVR NVL TV2 BCC HAG HSG HAH BCM GEX VJC QTP VEA BVH VIX HUT ASM BVS VDS
ACB VRE CMG BMI PPC BVB BMP SSI FTS EVF KLB TCX CMX GMD YEG BFC FMC SSB NAB HTN
LAS LHG VCS ABB CNG SBT CEO TDH PHR KDH VSC TDC CTR CTS KBC VGI NTC NKG VPB ORS
LSS MBS VGC TPB VPI QCG MSH VPL OCB NT2 FOX DXG NBC HDC SCR HDB ITC MBB DXS HDG
MSN HPG MST VPX MSR CSV DGC GAS GIL PTB DPG SMC PC1 MSB STB HVN CTG KSB LCG DDV
DGW CTD DHC CTI VIP NVB BID TNG NDN VND DCM PET LDG VIC FCN MIG VNM IJC DPR VIB
APG AGR D2D EIB DPM OIL AGG REE DRC ANV PLC NLG DBC VHM HCM DIG PDR PVC VHC PVD
MHC CII KHG MPC FPT PLX VGS VOS SGB NTL HQC
""".split()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS tickers (
        ticker TEXT PRIMARY KEY,
        branch_name TEXT,
        branch_path TEXT
    );
    CREATE TABLE IF NOT EXISTS smdt_ticker (
        ticker TEXT,
        date TEXT,
        smdt REAL,
        PRIMARY KEY (ticker, date)
    );
    CREATE TABLE IF NOT EXISTS smdt_branch (
        branch_path TEXT,
        date TEXT,
        smdt REAL,
        PRIMARY KEY (branch_path, date)
    );
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """)
    conn.commit()


def fetch_branch(ticker: str) -> dict | None:
    try:
        res = requests.post(
            f"{API_BASE}/service/data/getBranchPath",
            params={"ticker": ticker},
            timeout=REQUEST_TIMEOUT,
        )
        res.raise_for_status()
        data = res.json()
    except Exception as exc:
        print(f"  [LOI branch] {ticker}: {exc}")
        return None
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    return {"name": first.get("name") or first.get("path"), "path": first.get("path")}


def fetch_smdt(*, ticker: str | None = None, path: str | None = None, base_date: str) -> list[tuple[str, float]]:
    params = {"n": HISTORY_SESSIONS, "baseDate": base_date}
    if ticker:
        params["ticker"] = ticker
    if path:
        params["path"] = path
    try:
        res = requests.post(
            f"{API_BASE}/service/data/getSMDTLastN",
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        res.raise_for_status()
        data = res.json()
    except Exception as exc:
        who = ticker or path
        print(f"  [LOI smdt] {who}: {exc}")
        return []
    smdts = data.get("smdts") if isinstance(data, dict) else None
    if not smdts:
        return []
    points = [(str(item["date"])[:10], float(item["smdt"])) for item in smdts if "date" in item and "smdt" in item]
    by_date = {d: v for d, v in points}
    return sorted(by_date.items())


def main() -> None:
    today = date.today().isoformat()
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    print(f"=== Buoc 1/3: lay ngành cho {len(TICKERS)} mã ===")
    ticker_branch: dict[str, dict] = {}
    for i, t in enumerate(TICKERS, 1):
        branch = fetch_branch(t)
        if branch and branch.get("path"):
            ticker_branch[t] = branch
            conn.execute(
                "INSERT OR REPLACE INTO tickers (ticker, branch_name, branch_path) VALUES (?, ?, ?)",
                (t, branch["name"], branch["path"]),
            )
        else:
            print(f"  [BO QUA] {t}: không tìm thấy ngành")
        if i % 20 == 0:
            print(f"  ... {i}/{len(TICKERS)}")
            conn.commit()
    conn.commit()
    print(f"-> Lấy được ngành cho {len(ticker_branch)}/{len(TICKERS)} mã")

    unique_paths = sorted({b["path"] for b in ticker_branch.values()})
    print(f"\n=== Buoc 2/3: lay SMDT ngành cho {len(unique_paths)} ngành duy nhất ===")
    for i, path in enumerate(unique_paths, 1):
        points = fetch_smdt(path=path, base_date=today)
        for d, v in points:
            conn.execute(
                "INSERT OR REPLACE INTO smdt_branch (branch_path, date, smdt) VALUES (?, ?, ?)",
                (path, d, v),
            )
        if i % 10 == 0:
            print(f"  ... {i}/{len(unique_paths)}")
            conn.commit()
    conn.commit()
    print(f"-> Xong SMDT ngành")

    print(f"\n=== Buoc 3/3: lay SMDT mã cho {len(ticker_branch)} mã ===")
    ok_count = 0
    for i, t in enumerate(ticker_branch, 1):
        points = fetch_smdt(ticker=t, base_date=today)
        if points:
            ok_count += 1
        for d, v in points:
            conn.execute(
                "INSERT OR REPLACE INTO smdt_ticker (ticker, date, smdt) VALUES (?, ?, ?)",
                (t, d, v),
            )
        if i % 20 == 0:
            print(f"  ... {i}/{len(ticker_branch)}")
            conn.commit()
    conn.commit()
    print(f"-> Lấy được SMDT cho {ok_count}/{len(ticker_branch)} mã")

    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_updated', ?)", (today,))
    conn.commit()
    conn.close()
    print(f"\n=== HOÀN TẤT — data.db đã sẵn sàng (cập nhật đến ngày {today}) ===")


if __name__ == "__main__":
    main()
