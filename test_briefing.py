"""
daily_briefing.py 테스트 스크립트
기존 코드 건드리지 않고 독립 실행
"""
import yfinance as yf
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

def get_ticker_data(name, symbol, fmt_val, fmt_chg, is_percent=False):
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period="5d")
        if len(hist) < 2:
            return {"name": name, "value": "N/A", "change": "N/A", "pct": "N/A", "up": True}
        prev  = hist["Close"].iloc[-2]
        today = hist["Close"].iloc[-1]
        chg   = today - prev
        pct   = chg / prev * 100
        sign  = "+" if chg >= 0 else ""
        return {
            "name":   name,
            "value":  fmt_val(today),
            "change": f"{sign}{fmt_chg(chg)}",
            "pct":    f"{sign}{pct:.2f}%",
            "up":     chg >= 0,
        }
    except Exception as e:
        print(f"  [{name}] 오류: {e}")
        return {"name": name, "value": "N/A", "change": "N/A", "pct": "N/A", "up": True}

def collect_market_data():
    specs = [
        ("코스피",    "^KS11",    lambda v: f"{v:,.2f}",      lambda c: f"{c:,.2f}p"),
        ("코스닥",    "^KQ11",    lambda v: f"{v:,.2f}",      lambda c: f"{c:,.2f}p"),
        ("나스닥 100","^NDX",     lambda v: f"{v:,.2f}",      lambda c: f"{c:,.2f}p"),
        ("S&P 500",  "^GSPC",    lambda v: f"{v:,.2f}",      lambda c: f"{c:,.2f}p"),
        ("USD / KRW","USDKRW=X", lambda v: f"{v:,.1f}원",    lambda c: f"{c:,.1f}원"),
        ("EUR / KRW","EURKRW=X", lambda v: f"{v:,.1f}원",    lambda c: f"{c:,.1f}원"),
        ("옥수수 선물","ZC=F",    lambda v: f"${v/100:.2f}/bu", lambda c: f"${abs(c)/100:.3f}"),
        ("대두박 선물","ZM=F",    lambda v: f"${v:,.1f}/t",   lambda c: f"${abs(c):.1f}"),
    ]
    results = {}
    keys = ["kospi","kosdaq","nasdaq","sp500","usd_krw","eur_krw","corn","soymeal"]
    for i, (name, sym, fv, fc) in enumerate(specs):
        results[keys[i]] = get_ticker_data(name, sym, fv, fc)

    # 돈가 — 보류 (N/A)
    yesterday = (datetime.now(KST) - timedelta(days=1))
    results["dongga"] = {
        "today":     "N/A",
        "today_date": yesterday.strftime("%-m/%-d"),
        "chg":       "N/A",
        "chg_pct":   "N/A",
        "chg_up":    True,
        "yoy":       "N/A",
        "yoy_date":  (yesterday - timedelta(days=365)).strftime("%-m/%-d/%y"),
        "yoy_chg":   "N/A",
        "yoy_pct":   "N/A",
        "yoy_up":    True,
    }
    return results

if __name__ == "__main__":
    print("거시경제 지표 수집 중...")
    data = collect_market_data()
    print("\n=== 결과 ===")
    for key, val in data.items():
        if key == "dongga":
            print(f"돈가: {val['today']} ({val['today_date']})")
        else:
            arrow = "▲" if val["up"] else "▼"
            print(f"{val['name']:12} | {val['value']:>12} | {arrow} {val['change']:>10} | {val['pct']}")
