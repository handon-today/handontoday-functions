"""
================================================================
  일일 시황 브리핑 생성 모듈 — daily_briefing.py
  v1.0.0
================================================================
[역할]
  매일 오전 6시 KST, 전날 발행된 기사들을 바탕으로
  거시경제 + 양돈 시황 브리핑 기사를 자동 생성 후 DB 저장.

[데이터 소스]
  - 거시경제/환율/선물: yfinance (API 키 불필요)
  - 돈가: 추후 data.go.kr API 연동 예정 (현재 N/A)
  - 뉴스 요약: OpenRouter (Gemini 2.5 Flash Lite)

[카테고리]
  category = '국내'

[테스트 완료 항목]
  - yfinance 8개 지표 ✅
  - AI JSON 콘텐츠 생성 ✅
  - HTML 생성 + 렌더링 ✅
"""

import os
import re
import json
import urllib.request
import yfinance as yf
from datetime import datetime, timedelta, timezone
from sqlalchemy import text

KST = timezone(timedelta(hours=9))
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "google/gemini-2.5-flash-lite"


# ──────────────────────────────────────────────────
# 1. 거시경제 지표 수집 (yfinance)
# ──────────────────────────────────────────────────

def _get_ticker(name, symbol, fmt_val, fmt_chg):
    """yfinance로 단일 지표 수집"""
    try:
        hist = yf.Ticker(symbol).history(period="5d")
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
            "change": f"{sign}{fmt_chg(abs(chg))}",
            "pct":    f"{sign}{pct:.2f}%",
            "up":     chg >= 0,
        }
    except Exception as e:
        print(f"  [{name}] 지표 오류: {e}")
        return {"name": name, "value": "N/A", "change": "N/A", "pct": "N/A", "up": True}


