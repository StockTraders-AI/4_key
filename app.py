# -*- coding: utf-8 -*-
"""
4-Key Explorer - server Flask doc lap.
Chay: python app.py
Mac dinh lang nghe tai http://0.0.0.0:5000

Doc du lieu tu data.db (SQLite) da duoc build san boi build_db.py - tra cuu
tuc thi, KHONG goi API moi lan bam. Chay lai `python build_db.py` dinh ky
(VD: moi sang sau khi co du lieu phien moi) de cap nhat data.db.

Day la ban demo/thu nghiem doc lap: KHONG dung code trong repo chatbotgpt hay
stocktraders-mcp da chot.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, render_template_string

app = Flask(__name__)

DB_PATH = Path(__file__).parent / "data.db"

# So phien hien thi toi da tren giao dien - de rat cao de hien thi toan bo lich su
# co trong data.db (khong con gioi han ~3 thang nhu truoc).
DISPLAY_SESSIONS = 5000

LOOKBACK = 3
PERCENTILE = 0.45
MIN_SAMPLES = 10

KEY_DEFS = {
    (True, True): ("Đúng sóng - Đúng ngành", "MUA - tín hiệu thuận cả 2 chiều"),
    (True, False): ("Đúng sóng - Sai ngành", "CÂN NHẮC - mã mạnh riêng lẻ, ngược dòng ngành"),
    (False, True): ("Sai sóng - Đúng ngành", "THEO DÕI - ngành thuận nhưng mã chưa xác nhận"),
    (False, False): ("Sai sóng - Sai ngành", "TRÁNH - cả 2 chiều bất lợi"),
}


def key_of(right_wave: bool, right_branch: bool) -> tuple[str, str]:
    return KEY_DEFS[(right_wave, right_branch)]


# ---------------------------------------------------------------------------
# 1. DOC TU data.db (da duoc build_db.py fetch san tu API that)
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_lookup_branch(ticker: str) -> Optional[dict]:
    conn = get_db()
    row = conn.execute(
        "SELECT branch_name, branch_path FROM tickers WHERE ticker = ?", (ticker,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {"name": row["branch_name"], "path": row["branch_path"]}


def db_lookup_smdt_ticker(ticker: str) -> list[tuple[str, float]]:
    conn = get_db()
    rows = conn.execute(
        "SELECT date, smdt FROM smdt_ticker WHERE ticker = ? ORDER BY date", (ticker,)
    ).fetchall()
    conn.close()
    return [(r["date"], r["smdt"]) for r in rows]


def db_lookup_price(ticker: str) -> list[tuple[str, float]]:
    conn = get_db()
    rows = conn.execute(
        "SELECT date, close FROM price_ticker WHERE ticker = ? ORDER BY date", (ticker,)
    ).fetchall()
    conn.close()
    return [(r["date"], r["close"]) for r in rows]


def db_lookup_cashflow(ticker: str) -> dict[str, str]:
    conn = get_db()
    rows = conn.execute(
        "SELECT date, content FROM cashflow_ticker WHERE ticker = ? ORDER BY date", (ticker,)
    ).fetchall()
    conn.close()
    return {r["date"]: r["content"] for r in rows}


def db_lookup_peer_smdt_by_date(branch_path: str, exclude_ticker: str) -> dict[str, list[float]]:
    """Cho tung ngay, tra ve danh sach SMDT cua cac ma KHAC cung nganh (peer) -
    du lieu nay da co san trong bang smdt_ticker (khong can goi them API nao)."""
    conn = get_db()
    peer_rows = conn.execute(
        "SELECT ticker FROM tickers WHERE branch_path = ? AND ticker != ?",
        (branch_path, exclude_ticker),
    ).fetchall()
    peer_tickers = [r["ticker"] for r in peer_rows]
    if not peer_tickers:
        conn.close()
        return {}
    placeholders = ",".join("?" * len(peer_tickers))
    rows = conn.execute(
        f"SELECT date, smdt FROM smdt_ticker WHERE ticker IN ({placeholders})",
        peer_tickers,
    ).fetchall()
    conn.close()
    by_date: dict[str, list[float]] = {}
    for r in rows:
        by_date.setdefault(r["date"], []).append(r["smdt"])
    return by_date


def db_lookup_smdt_branch(branch_path: str) -> list[tuple[str, float]]:
    conn = get_db()
    rows = conn.execute(
        "SELECT date, smdt FROM smdt_branch WHERE branch_path = ? ORDER BY date", (branch_path,)
    ).fetchall()
    conn.close()
    return [(r["date"], r["smdt"]) for r in rows]


def db_last_updated() -> Optional[str]:
    conn = get_db()
    row = conn.execute("SELECT value FROM meta WHERE key = 'last_updated'").fetchone()
    conn.close()
    return row["value"] if row else None


# ---------------------------------------------------------------------------
# 2. LOGIC TINH NHOM 4-KEY (cach hien tai + cach de xuat)
# ---------------------------------------------------------------------------

def percentile(sorted_vals: list[float], q: float) -> float:
    idx = min(int(len(sorted_vals) * q), len(sorted_vals) - 1)
    return sorted_vals[idx]


def compute_rows(dates: list[str], ticker_vals: list[float], branch_vals: list[float]) -> list[Optional[dict]]:
    n = len(dates)
    rows: list[Optional[dict]] = []
    prev_w: Optional[bool] = None
    prev_b: Optional[bool] = None

    for i in range(n):
        if i < LOOKBACK:
            rows.append(None)
            continue

        dt = ticker_vals[i] - ticker_vals[i - LOOKBACK]
        db = branch_vals[i] - branch_vals[i - LOOKBACK]

        old_group, old_rec = key_of(dt > 0, db > 0)

        hist_t = [abs(ticker_vals[j] - ticker_vals[j - LOOKBACK]) for j in range(LOOKBACK, i + 1)]
        hist_b = [abs(branch_vals[j] - branch_vals[j - LOOKBACK]) for j in range(LOOKBACK, i + 1)]
        sorted_t = sorted(hist_t)
        sorted_b = sorted(hist_b)
        th_t = percentile(sorted_t, PERCENTILE) if len(hist_t) >= MIN_SAMPLES else 0.0
        th_b = percentile(sorted_b, PERCENTILE) if len(hist_b) >= MIN_SAMPLES else 0.0

        used_t = abs(dt) >= th_t
        used_b = abs(db) >= th_b
        right_wave = (dt > 0) if used_t else (prev_w if prev_w is not None else dt > 0)
        right_branch = (db > 0) if used_b else (prev_b if prev_b is not None else db > 0)
        prev_key = key_of(prev_w, prev_b) if (prev_w is not None and prev_b is not None) else None
        prev_w, prev_b = right_wave, right_branch

        new_group, new_rec = key_of(right_wave, right_branch)

        rows.append({
            "date": dates[i],
            "smdt_ticker": round(ticker_vals[i], 2),
            "smdt_ticker_prev": round(ticker_vals[i - LOOKBACK], 2),
            "smdt_branch": round(branch_vals[i], 2),
            "smdt_branch_prev": round(branch_vals[i - LOOKBACK], 2),
            "delta_ticker": round(dt, 2),
            "delta_branch": round(db, 2),
            "threshold_ticker": round(th_t, 2),
            "threshold_branch": round(th_b, 2),
            "used_ticker_threshold": used_t,
            "used_branch_threshold": used_b,
            "n_history": len(hist_t),
            "sorted_ticker_deltas": [round(v, 2) for v in sorted_t],
            "sorted_branch_deltas": [round(v, 2) for v in sorted_b],
            "old_group": old_group,
            "old_recommendation": old_rec,
            "new_group": new_group,
            "new_recommendation": new_rec,
            "prev_group": prev_key[0] if prev_key else None,
        })

    return rows


CASHFLOW_MAP = {
    "Tiếp tục đổ vào": 1.0, "Đang đổ vào": 1.0, "Nhen nhóm đổ vào": 0.5,
    "Tiếp tục thoát ra": -1.0, "Đang thoát ra": -1.0, "Bắt đầu thoát ra": -0.5,
}
SCORE_WEIGHTS = {
    "smdt_vs_nganh": 32.0,
    "smdt_delta": 30.0,
    "smdt_rank": 18.0,
    "gia_dong_luong": 10.0,
    "dong_tien": 10.0,
}


def normalize_series(values: list[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi == lo:
        return [50.0] * len(values)
    return [(v - lo) / (hi - lo) * 100.0 for v in values]


def rating_of(score: float) -> str:
    if score >= 70:
        return "MUA MẠNH"
    if score >= 55:
        return "MUA"
    if score >= 45:
        return "TRUNG LẬP"
    if score >= 30:
        return "BÁN"
    return "BÁN MẠNH"


def compute_scores(
    dates: list[str],
    ticker_vals: list[float],
    branch_vals: list[float],
    price_map: dict[str, float],
    cashflow_map: dict[str, str],
    peer_smdt_by_date: dict[str, list[float]],
) -> dict[str, dict]:
    """Composite Score (0-100), giong cong thuc goc trong stock_4key_evaluator.py,
    bao gom ca yeu to smdt_rank (18%, xep hang so voi cac ma cung nganh)."""
    n = len(dates)
    smdt_vs_nganh_vals = [ticker_vals[i] - branch_vals[i] for i in range(n)]
    delta_vals = [
        (ticker_vals[i] - ticker_vals[i - LOOKBACK]) if i >= LOOKBACK else 0.0
        for i in range(n)
    ]
    vs_scores = normalize_series(smdt_vs_nganh_vals)
    delta_scores = normalize_series(delta_vals)

    rank_scores: list[Optional[float]] = [None] * n
    rank_peer_count: list[int] = [0] * n
    for i in range(n):
        peers = peer_smdt_by_date.get(dates[i]) or []
        if not peers:
            continue
        combined = peers + [ticker_vals[i]]
        normed = normalize_series(combined)
        rank_scores[i] = normed[-1]
        rank_peer_count[i] = len(peers)

    price_series = [price_map.get(d) for d in dates]
    has_price = any(v is not None for v in price_series)
    price_scores: list[Optional[float]] = [None] * n
    one_day_returns: list[Optional[float]] = [None] * n
    if has_price:
        returns = []
        for i in range(1, n):
            prev_p, cur_p = price_series[i - 1], price_series[i]
            returns.append((cur_p / prev_p - 1.0) if (prev_p and cur_p is not None) else 0.0)
        norm_returns = normalize_series(returns)
        for i in range(1, n):
            one_day_returns[i] = returns[i - 1]
            price_scores[i] = norm_returns[i - 1]

    out: dict[str, dict] = {}
    for i in range(n):
        active_weights = dict(SCORE_WEIGHTS)
        breakdown = {
            "smdt_vs_nganh": round(vs_scores[i], 1),
            "smdt_delta": round(delta_scores[i], 1),
        }
        weighted_sum = active_weights["smdt_vs_nganh"] * vs_scores[i] + active_weights["smdt_delta"] * delta_scores[i]

        if rank_scores[i] is None:
            active_weights.pop("smdt_rank", None)
        else:
            weighted_sum += active_weights["smdt_rank"] * rank_scores[i]
            breakdown["smdt_rank"] = round(rank_scores[i], 1)
            breakdown["smdt_rank_peer_count"] = rank_peer_count[i]

        if not has_price or price_scores[i] is None:
            active_weights.pop("gia_dong_luong", None)
        else:
            weighted_sum += active_weights["gia_dong_luong"] * price_scores[i]
            breakdown["gia_dong_luong"] = round(price_scores[i], 1)
            breakdown["gia_return_1d_pct"] = round(one_day_returns[i] * 100.0, 2)

        cf_content = cashflow_map.get(dates[i])
        if cf_content and cf_content in CASHFLOW_MAP:
            cf_score = (CASHFLOW_MAP[cf_content] + 1.0) / 2.0 * 100.0
            breakdown["dong_tien_label"] = cf_content
        else:
            cf_score = 50.0
        weighted_sum += active_weights["dong_tien"] * cf_score
        breakdown["dong_tien"] = round(cf_score, 1)

        total_w = sum(active_weights.values())
        score = max(0.0, min(100.0, weighted_sum / total_w)) if total_w else 0.0
        out[dates[i]] = {"score": round(score, 1), "rating": rating_of(score), "breakdown": breakdown}
    return out


def flips_stats(rows: list[dict], field: str) -> dict:
    valid = [r[field] for r in rows]
    flips = sum(1 for i in range(1, len(valid)) if valid[i] != valid[i - 1])
    n = len(valid)
    streak = round(n / (flips + 1), 2) if n else 0.0
    return {"flips": flips, "n": n, "streak": streak}


def evaluate(ticker: str) -> dict:
    ticker = ticker.strip().upper()
    if not ticker or not ticker.isalnum() or len(ticker) > 6:
        return {"error": f"Mã \"{ticker}\" không hợp lệ."}

    branch = db_lookup_branch(ticker)
    if not branch or not branch.get("path"):
        return {"error": f"Mã {ticker} không có trong cơ sở dữ liệu (175 mã đã build). Kiểm tra lại mã, hoặc chạy `python build_db.py` để cập nhật danh sách."}

    ticker_smdt = db_lookup_smdt_ticker(ticker)
    branch_smdt = db_lookup_smdt_branch(branch["path"])
    if not ticker_smdt:
        return {"error": f"Không có dữ liệu SMDT cho mã {ticker} trong data.db."}
    if not branch_smdt:
        return {"error": f"Không có dữ liệu SMDT ngành \"{branch['name']}\" trong data.db."}

    branch_map = dict(branch_smdt)
    merged = [(d, v, branch_map[d]) for d, v in ticker_smdt if d in branch_map]
    merged.sort(key=lambda x: x[0])
    if len(merged) < LOOKBACK + MIN_SAMPLES:
        return {"error": f"Mã {ticker} chưa đủ lịch sử giao dịch để tính (cần tối thiểu {LOOKBACK + MIN_SAMPLES} phiên)."}

    dates = [m[0] for m in merged]
    ticker_vals = [m[1] for m in merged]
    branch_vals = [m[2] for m in merged]

    all_rows = compute_rows(dates, ticker_vals, branch_vals)
    valid_rows = [r for r in all_rows if r is not None]
    display_rows = valid_rows[-DISPLAY_SESSIONS:]

    price_map = dict(db_lookup_price(ticker))
    cashflow_map = db_lookup_cashflow(ticker)
    peer_smdt_by_date = db_lookup_peer_smdt_by_date(branch["path"], ticker)
    scores_by_date = compute_scores(dates, ticker_vals, branch_vals, price_map, cashflow_map, peer_smdt_by_date)
    for r in display_rows:
        s = scores_by_date.get(r["date"])
        if s:
            r["score"] = s["score"]
            r["rating"] = s["rating"]
            r["score_breakdown"] = s["breakdown"]

    old_stats = flips_stats(display_rows, "old_group")
    new_stats = flips_stats(display_rows, "new_group")
    reduction = round((1 - new_stats["flips"] / old_stats["flips"]) * 100, 1) if old_stats["flips"] else 0.0

    return {
        "ticker": ticker,
        "branch": branch["name"],
        "rows": display_rows,
        "old_stats": old_stats,
        "new_stats": new_stats,
        "reduction_pct": reduction,
    }


# ---------------------------------------------------------------------------
# 3. API
# ---------------------------------------------------------------------------

@app.get("/api/evaluate/<ticker>")
def api_evaluate(ticker: str):
    result = evaluate(ticker)
    if "error" in result:
        return jsonify(result), 404
    return jsonify(result)


@app.get("/api/tickers")
def api_tickers():
    conn = get_db()
    rows = conn.execute("SELECT ticker FROM tickers ORDER BY ticker").fetchall()
    conn.close()
    return jsonify({"tickers": [r["ticker"] for r in rows], "last_updated": db_last_updated()})


# ---------------------------------------------------------------------------
# 4. TRANG GIAO DIEN
# ---------------------------------------------------------------------------

INDEX_HTML = """
<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>4-Key Explorer</title>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap">
<style>
  :root{
    --bg:#F6F4EF; --surface:#FFFFFF; --surface-2:#EFEBE2;
    --border:#E1DCCF; --border-strong:#C9C2AF;
    --text:#232019; --text-2:#5B5648; --text-3:#8B8574;
    --accent:#3D6B5C; --accent-ink:#F2FBF7; --accent-weak:#E1EEE8;
    --good-bg:#E7F3E1; --good-fg:#2A5C1E;
    --warn-bg:#FBF0DB; --warn-fg:#7A5410;
    --info-bg:#E5EEF8; --info-fg:#1E4E82;
    --bad-bg:#FBE8E5;  --bad-fg:#8C2A1E;
    --shadow: 0 1px 2px rgba(35,32,25,.06), 0 4px 14px rgba(35,32,25,.05);
  }
  @media (prefers-color-scheme: dark){
    :root{
      --bg:#18170F; --surface:#211F17; --surface-2:#292619;
      --border:#3A3627; --border-strong:#4C4732;
      --text:#EFEADF; --text-2:#B9B29B; --text-3:#847C64;
      --accent:#6FBF9F; --accent-ink:#0E2019; --accent-weak:#233A31;
      --good-bg:#213821; --good-fg:#8FD98A;
      --warn-bg:#3A2F14; --warn-fg:#E8C066;
      --info-bg:#1B2E42; --info-fg:#8FC2EE;
      --bad-bg:#3A1F1A;  --bad-fg:#F0968A;
      --shadow: 0 1px 2px rgba(0,0,0,.3), 0 6px 20px rgba(0,0,0,.35);
    }
  }
  *{box-sizing:border-box}
  body{background:var(--bg);color:var(--text);font-family:Inter,system-ui,sans-serif;margin:0}
  .mono{font-family:"JetBrains Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums}
  .wrap{max-width:1080px;margin:0 auto;padding:28px 20px 56px}
  h1{font-size:21px;font-weight:700;letter-spacing:-.01em;margin:0 0 4px}
  .sub{color:var(--text-2);font-size:13.5px;margin:0 0 22px;line-height:1.55}
  .panel{background:var(--surface);border:1px solid var(--border);border-radius:14px;box-shadow:var(--shadow)}
  .searchbar{display:flex;gap:10px;padding:16px;flex-wrap:wrap;align-items:center}
  .searchbar input{
    flex:1;min-width:160px;background:var(--surface-2);border:1px solid var(--border-strong);
    border-radius:9px;padding:11px 14px;font-size:16px;font-weight:600;letter-spacing:.02em;
    color:var(--text);text-transform:uppercase;font-family:"JetBrains Mono",monospace;
  }
  .searchbar input:focus{outline:2px solid var(--accent);outline-offset:1px}
  .searchbar button{
    background:var(--accent);color:var(--accent-ink);border:none;border-radius:9px;
    padding:11px 20px;font-size:14.5px;font-weight:600;cursor:pointer;font-family:inherit;
  }
  .searchbar button:active{transform:scale(.98)}
  .searchbar button:disabled{opacity:.6;cursor:default}
  .hint{color:var(--text-3);font-size:12.5px;padding:0 16px 14px}
  .msg{padding:34px 20px;text-align:center;color:var(--text-2);font-size:14px}
  .msg b{color:var(--text)}

  .result{display:flex;flex-direction:column;gap:18px;margin-top:20px}
  .head-row{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
  .ticker-name{font-size:26px;font-weight:700;letter-spacing:-.01em}
  .branch-name{color:var(--text-2);font-size:13.5px}

  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
  .stat{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
  .stat .label{font-size:11.5px;color:var(--text-3);text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px}
  .stat .value{font-size:22px;font-weight:700;font-family:"JetBrains Mono",monospace}
  .stat .value small{font-size:13px;font-weight:500;color:var(--text-2)}
  .stat.accent{background:var(--accent-weak)}
  .stat.accent .value{color:var(--accent)}

  .tabs{display:flex;gap:6px;padding:12px 16px 0;flex-wrap:wrap}
  .tab{
    padding:7px 14px;border-radius:8px 8px 0 0;font-size:13px;font-weight:600;cursor:pointer;
    color:var(--text-2);border:1px solid transparent;
  }
  .tab.active{background:var(--surface-2);color:var(--text);border-color:var(--border);border-bottom-color:var(--surface-2)}

  .date-nav{position:relative;display:inline-flex;align-items:center;gap:8px;margin:14px 16px 0}
  .date-nav-btn{
    background:var(--surface-2);border:1px solid var(--border);color:var(--text);border-radius:8px;
    padding:6px 10px;cursor:pointer;font-size:13px;font-weight:600;font-family:inherit;
  }
  .date-nav-btn:disabled{opacity:.4;cursor:default}
  .date-current{
    display:flex;align-items:center;gap:6px;background:var(--surface-2);border:1px solid var(--border);
    border-radius:8px;padding:6px 14px;cursor:pointer;font-size:13px;font-weight:700;color:var(--accent);
    font-family:"JetBrains Mono",monospace;
  }
  .calendar-popup{
    position:absolute;top:calc(100% + 8px);left:0;z-index:20;
    background:var(--surface);border:1px solid var(--border);border-radius:14px;box-shadow:var(--shadow);
    padding:14px;width:280px;
  }
  .calendar-popup.hidden{display:none}
  .cal-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
  .cal-head button{background:transparent;border:none;color:var(--text-2);font-size:16px;cursor:pointer;padding:4px 8px;border-radius:6px;font-family:inherit}
  .cal-head button:hover:not(:disabled){background:var(--surface-2)}
  .cal-head button:disabled{opacity:.3;cursor:default}
  .cal-month-label{font-size:14px;font-weight:700;color:var(--text)}
  .cal-weekdays{display:grid;grid-template-columns:repeat(7,1fr);text-align:center;font-size:10.5px;color:var(--text-3);font-weight:600;margin-bottom:4px}
  .cal-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:2px}
  .cal-day{
    aspect-ratio:1;display:flex;align-items:center;justify-content:center;border-radius:8px;
    font-size:12.5px;font-family:"JetBrains Mono",monospace;color:var(--text);cursor:pointer;border:none;background:transparent;
  }
  .cal-day:hover:not(:disabled){background:var(--surface-2)}
  .cal-day.outside{color:var(--text-3);opacity:.4}
  .cal-day:disabled{cursor:default;opacity:.3}
  .cal-day.selected{background:var(--accent);color:var(--accent-ink);font-weight:700}
  .cal-footer{display:flex;justify-content:space-between;margin-top:10px;padding-top:10px;border-top:1px solid var(--border)}
  .cal-footer button{background:none;border:none;font-size:12.5px;font-weight:600;cursor:pointer;font-family:inherit}
  .cal-today-btn{color:var(--accent)}
  .cal-close-btn{color:var(--text-2)}

  .pager{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:12px 16px 0;flex-wrap:wrap}
  .pager .page-label{font-size:13px;font-weight:600;color:var(--text)}
  .pager .page-nav{display:flex;gap:8px}
  .pager button{
    background:var(--surface-2);color:var(--text);border:1px solid var(--border);border-radius:8px;
    padding:6px 13px;font-size:13px;font-weight:600;cursor:pointer;font-family:inherit;
  }
  .pager button:disabled{opacity:.4;cursor:default}
  tr.month-divider td{
    background:var(--surface-2);color:var(--text-3);font-weight:700;font-size:10.5px;
    text-transform:uppercase;letter-spacing:.04em;padding:7px 10px;
  }
  .tablewrap{overflow-x:auto;border-top:1px solid var(--border)}
  table{width:100%;border-collapse:collapse;font-size:12.5px;min-width:760px}
  th{
    text-align:left;padding:9px 10px;background:var(--surface-2);color:var(--text-3);
    font-size:10.5px;text-transform:uppercase;letter-spacing:.04em;font-weight:600;
    position:sticky;top:0;border-bottom:1px solid var(--border);white-space:nowrap;
  }
  td{padding:8px 10px;border-bottom:1px solid var(--border);white-space:nowrap;font-family:"JetBrains Mono",monospace}
  tr:last-child td{border-bottom:none}
  tr.diff td{background:var(--warn-bg)}
  td.num{text-align:right}
  .badge{
    display:inline-block;padding:3px 9px;border-radius:100px;font-size:11.5px;font-weight:600;
    font-family:Inter,sans-serif;white-space:nowrap;
  }
  .b-good{background:var(--good-bg);color:var(--good-fg)}
  .b-warn{background:var(--warn-bg);color:var(--warn-fg)}
  .b-info{background:var(--info-bg);color:var(--info-fg)}
  .b-bad{background:var(--bad-bg);color:var(--bad-fg)}
  .flag{font-size:10.5px;color:var(--text-3);font-family:Inter,sans-serif}
  .legend{display:flex;gap:14px;flex-wrap:wrap;padding:14px 16px;font-size:12px;color:var(--text-2)}
  .legend span{display:inline-flex;align-items:center;gap:6px}
  .dot{width:9px;height:9px;border-radius:3px;display:inline-block}
  tr.row-click{cursor:pointer}
  tr.row-click:hover td{background:var(--surface-2)}
  tr.selected td{background:var(--accent-weak)!important}

  .detail{padding:18px 16px;border-top:1px solid var(--border);background:var(--surface-2)}
  .detail h3{margin:0 0 2px;font-size:15px}
  .detail .detail-sub{color:var(--text-2);font-size:12.5px;margin:0 0 14px}
  .detail-tabs{display:flex;gap:6px;margin-bottom:14px}
  .detail-tab{
    padding:7px 14px;border-radius:8px;font-size:12.5px;font-weight:600;cursor:pointer;
    color:var(--text-2);background:var(--surface);border:1px solid var(--border);
  }
  .detail-tab.active{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}
  .detail-pane{display:none}
  .detail-pane.active{display:block}
  .detail-card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px;margin-bottom:12px}
  .detail-card h4{margin:0 0 10px;font-size:12.5px;text-transform:uppercase;letter-spacing:.04em;color:var(--text-3)}
  .detail-card p{margin:0 0 9px;font-size:13.5px;line-height:1.65;color:var(--text)}
  .detail-card p:last-child{margin-bottom:0}
  .detail-card .num{font-family:"JetBrains Mono",monospace;font-weight:600}
  .detail-conclusion{margin-top:10px;padding-top:10px;border-top:1px dashed var(--border)}
  .close-detail{
    float:right;background:none;border:none;color:var(--text-3);font-size:13px;cursor:pointer;
    font-family:inherit;padding:2px 6px;
  }
  .close-detail:hover{color:var(--text)}
  .sorted-list{
    font-family:"JetBrains Mono",monospace;font-size:12px;color:var(--text-2);
    background:var(--surface-2);border-radius:8px;padding:10px 12px;line-height:1.9;
    overflow-x:auto;white-space:nowrap;margin-bottom:8px;
  }
  .sorted-list .pick{background:var(--accent);color:var(--accent-ink);padding:1px 5px;border-radius:4px;font-weight:700}
  footer{color:var(--text-3);font-size:11.5px;text-align:center;margin-top:24px;line-height:1.6}
  @media (max-width:520px){
    .ticker-name{font-size:21px}
    table{min-width:680px}
  }
</style>
</head>
<body>
<div class="wrap">
  <h1>4-Key Explorer</h1>
  <p class="sub">Nhập bất kỳ mã cổ phiếu nào để xem Nhóm 4-Key toàn bộ lịch sử có trong cơ sở dữ liệu, so sánh cách tính hiện tại (chỉ nhìn dấu +/-) với hướng đề xuất (ngưỡng thích ứng theo lịch sử riêng từng mã, phân vị 45%). Dữ liệu đọc từ data.db, build sẵn từ API stocktradersai.vn.</p>

  <div class="panel">
    <div class="searchbar">
      <input id="tickerInput" type="text" placeholder="Nhập mã, ví dụ TCB" maxlength="10" autocomplete="off">
      <button id="runBtn">Xem</button>
    </div>
    <div class="hint" id="hintLine">Đang tải danh sách mã có sẵn...</div>
  </div>

  <div id="output"></div>

  <footer>Dữ liệu SMDT thật, đọc từ data.db (build sẵn từ API stocktradersai.vn). Chạy <code>python build_db.py</code> để cập nhật. Server Flask độc lập, không đụng vào code chính thức đã chốt.</footer>
</div>

<script>
const KEYS_CLASS = {
  "Đúng sóng - Đúng ngành": "b-good",
  "Đúng sóng - Sai ngành": "b-warn",
  "Sai sóng - Đúng ngành": "b-info",
  "Sai sóng - Sai ngành": "b-bad",
};

function fmt(v){ return (v>=0?"+":"") + v.toFixed(2); }
function fmt2(v){ return v.toFixed(2); }
function badge(name){ return `<span class="badge ${KEYS_CLASS[name]||''}">${name}</span>`; }
const RATING_CLASS = {
  "MUA MẠNH": "b-good", "MUA": "b-good",
  "TRUNG LẬP": "b-warn",
  "BÁN": "b-bad", "BÁN MẠNH": "b-bad",
};
function ratingBadge(score, rating){
  if (score === undefined || score === null) return `<span class="flag">—</span>`;
  return `<span class="badge ${RATING_CLASS[rating]||''}">${score.toFixed(1)}</span>`;
}
function dmy(iso){ const [y,m,d]=iso.split("-"); return `${d}/${m}`; }
function renderSortedList(sorted, pickIdx){
  return sorted.map((v,i) => i===pickIdx ? `<span class="pick">${v.toFixed(2)}</span>` : v.toFixed(2)).join(", ");
}

async function render(sym){
  const out = document.getElementById("output");
  const btn = document.getElementById("runBtn");
  btn.disabled = true;
  out.innerHTML = `<div class="panel msg">Đang gọi API lấy dữ liệu ${sym}...</div>`;
  let data;
  try{
    const res = await fetch("/api/evaluate/" + encodeURIComponent(sym));
    const body = await res.json();
    if (!res.ok){
      out.innerHTML = `<div class="panel msg">${body.error}</div>`;
      btn.disabled = false;
      return;
    }
    data = body;
  }catch(e){
    out.innerHTML = `<div class="panel msg">Lỗi kết nối tới server.</div>`;
    btn.disabled = false;
    return;
  }
  btn.disabled = false;

  const rows = data.rows;

  // gom nhom theo thang (YYYY-MM) tu du lieu tra ve, khong co dinh cung
  const monthKeys = [...new Set(rows.map(r => r.date.slice(0,7)))];
  const monthLabel = (k) => { const [y,m] = k.split("-"); return `Tháng ${parseInt(m,10)}/${y}`; };

  out.innerHTML = `
  <div class="result">
    <div class="head-row">
      <div>
        <div class="ticker-name">${data.ticker}</div>
        <div class="branch-name">${data.branch}</div>
      </div>
    </div>

    <div class="stats">
      <div class="stat"><div class="label">Số lần đổi — cách cũ</div><div class="value">${data.old_stats.flips}<small> / ${data.old_stats.n} phiên</small></div></div>
      <div class="stat accent"><div class="label">Số lần đổi — cách mới</div><div class="value">${data.new_stats.flips}<small> / ${data.new_stats.n} phiên</small></div></div>
      <div class="stat"><div class="label">Giảm nhiễu</div><div class="value">${data.reduction_pct}%</div></div>
      <div class="stat"><div class="label">Độ dài TB mỗi nhóm</div><div class="value">${data.old_stats.streak}<small> → ${data.new_stats.streak} phiên</small></div></div>
    </div>

    <div class="panel">
      <div class="legend">
        <span><span class="dot" style="background:var(--good-fg)"></span>Đúng sóng - Đúng ngành (MUA)</span>
        <span><span class="dot" style="background:var(--warn-fg)"></span>Đúng sóng - Sai ngành (CÂN NHẮC)</span>
        <span><span class="dot" style="background:var(--info-fg)"></span>Sai sóng - Đúng ngành (THEO DÕI)</span>
        <span><span class="dot" style="background:var(--bad-fg)"></span>Sai sóng - Sai ngành (TRÁNH)</span>
      </div>
      <div class="date-nav">
        <button class="date-nav-btn" id="dayPrev">‹</button>
        <button class="date-current" id="dateOpen">📅 <span id="dateOpenLabel"></span></button>
        <button class="date-nav-btn" id="dayNext">›</button>
        <div class="calendar-popup hidden" id="calendarPopup">
          <div class="cal-head">
            <button id="calPrevMonth">‹</button>
            <div class="cal-month-label" id="calMonthLabel"></div>
            <button id="calNextMonth">›</button>
          </div>
          <div class="cal-weekdays"><span>CN</span><span>T2</span><span>T3</span><span>T4</span><span>T5</span><span>T6</span><span>T7</span></div>
          <div class="cal-grid" id="calGrid"></div>
          <div class="cal-footer">
            <button class="cal-today-btn" id="calToday">Hôm nay</button>
            <button class="cal-close-btn" id="calClose">Đóng</button>
          </div>
        </div>
      </div>
      <div class="pager" id="pager"></div>
      <div class="tablewrap"><table id="dataTable"></table></div>
      <div id="detailPanel"></div>
    </div>
  </div>`;

  const pagerEl = document.getElementById("pager");
  const tableEl = document.getElementById("dataTable");

  // gom moi 3 thang lien tiep thanh 1 trang, phan trang thay vi liet ke het thang
  const MONTHS_PER_PAGE = 3;
  const monthPages = [];
  for (let i = 0; i < monthKeys.length; i += MONTHS_PER_PAGE) monthPages.push(monthKeys.slice(i, i + MONTHS_PER_PAGE));
  const pageLabel = (mks) => mks.length > 1
    ? `${monthLabel(mks[0])} — ${monthLabel(mks[mks.length-1])}`
    : monthLabel(mks[0]);

  const theadHtml = `<tr>
    <th>Ngày</th><th>SMDT mã</th><th>Δ mã</th><th>Ngưỡng mã</th><th>Δ ngành</th><th>Ngưỡng ngành</th>
    <th>Key — cách cũ</th><th>Key — cách mới</th><th>Score</th>
  </tr>`;
  function rowHtml(r, i){
    const diff = r.old_group !== r.new_group;
    return `<tr class="${diff?'diff':''} row-click" data-i="${i}">
      <td>${dmy(r.date)}</td>
      <td class="num">${fmt2(r.smdt_ticker)}</td>
      <td class="num">${fmt(r.delta_ticker)}</td>
      <td class="num">${r.threshold_ticker.toFixed(2)} <span class="flag">(${r.used_ticker_threshold?"cập nhật":"giữ nguyên"})</span></td>
      <td class="num">${fmt(r.delta_branch)}</td>
      <td class="num">${r.threshold_branch.toFixed(2)} <span class="flag">(${r.used_branch_threshold?"cập nhật":"giữ nguyên"})</span></td>
      <td>${badge(r.old_group)}</td>
      <td>${badge(r.new_group)}</td>
      <td>${ratingBadge(r.score, r.rating)}</td>
    </tr>`;
  }
  function attachRowClicks(){
    tableEl.querySelectorAll("tr.row-click").forEach(tr => {
      tr.addEventListener("click", () => {
        tableEl.querySelectorAll("tr").forEach(x => x.classList.remove("selected"));
        tr.classList.add("selected");
        showDetail(+tr.dataset.i);
      });
    });
  }

  let singleDateMode = false;

  function renderPage(pIdx){
    singleDateMode = false;
    const mks = monthPages[pIdx];
    pagerEl.innerHTML = `
      <div class="page-nav">
        <button id="pagePrev" ${pIdx<=0?"disabled":""}>‹ Trước</button>
        <button id="pageNext" ${pIdx>=monthPages.length-1?"disabled":""}>Sau ›</button>
      </div>
      <div class="page-label">${pageLabel(mks)} <span class="flag">(trang ${pIdx+1}/${monthPages.length})</span></div>`;
    document.getElementById("pagePrev").addEventListener("click", () => renderPage(pIdx-1));
    document.getElementById("pageNext").addEventListener("click", () => renderPage(pIdx+1));

    let tbody = "";
    let lastMonth = null;
    rows.forEach((r, i) => {
      const mk = r.date.slice(0,7);
      if (!mks.includes(mk)) return;
      if (mk !== lastMonth) {
        tbody += `<tr class="month-divider"><td colspan="9">${monthLabel(mk)}</td></tr>`;
        lastMonth = mk;
      }
      tbody += rowHtml(r, i);
    });
    tableEl.innerHTML = theadHtml + tbody;
    document.getElementById("detailPanel").innerHTML = "";
    attachRowClicks();
  }

  function goToFullPageForIndex(idx){
    const mk = rows[idx].date.slice(0,7);
    let pageIdx = monthPages.findIndex(mks => mks.includes(mk));
    if (pageIdx === -1) pageIdx = monthPages.length - 1;
    renderPage(pageIdx);
    const tr = tableEl.querySelector(`tr.row-click[data-i="${idx}"]`);
    if (tr) {
      tableEl.querySelectorAll("tr").forEach(x => x.classList.remove("selected"));
      tr.classList.add("selected");
      tr.scrollIntoView({block:"center", behavior:"smooth"});
    }
  }

  function renderSingleDate(idx){
    singleDateMode = true;
    const r = rows[idx];
    pagerEl.innerHTML = `
      <div class="page-nav">
        <button id="viewAllBtn">Xem tất cả ↺</button>
      </div>
      <div class="page-label">Đang xem 1 phiên — ${monthLabel(r.date.slice(0,7))}, ngày ${dmy(r.date)}</div>`;
    document.getElementById("viewAllBtn").addEventListener("click", () => goToFullPageForIndex(idx));

    tableEl.innerHTML = theadHtml + rowHtml(r, idx);
    document.getElementById("detailPanel").innerHTML = "";
    attachRowClicks();
    tableEl.querySelector(`tr.row-click[data-i="${idx}"]`)?.classList.add("selected");
  }

  renderPage(monthPages.length - 1);

  // ==== Bo chon ngay (date nav + calendar popup) ====
  const rowIndexByDate = new Map(rows.map((r,i) => [r.date, i]));
  const fullDmy = (iso) => { const [y,m,d]=iso.split("-"); return `${d}/${m}/${y}`; };
  const pad2 = (n) => String(n).padStart(2,"0");
  const ymd = (y,m,d) => `${y}-${pad2(m+1)}-${pad2(d)}`;
  let selectedIdx = rows.length - 1;
  let calYear, calMonth;

  const dateOpenBtn = document.getElementById("dateOpen");
  const dateOpenLabel = document.getElementById("dateOpenLabel");
  const dayPrevBtn = document.getElementById("dayPrev");
  const dayNextBtn = document.getElementById("dayNext");
  const calendarPopup = document.getElementById("calendarPopup");
  const calMonthLabel = document.getElementById("calMonthLabel");
  const calGrid = document.getElementById("calGrid");

  function updateDateNav(){
    dateOpenLabel.textContent = fullDmy(rows[selectedIdx].date);
    dayPrevBtn.disabled = selectedIdx <= 0;
    dayNextBtn.disabled = selectedIdx >= rows.length - 1;
  }

  function jumpToIndex(idx, openDetail=true){
    idx = Math.max(0, Math.min(rows.length - 1, idx));
    selectedIdx = idx;
    renderSingleDate(idx);
    if (openDetail) showDetail(idx);
    updateDateNav();
  }

  function renderCalendar(){
    calMonthLabel.textContent = `Tháng ${calMonth+1}, ${calYear}`;
    const startWeekday = new Date(Date.UTC(calYear, calMonth, 1)).getUTCDay();
    const daysInMonth = new Date(Date.UTC(calYear, calMonth+1, 0)).getUTCDate();
    const prevMonthDays = new Date(Date.UTC(calYear, calMonth, 0)).getUTCDate();
    const selectedDate = rows[selectedIdx].date;
    let cells = "";
    for (let i = 0; i < 42; i++){
      const dayOffset = i - startWeekday + 1;
      let y = calYear, m = calMonth, d, outside = false;
      if (dayOffset < 1) { d = prevMonthDays + dayOffset; m -= 1; outside = true; }
      else if (dayOffset > daysInMonth) { d = dayOffset - daysInMonth; m += 1; outside = true; }
      else { d = dayOffset; }
      if (m < 0) { m = 11; y -= 1; } else if (m > 11) { m = 0; y += 1; }
      const dateStr = ymd(y, m, d);
      const hasData = rowIndexByDate.has(dateStr);
      const isSelected = dateStr === selectedDate;
      cells += `<button class="cal-day ${outside?"outside":""} ${isSelected?"selected":""}" data-date="${dateStr}" ${hasData?"":"disabled"}>${d}</button>`;
    }
    calGrid.innerHTML = cells;
    calGrid.querySelectorAll(".cal-day:not(:disabled)").forEach(btn => {
      btn.addEventListener("click", () => {
        jumpToIndex(rowIndexByDate.get(btn.dataset.date));
        calendarPopup.classList.add("hidden");
      });
    });
  }

  dayPrevBtn.addEventListener("click", () => jumpToIndex(selectedIdx - 1));
  dayNextBtn.addEventListener("click", () => jumpToIndex(selectedIdx + 1));
  dateOpenBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    const [y,m] = rows[selectedIdx].date.split("-");
    calYear = parseInt(y,10); calMonth = parseInt(m,10) - 1;
    renderCalendar();
    calendarPopup.classList.toggle("hidden");
  });
  document.getElementById("calPrevMonth").addEventListener("click", () => {
    calMonth -= 1; if (calMonth < 0) { calMonth = 11; calYear -= 1; }
    renderCalendar();
  });
  document.getElementById("calNextMonth").addEventListener("click", () => {
    calMonth += 1; if (calMonth > 11) { calMonth = 0; calYear += 1; }
    renderCalendar();
  });
  document.getElementById("calToday").addEventListener("click", () => {
    jumpToIndex(rows.length - 1);
    calendarPopup.classList.add("hidden");
  });
  document.getElementById("calClose").addEventListener("click", () => calendarPopup.classList.add("hidden"));
  document.addEventListener("click", (e) => {
    if (!calendarPopup.classList.contains("hidden") && !e.target.closest(".date-nav")) {
      calendarPopup.classList.add("hidden");
    }
  });

  updateDateNav();

  function showDetail(idx){
    const r = rows[idx];
    const date_ = dmy(r.date);
    const prevDate = idx >= 3 ? dmy(rows[idx-3] ? rows[idx-3].date : r.date) : "?";
    const idxT = Math.min(Math.floor(r.sorted_ticker_deltas.length * 0.45), r.sorted_ticker_deltas.length - 1);
    const idxB = Math.min(Math.floor(r.sorted_branch_deltas.length * 0.45), r.sorted_branch_deltas.length - 1);

    const oldWaveWord = r.delta_ticker > 0 ? "đúng sóng" : "sai sóng";
    const oldBranchWord = r.delta_branch > 0 ? "đúng ngành" : "sai ngành";

    function prevWord(isWave){
      const label = isWave ? ["đúng sóng","sai sóng"] : ["đúng ngành","sai ngành"];
      if (!r.prev_group) return isWave ? (r.delta_ticker>0?label[0]:label[1]) : (r.delta_branch>0?label[0]:label[1]);
      return isWave ? (r.prev_group.startsWith("Đúng sóng")?label[0]:label[1])
                    : (r.prev_group.includes("Đúng ngành")?label[0]:label[1]);
    }

    function stepThreshold(dim, delta, th, used, sorted, pickIdx, n, isWave){
      const label = isWave ? ["đúng sóng","sai sóng"] : ["đúng ngành","sai ngành"];
      const concl = used
        ? `độ lớn <span class="num">${Math.abs(delta).toFixed(2)}%</span> ≥ ngưỡng → tin là biến động <b>thật</b> → kết luận <b>"${delta>0?label[0]:label[1]}"</b>`
        : `độ lớn <span class="num">${Math.abs(delta).toFixed(2)}%</span> &lt; ngưỡng → coi là <b>nhiễu</b> → giữ nguyên kết luận phiên trước: <b>"${prevWord(isWave)}"</b>`;
      return `
        <div class="detail-card">
          <h4>Ngưỡng ${dim}</h4>
          <p>Lấy <span class="num">${n}</span> phiên lịch sử gần nhất tính đến ${date_}, tính độ lớn biến động 3 phiên (|delta|) tại từng phiên, sắp xếp từ nhỏ đến lớn:</p>
          <div class="sorted-list">${renderSortedList(sorted, pickIdx)}</div>
          <p>Vị trí phân vị 45% = <span class="num">int(${n} × 0.45) = ${pickIdx}</span> (đếm từ 0) → giá trị tại vị trí đó = <span class="num">${sorted[pickIdx].toFixed(2)}</span> → <b>Ngưỡng ${dim} = ${th.toFixed(2)}%</b></p>
          <p>So sánh: ${concl}.</p>
        </div>`;
    }

    const oldPane = `
      <div class="detail-pane" id="paneOld">
        <div class="detail-card">
          <h4>Dữ liệu gốc</h4>
          <p>SMDT mã hôm nay (${date_}) = <span class="num">${fmt2(r.smdt_ticker)}%</span>, 3 phiên trước (${prevDate}) = <span class="num">${fmt2(r.smdt_ticker_prev)}%</span>.</p>
          <p>SMDT ngành hôm nay = <span class="num">${fmt2(r.smdt_branch)}%</span>, 3 phiên trước = <span class="num">${fmt2(r.smdt_branch_prev)}%</span>.</p>
        </div>
        <div class="detail-card">
          <h4>Tính delta</h4>
          <p>delta mã = ${fmt2(r.smdt_ticker)} − ${fmt2(r.smdt_ticker_prev)} = <span class="num">${fmt(r.delta_ticker)}%</span></p>
          <p>delta ngành = ${fmt2(r.smdt_branch)} − ${fmt2(r.smdt_branch_prev)} = <span class="num">${fmt(r.delta_branch)}%</span></p>
        </div>
        <div class="detail-card">
          <h4>Quy tắc hiện tại đang áp dụng</h4>
          <p>Cách hiện tại chỉ nhìn dấu của delta, không quan tâm độ lớn, không có ngưỡng tối thiểu, và không nhớ kết luận của phiên trước.</p>
          <p>delta mã ${fmt(r.delta_ticker)}% là số ${r.delta_ticker>0?"dương":"âm"} → kết luận ngay là <b>"${oldWaveWord}"</b>.</p>
          <p>delta ngành ${fmt(r.delta_branch)}% là số ${r.delta_branch>0?"dương":"âm"} → kết luận ngay là <b>"${oldBranchWord}"</b>.</p>
          <div class="detail-conclusion">Kết luận theo cách hiện tại: ${badge(r.old_group)}</div>
        </div>
      </div>`;

    const newPane = `
      <div class="detail-pane active" id="paneNew">
        <div class="detail-card">
          <h4>Dữ liệu gốc</h4>
          <p>SMDT mã hôm nay (${date_}) = <span class="num">${fmt2(r.smdt_ticker)}%</span>, 3 phiên trước (${prevDate}) = <span class="num">${fmt2(r.smdt_ticker_prev)}%</span>.</p>
          <p>SMDT ngành hôm nay = <span class="num">${fmt2(r.smdt_branch)}%</span>, 3 phiên trước = <span class="num">${fmt2(r.smdt_branch_prev)}%</span>.</p>
        </div>
        <div class="detail-card">
          <h4>Tính delta</h4>
          <p>delta mã = ${fmt2(r.smdt_ticker)} − ${fmt2(r.smdt_ticker_prev)} = <span class="num">${fmt(r.delta_ticker)}%</span></p>
          <p>delta ngành = ${fmt2(r.smdt_branch)} − ${fmt2(r.smdt_branch_prev)} = <span class="num">${fmt(r.delta_branch)}%</span></p>
        </div>
        ${stepThreshold("mã", r.delta_ticker, r.threshold_ticker, r.used_ticker_threshold, r.sorted_ticker_deltas, idxT, r.n_history, true)}
        ${stepThreshold("ngành", r.delta_branch, r.threshold_branch, r.used_branch_threshold, r.sorted_branch_deltas, idxB, r.n_history, false)}
        <div class="detail-card">
          <h4>Kết luận theo cách đề xuất</h4>
          <p>Chiều mã → <b>${r.used_ticker_threshold ? (r.delta_ticker>0?"đúng sóng":"sai sóng") : prevWord(true)}</b> (${r.used_ticker_threshold?"cập nhật":"giữ nguyên"}). Chiều ngành → <b>${r.used_branch_threshold ? (r.delta_branch>0?"đúng ngành":"sai ngành") : prevWord(false)}</b> (${r.used_branch_threshold?"cập nhật":"giữ nguyên"}).</p>
          <div class="detail-conclusion">${badge(r.new_group)}</div>
        </div>
      </div>`;

    const bd = r.score_breakdown || {};
    const hasPrice = bd.gia_dong_luong !== undefined;
    const hasRank = bd.smdt_rank !== undefined;
    const lvl = v => v>=80?"rất mạnh":v>=60?"mạnh":v>=40?"trung bình":v>=20?"yếu":"rất yếu";
    const diffTicker = fmt(r.smdt_ticker - r.smdt_branch);
    const diffWord = (r.smdt_ticker - r.smdt_branch) >= 0 ? "nhiều hơn" : "ít hơn";
    const deltaWord = r.delta_ticker >= 0 ? "tăng thêm" : "giảm mất";
    const priceWord = (bd.gia_return_1d_pct ?? 0) >= 0 ? "tăng" : "giảm";
    const scorePane = `
      <div class="detail-pane" id="paneScore">
        <div class="detail-card">
          <h4>5 yếu tố tính điểm (Composite Score)</h4>
          <p class="detail-sub">Mỗi yếu tố dưới đây được quy về thang <b>0-100</b> để so sánh công bằng: 100 = tốt nhất trong nhóm so sánh (lịch sử hoặc các mã cùng ngành), 0 = kém nhất, 50 = trung bình. Điểm càng cao càng tích cực cho mã.</p>
          <p><b>Dòng tiền vào mã so với vào ngành</b> (trọng số 32%): dòng tiền vào mã đang <b>${diffWord}</b> dòng tiền vào cả ngành (chênh lệch ${diffTicker}). So với 3 tháng gần đây, mức chênh lệch này thuộc nhóm <b>${lvl(bd.smdt_vs_nganh)}</b> (điểm quy đổi ${bd.smdt_vs_nganh}/100).</p>
          <p><b>Đà tăng/giảm dòng tiền vào mã</b> (trọng số 30%): so với 3 phiên trước, dòng tiền vào mã <b>${deltaWord} ${fmt2(Math.abs(r.delta_ticker))}%</b>. So với lịch sử, mức thay đổi này thuộc nhóm <b>${lvl(bd.smdt_delta)}</b> (điểm quy đổi ${bd.smdt_delta}/100).</p>
          ${hasRank
            ? `<p><b>Xếp hạng so với mã cùng ngành</b> (trọng số 18%): so với <b>${bd.smdt_rank_peer_count}</b> mã khác cùng ngành trong cùng ngày, dòng tiền vào mã này thuộc nhóm <b>${lvl(bd.smdt_rank)}</b> trong ngành (điểm quy đổi ${bd.smdt_rank}/100 — càng cao nghĩa là xếp hạng càng tốt trong ngành).</p>`
            : `<p><b>Xếp hạng so với mã cùng ngành</b> (trọng số 18%): <i>không có mã cùng ngành nào có dữ liệu cho ngày này</i> → tạm bỏ yếu tố này, dồn trọng số sang các yếu tố còn lại.</p>`}
          ${hasPrice
            ? `<p><b>Đà tăng/giảm giá</b> (trọng số 10%): giá ${priceWord} <b>${fmt2(Math.abs(bd.gia_return_1d_pct))}%</b> trong phiên gần nhất. So với lịch sử, mức thay đổi này thuộc nhóm <b>${lvl(bd.gia_dong_luong)}</b> (điểm quy đổi ${bd.gia_dong_luong}/100).</p>`
            : `<p><b>Đà tăng/giảm giá</b> (trọng số 10%): <i>không có dữ liệu giá cho ngày này</i> → tạm bỏ yếu tố này, dồn trọng số sang các yếu tố còn lại.</p>`}
          <p><b>Tín hiệu dòng tiền</b> (trọng số 10%): ${bd.dong_tien_label ? `tín hiệu hiện tại là <b>"${bd.dong_tien_label}"</b>` : "không có dữ liệu (tính mặc định trung lập)"} → mức <b>${lvl(bd.dong_tien)}</b> (điểm quy đổi ${bd.dong_tien}/100).</p>
        </div>
        <div class="detail-card">
          <h4>Cách gộp thành điểm tổng</h4>
          <p>Điểm tổng là trung bình cộng của 5 điểm trên, nhưng mỗi điểm được "cân" theo mức độ quan trọng của nó (trọng số): dòng tiền vào ngành nặng nhất (32%), rồi đến đà dòng tiền (30%), xếp hạng ngành (18%), còn giá và tín hiệu dòng tiền mỗi cái 10%. Yếu tố nào thiếu dữ liệu thì bỏ qua, phần trọng số còn lại chia đều cho các yếu tố có dữ liệu.</p>
          <p>Điểm càng cao thì khuyến nghị càng tích cực: ≥70 <b>MUA MẠNH</b> · ≥55 <b>MUA</b> · ≥45 <b>TRUNG LẬP</b> · ≥30 <b>BÁN</b> · &lt;30 <b>BÁN MẠNH</b>.</p>
          <div class="detail-conclusion">Điểm tổng = <span class="num">${r.score !== undefined ? r.score.toFixed(1) : "—"}</span> → ${ratingBadge(r.score, r.rating)}</div>
        </div>
      </div>`;

    const html = `
      <div class="detail">
        <button class="close-detail" id="closeDetail">Đóng ✕</button>
        <h3>Chi tiết cách tính — ${data.ticker}, ngày ${date_}</h3>
        <p class="detail-sub">Mọi con số dưới đây đều tính lại riêng cho đúng phiên này.</p>
        <div class="detail-tabs">
          <div class="detail-tab active" data-pane="paneNew">Cách tính đề xuất</div>
          <div class="detail-tab" data-pane="paneOld">Cách tính hiện tại</div>
          <div class="detail-tab" data-pane="paneScore">Cách tính Score</div>
        </div>
        ${newPane}
        ${oldPane}
        ${scorePane}
      </div>`;
    const panel = document.getElementById("detailPanel");
    panel.innerHTML = html;
    panel.querySelectorAll(".detail-tab").forEach(t => {
      t.addEventListener("click", () => {
        panel.querySelectorAll(".detail-tab").forEach(x=>x.classList.remove("active"));
        panel.querySelectorAll(".detail-pane").forEach(x=>x.classList.remove("active"));
        t.classList.add("active");
        document.getElementById(t.dataset.pane).classList.add("active");
      });
    });
    document.getElementById("closeDetail").addEventListener("click", () => { panel.innerHTML = ""; tableEl.querySelectorAll("tr").forEach(x=>x.classList.remove("selected")); });
    panel.scrollIntoView({behavior:"smooth", block:"nearest"});
  }
}

async function loadTickerList(){
  try{
    const res = await fetch("/api/tickers");
    const body = await res.json();
    const n = body.tickers.length;
    document.getElementById("hintLine").innerHTML =
      `Có sẵn <b>${n}</b> mã trong cơ sở dữ liệu (cập nhật đến ${body.last_updated || "?"}). Gõ mã bất kỳ trong số đó, ví dụ TCB, HPG, MWG...`;
  }catch(e){
    document.getElementById("hintLine").textContent = "Không tải được danh sách mã.";
  }
}

document.getElementById("runBtn").addEventListener("click", () => {
  const v = document.getElementById("tickerInput").value.trim().toUpperCase();
  if (v) render(v);
});
document.getElementById("tickerInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("runBtn").click();
});
loadTickerList();
document.getElementById("tickerInput").value = "TCB";
render("TCB");
</script>
</body>
</html>
"""


@app.get("/")
def index():
    return render_template_string(INDEX_HTML)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