def _fetch_dongga():
    """돈가 — 축산물품질평가원 돈육대표가격 API"""
    EKAPE_KEY = os.getenv("EKAPE_API_KEY", "")
    KST = timezone(timedelta(hours=9))
    yesterday = datetime.now(KST) - timedelta(days=1)
    day_before = datetime.now(KST) - timedelta(days=2)
    year_ago   = datetime.now(KST) - timedelta(days=366)

    def _get_price(target_date):
        ymd = target_date.strftime("%Y%m%d")
        url = (
            f"https://data.ekape.or.kr/openapi-data/service/user/grade"
            f"/auct/pigRepresentativePrice"
            f"?serviceKey={EKAPE_KEY}&pageNo=1&numOfRows=10"
            f"&startYmd={ymd}&endYmd={ymd}"
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                import xml.etree.ElementTree as ET
                root = ET.fromstring(r.read().decode())
                items = root.findall(".//item")
                if not items:
                    return None
                # 탕박(dongpiCode=1) 전체 평균가
                for item in items:
                    row = {c.tag: c.text for c in item}
                    print(f"  [돈가 raw] {row}")
                    # avgPrice 또는 price 필드
                    price = row.get("avgPrice") or row.get("price") or row.get("avgAucpc")
                    if price:
                        return int(float(price.replace(",", "")))
        except Exception as e:
            print(f"  [돈가 오류] {e}")
        return None

    today_price  = _get_price(yesterday)
    before_price = _get_price(day_before)
    yoy_price    = _get_price(year_ago)

    chg     = round(today_price - before_price) if today_price and before_price else None
    chg_pct = round(chg / before_price * 100, 2) if chg and before_price else None
    yoy_chg = round(today_price - yoy_price) if today_price and yoy_price else None
    yoy_pct = round(yoy_chg / yoy_price * 100, 2) if yoy_chg and yoy_price else None

    print(f"  [돈가] {yesterday.strftime('%-m/%-d')}: {today_price}원, 전일대비: {chg}원")

    return {
        "today":      f"{today_price:,}원/㎏" if today_price else "N/A",
        "today_date": yesterday.strftime("%-m/%-d"),
        "chg":        f"{'+'if chg>=0 else ''}{chg:,}원" if chg is not None else "N/A",
        "chg_pct":    f"{'+'if chg_pct>=0 else ''}{chg_pct}%" if chg_pct is not None else "N/A",
        "chg_up":     chg >= 0 if chg is not None else True,
        "yoy":        f"{yoy_price:,}원/㎏" if yoy_price else "N/A",
        "yoy_date":   year_ago.strftime("%-m/%-d/%y"),
        "yoy_chg":    f"{'+'if yoy_chg>=0 else ''}{yoy_chg:,}원" if yoy_chg is not None else "N/A",
        "yoy_pct":    f"{'+'if yoy_pct>=0 else ''}{yoy_pct}%" if yoy_pct is not None else "N/A",
        "yoy_up":     yoy_chg >= 0 if yoy_chg is not None else True,
    }


def collect_market_data():
    """전체 거시경제 + 양돈 지표 수집"""
    print("  [브리핑] 거시경제 지표 수집 중...")

    specs = [
        ("코스피",     "^KS11",    lambda v: f"{v:,.2f}",        lambda c: f"{c:,.2f}p"),
        ("코스닥",     "^KQ11",    lambda v: f"{v:,.2f}",        lambda c: f"{c:,.2f}p"),
        ("나스닥 100", "^NDX",     lambda v: f"{v:,.2f}",        lambda c: f"{c:,.2f}p"),
        ("S&P 500",   "^GSPC",    lambda v: f"{v:,.2f}",        lambda c: f"{c:,.2f}p"),
        ("USD / KRW", "USDKRW=X", lambda v: f"{v:,.1f}원",      lambda c: f"{c:,.1f}원"),
        ("EUR / KRW", "EURKRW=X", lambda v: f"{v:,.1f}원",      lambda c: f"{c:,.1f}원"),
        ("옥수수 선물","ZC=F",     lambda v: f"${v/100:.2f}/bu", lambda c: f"${c/100:.3f}"),
        ("대두박 선물","ZM=F",     lambda v: f"${v:,.1f}/t",     lambda c: f"${c:.1f}"),
    ]
    keys = ["kospi","kosdaq","nasdaq","sp500","usd_krw","eur_krw","corn","soymeal"]
    market = {keys[i]: _get_ticker(*s) for i, s in enumerate(specs)}

    # 돈가 — data.go.kr 축산물품질평가원 API
    dong = _fetch_dongga()
    market["dongga"] = dong

    print("  [브리핑] 지표 수집 완료")
    return market


# ──────────────────────────────────────────────────
# 2. 전날 기사 DB에서 가져오기
# ──────────────────────────────────────────────────

def get_yesterday_articles(engine):
    """전날 발행된 기사 목록 반환"""
    yesterday_start = datetime.now(KST).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=1)
    yesterday_end = yesterday_start + timedelta(days=1)

    sql = text("""
        SELECT id, title, category, slug
        FROM generated_articles
        WHERE publish_status = 'published'
          AND published_at >= :start
          AND published_at <  :end
          AND title NOT LIKE '🐷 한돈투데이 모닝 브리핑%'
        ORDER BY published_at DESC
        LIMIT 30
    """)

    with engine.connect() as conn:
        rows = conn.execute(sql, {
            "start": yesterday_start.astimezone(timezone.utc),
            "end":   yesterday_end.astimezone(timezone.utc),
        }).fetchall()

    articles = []
    for row in rows:
        article_id, title, category, slug = row
        slug = slug or f"article-{article_id}"
        articles.append({
            "id":       article_id,
            "title":    title,
            "category": category,
            "url":      f"/article/{article_id}-{slug}/",
        })

    print(f"  [브리핑] 전날 기사 {len(articles)}건 로드")
    return articles


# ──────────────────────────────────────────────────
# 3. AI로 브리핑 콘텐츠 생성
# ──────────────────────────────────────────────────

SYSTEM_PROMPT = """당신은 양돈 전문 미디어 '한돈투데이(Handon Today)'의 시황 담당 기자입니다.
전날 발행된 기사 목록을 받아 브리핑 콘텐츠를 JSON으로 생성합니다.

[출력 형식 — JSON만 출력, 다른 텍스트 절대 금지]
{
  "lead1": "한 줄 핵심 요약 (20자 이내, 마침표 포함)",
  "lead2": "두 번째 핵심 요약 (20자 이내, 마침표 포함)",
  "news": [
    {"id": 기사ID(정수), "cat": "국내·핵심", "title": "20자내", "desc": "40자내"},
    {"id": 기사ID(정수), "cat": "글로벌·ASF", "title": "20자내", "desc": "40자내"},
    {"id": 기사ID(정수), "cat": "글로벌·수출", "title": "20자내", "desc": "40자내"},
    {"id": 기사ID(정수), "cat": "국내·시장", "title": "20자내", "desc": "40자내"},
    {"id": 기사ID(정수), "cat": "글로벌·무역", "title": "20자내", "desc": "40자내"}
  ],
  "points": [
    "① 포인트 내용 (30자 이내)",
    "② 포인트 내용 (30자 이내)",
    "③ 포인트 내용 (30자 이내)"
  ],
  "summary": "한 줄 요약 (40자 이내)"
}

[규칙]
- lead1, lead2: 각 20자 이내, 마침표로 끝, 서로 다른 주제
- news: 국내 2~3건 + 글로벌 2~3건 균형, 총 5건
- cat 형식 예시: 국내·핵심 / 국내·시장 / 국내·정책 / 글로벌·ASF / 글로벌·수출 / 글로벌·무역
- points: 오늘 농가가 실제로 챙겨야 할 것, 원문 기반으로만
- JSON 외 절대 출력 금지"""


def generate_content(articles):
    """AI로 브리핑 콘텐츠 생성"""
    if not articles:
        return None, 0

    yesterday_str = (datetime.now(KST) - timedelta(days=1)).strftime("%Y년 %m월 %d일")
    today_str = datetime.now(KST).strftime("%Y년 %m월 %d일")

    article_list = "\n".join([
        f"[ID:{a['id']}] [{a['category']}] {a['title']}"
        for a in articles
    ])

    payload = {
        "model": MODEL,
        "max_tokens": 800,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content":
                f"다음은 {yesterday_str} 발행된 기사 목록입니다.\n"
                f"{today_str} 아침 브리핑 JSON을 생성해주세요.\n\n"
                f"{article_list}\n\nJSON만 출력하세요."},
        ],
    }

    req = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://handontoday.com",
            "X-Title": "Handon Today",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=90) as r:
        result = json.loads(r.read().decode())
        text_raw = result["choices"][0]["message"]["content"].strip()
        text_raw = re.sub(r'^```(?:json)?\s*', '', text_raw)
        text_raw = re.sub(r'\s*```$', '', text_raw)
        content = json.loads(text_raw)
        usage = result.get("usage", {})
        cost = (usage.get("prompt_tokens", 0) / 1e6 * 0.10 +
                usage.get("completion_tokens", 0) / 1e6 * 0.40)
        print(f"  [브리핑 AI] 토큰: {usage.get('prompt_tokens')}/{usage.get('completion_tokens')}, 비용: ${cost:.4f}")
        return content, cost


# ──────────────────────────────────────────────────
# 4. HTML 생성
# ──────────────────────────────────────────────────

def _card(item):
    """지표 카드 HTML"""
    arrow = "▲" if item["up"] else "▼"
    cls   = "up" if item["up"] else "down"
    chg   = item["change"].lstrip("+-")
    return (
        f'<div class="mc">'
        f'<div><div class="mc-name">{item["name"]}</div>'
        f'<div class="mc-val">{item["value"]}</div></div>'
        f'<div class="mc-right">'
        f'<div class="mc-chg {cls}">{arrow} {chg}</div>'
        f'<div class="mc-pct {cls}">{item["pct"]}</div>'
        f'</div></div>'
    )


def _tl_item(n, articles_map):
    """타임라인 뉴스 아이템 HTML"""
    cat = n.get("cat", "")
    cls = "k" if "국내" in cat else ("a" if "ASF" in cat else "g")
    aid = n.get("id")
    url = articles_map.get(aid, {}).get("url", "#")
    return (
        f'<a href="https://handontoday.com{url}" '
        f'style="text-decoration:none;display:block;color:inherit">'
        f'<div class="ti {cls}">'
        f'<div class="tc {cls}">{cat}</div>'
        f'<div class="tt">{n.get("title","")}</div>'
        f'<div class="td">{n.get("desc","")}</div>'
        f'</div></a>'
    )


def build_html(market, content, articles):
    """확정 UI HTML 생성"""
    dong = market["dongga"]
    today_str = datetime.now(KST).strftime("%Y년 %m월 %d일")
    keys = ["kospi","kosdaq","nasdaq","sp500","usd_krw","eur_krw","corn","soymeal"]

    articles_map = {a["id"]: a for a in articles}
    cards_html    = "".join(_card(market[k]) for k in keys)
    timeline_html = "".join(_tl_item(n, articles_map) for n in content.get("news", [])[:5])
    points_html   = "".join(f'<div class="fp">{p}</div>' for p in content.get("points", []))

    dong_up  = dong["chg_up"]
    yoy_up   = dong["yoy_up"]
    d_arrow  = "▲" if dong_up  else "▼"
    y_arrow  = "▲" if yoy_up   else "▼"
    d_cls    = "up" if dong_up  else "down"
    y_cls    = "up" if yoy_up   else "down"

    return f"""<meta charset="UTF-8">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
.wrap{{padding:.75rem;max-width:390px;margin:0 auto;font-family:var(--font-sans,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif)}}
.hdr{{background:#1a1a1f;border-radius:12px;padding:.85rem 1rem;margin-bottom:.5rem}}
.hdr-eye{{font-size:10px;color:#888;font-family:monospace;letter-spacing:.08em;margin-bottom:4px}}
.hdr-title{{font-size:16px;font-weight:500;color:#fff;margin-bottom:8px;line-height:1}}
.hdr-lead{{display:flex;flex-direction:column;gap:4px}}
.hdr-line{{font-size:12px;color:#bbb;line-height:1.4;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.hdr-num{{color:#c0392b;margin-right:4px;font-weight:500}}
.grid2{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:3px;margin-bottom:3px}}
.mc{{background:var(--color-background-secondary,#f5f5f5);border-radius:6px;padding:7px 10px;display:flex;justify-content:space-between;align-items:center}}
.mc-name{{font-size:13px;font-weight:400;color:var(--color-text-secondary,#888);line-height:1;margin-bottom:3px}}
.mc-val{{font-size:13px;font-weight:500;color:var(--color-text-primary,#1a1a1a);line-height:1}}
.mc-right{{text-align:right;flex-shrink:0;margin-left:6px}}
.mc-chg{{font-size:11px;font-family:monospace;white-space:nowrap;line-height:1;margin-bottom:2px}}
.mc-pct{{font-size:10px;font-family:monospace;white-space:nowrap;line-height:1}}
.up{{color:#A32D2D}}.down{{color:#185FA5}}
.dong-card{{background:var(--color-background-secondary,#f5f5f5);border-radius:6px;padding:8px 10px;margin-bottom:3px}}
.dong-inner{{display:grid;grid-template-columns:1fr 1px 1fr;align-items:start}}
.dong-sep{{background:var(--color-border-tertiary,#ddd)}}
.dong-col{{padding:0 8px;display:flex;justify-content:space-between;align-items:center}}
.dong-col:first-child{{padding-left:0}}
.dong-label{{font-size:10px;color:var(--color-text-secondary,#888);font-family:monospace;margin-bottom:4px}}
.dong-val{{font-size:14px;font-weight:500;line-height:1;margin-bottom:2px}}
.dong-chg{{font-size:11px;font-family:monospace;line-height:1;margin-bottom:2px;white-space:nowrap}}
.dong-pct{{font-size:10px;font-family:monospace;line-height:1;white-space:nowrap}}
.note{{font-size:10px;color:var(--color-text-secondary,#999);font-style:italic;margin:.4rem 0 .75rem}}
.divider{{height:.5px;background:var(--color-border-tertiary,#e0e0e0);margin:.6rem 0}}
.sec-head{{display:flex;align-items:center;gap:6px;margin-bottom:8px;padding-bottom:5px;border-bottom:.5px solid var(--color-border-tertiary,#e0e0e0)}}
.sec-title{{font-size:11px;font-weight:500;color:var(--color-text-secondary,#888);letter-spacing:.04em}}
.tl{{position:relative;padding-left:1rem;margin-bottom:.75rem}}
.tl::before{{content:"";position:absolute;left:4px;top:0;bottom:0;width:1px;background:var(--color-border-secondary,#ddd)}}
.ti{{position:relative;padding-bottom:.75rem}}
.ti::before{{content:"";position:absolute;left:-12px;top:4px;width:7px;height:7px;border-radius:50%}}
.ti.k::before{{background:#A32D2D}}.ti.g::before{{background:#185FA5}}.ti.a::before{{background:#BA7517}}
.tc{{font-size:10px;font-weight:500;letter-spacing:.05em;margin-bottom:2px}}
.tc.k{{color:#A32D2D}}.tc.g{{color:#185FA5}}.tc.a{{color:#BA7517}}
.tt{{font-size:13px;font-weight:500;color:var(--color-text-primary,#1a1a1a);line-height:1.3;margin-bottom:2px}}
.td{{font-size:11px;color:var(--color-text-secondary,#666);line-height:1.45}}
.fp{{font-size:12px;color:var(--color-text-primary,#1a1a1a);line-height:1.55;padding:5px 0;border-bottom:.5px solid var(--color-border-tertiary,#e0e0e0)}}
.fp:last-child{{border-bottom:none}}
.sbox{{border:.5px solid var(--color-border-secondary,#ddd);border-radius:6px;padding:.65rem .85rem;margin-top:.6rem}}
.slabel{{font-size:10px;color:#A32D2D;font-weight:500;letter-spacing:.05em;margin-bottom:3px}}
.stext{{font-size:12px;font-weight:500;color:var(--color-text-primary,#1a1a1a);line-height:1.5}}
.byline{{font-size:10px;color:var(--color-text-secondary,#999);font-style:italic;text-align:right;margin-top:.5rem}}
</style>
<div class="wrap">
  <div class="hdr">
    <div class="hdr-eye">HANDON TODAY · {today_str} · 06:00 KST</div>
    <div class="hdr-title">한돈투데이 모닝 브리핑</div>
    <div class="hdr-lead">
      <div class="hdr-line"><span class="hdr-num">1.</span>{content.get("lead1","")}</div>
      <div class="hdr-line"><span class="hdr-num">2.</span>{content.get("lead2","")}</div>
    </div>
  </div>
  <div class="grid2">{cards_html}</div>
  <div class="dong-card">
    <div class="dong-inner">
      <div class="dong-col">
        <div>
          <div class="dong-label">전날 돈가 ({dong["today_date"]})</div>
          <div class="dong-val" style="color:var(--color-text-primary,#1a1a1a)">{dong["today"]}</div>
        </div>
        <div style="text-align:right;flex-shrink:0;margin-left:6px">
          <div class="dong-chg {d_cls}">{d_arrow} {dong["chg"]}</div>
          <div class="dong-pct {d_cls}">{dong["chg_pct"]}</div>
        </div>
      </div>
      <div class="dong-sep"></div>
      <div class="dong-col">
        <div>
          <div class="dong-label">작년 동일 ({dong["yoy_date"]})</div>
          <div class="dong-val" style="color:#185FA5">{dong["yoy"]}</div>
        </div>
        <div style="text-align:right;flex-shrink:0;margin-left:6px">
          <div class="dong-chg {y_cls}">{y_arrow} {dong["yoy_chg"]}</div>
          <div class="dong-pct {y_cls}">{dong["yoy_pct"]}</div>
        </div>
      </div>
    </div>
  </div>
  <div class="note">* 선물: CME USD · 전전날→전날 기준 | 돈가: 추후 연동 예정</div>
  <div class="divider"></div>
  <div class="sec-head"><span style="font-size:14px">📰</span><span class="sec-title">어제의 주요 뉴스</span></div>
  <div class="tl">{timeline_html}</div>
  <div class="sec-head"><span style="font-size:14px">☀️</span><span class="sec-title">오늘 농가 주목 포인트</span></div>
  <div style="margin-bottom:.75rem">{points_html}</div>
  <div class="sbox">
    <div class="slabel">한 줄 요약</div>
    <div class="stext">{content.get("summary","")}</div>
  </div>
  <div class="byline">한돈투데이 (Handon Today) | 팜스링크 기자 작성</div>
</div>"""


# ──────────────────────────────────────────────────
# 5. DB 저장
# ──────────────────────────────────────────────────

def save_to_db(engine, body_html, cost_usd, pipeline_run_id=None):
    """브리핑 기사를 generated_articles에 저장"""
    now_kst = datetime.now(KST)
    title = f"🐷 한돈투데이 모닝 브리핑 — {now_kst.strftime('%-m월 %-d일')}"
    slug  = f"morning-briefing-{now_kst.strftime('%Y-%m-%d')}"

    sql = text("""
        INSERT INTO generated_articles
          (title, deck, body, body_html, body_markdown, category, match_reason,
           validation_passed, cost_usd, publish_status,
           published_at, pipeline_run_id, generated_at, slug)
        VALUES
          (:title, :deck, :body, :body_html, :body_markdown, '국내', '일일 시황 브리핑 자동 생성',
           true, :cost_usd, 'published',
           :now, :run_id, :now, :slug)
        RETURNING id
    """)

    with engine.begin() as conn:
        row = conn.execute(sql, {
            "title":     title,
            "deck":      "전일 주요 시장 지표와 뉴스 요약을 전해드립니다.",
            "body":      "일일 시황 브리핑",
            "body_html": body_html,
            "body_markdown": "일일 시황 브리핑",
            "cost_usd":  cost_usd,
            "now":       now_kst.astimezone(timezone.utc),
            "run_id":    pipeline_run_id,
            "slug":      slug,
        }).fetchone()

    print(f"  [브리핑] DB 저장 완료 — id={row[0]}, title={title}")
    return row[0]


# ──────────────────────────────────────────────────
# 6. 메인 진입점
# ──────────────────────────────────────────────────

def run_daily_briefing(engine, pipeline_run_id=None):
    """
    일일 시황 브리핑 전체 파이프라인.
    main.py의 오전 6시 트리거에서 호출.

    Returns:
        {"success": bool, "article_id": int, "cost_usd": float}
    """
    now_kst = datetime.now(KST)
    print(f"\n{'='*50}")
    print(f"  🐷 일일 시황 브리핑 시작")
    print(f"  {now_kst.strftime('%Y-%m-%d %H:%M KST')}")
    print(f"{'='*50}")

    total_cost = 0.0

    try:
        # 1. 거시경제 지표 수집
        market = collect_market_data()

        # 2. 전날 기사 가져오기
        articles = get_yesterday_articles(engine)
        if not articles:
            print("  [브리핑] 전날 기사 없음 — 생성 중단")
            return {"success": False, "article_id": None, "cost_usd": 0}

        # 3. AI 콘텐츠 생성
        content, cost = generate_content(articles)
        total_cost += cost
        if not content:
            return {"success": False, "article_id": None, "cost_usd": total_cost}

        # 4. HTML 생성
        body_html = build_html(market, content, articles)

        # 5. DB 저장
        article_id = save_to_db(engine, body_html, total_cost, pipeline_run_id)

        print(f"\n  ✅ 브리핑 완료 — 비용: ${total_cost:.4f} (≈{total_cost*1400:.1f}원)")
        return {"success": True, "article_id": article_id, "cost_usd": total_cost}

    except Exception as e:
        import traceback
        print(f"  ❌ 브리핑 실패: {e}")
        print(traceback.format_exc()[:500])
        return {"success": False, "article_id": None, "cost_usd": total_cost}
