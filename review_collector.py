"""
================================================================
  한돈투데이 N년 전 오늘 회고 파이프라인
  review_collector.py
  v1.0.3
================================================================
[변경사항 v1.0.3]
  - Slack 알림 URL에 article id 포함 (Django URL 패턴 일치)
  - 인사이트 라벨 "N년 전과 오늘" 올바르게 표시 (target_year → years_ago)
  - _build_title 불필요 변수 제거
  - 윤년 2월 29일 방어 코드 추가
  - HTML 엔티티 처리 html.unescape()로 통합
  - Gemini 응답 korea_events/us_events 타입 검증
  - db_manager.close_engine() 호출 추가
  - notifier 함수명 send_simple_message로 수정
  - DB 컬럼명 publish_status로 수정

[역할]
  기준일로부터 정확히 N년 전 주간 양돈타임스 기사를 크롤링하여
  "N년 전 오늘" 회고 기사를 자동 생성합니다.

  - 2016review: 매주 수요일 09:00 KST (10년 전)
  - 2006review: 매주 토요일 09:00 KST (20년 전)

[핵심 로직]
  1. 기준일(오늘) - N년 ±4일 범위로 양돈타임스 크롤링
  2. article:published_time 기준 목표 연도 기사만 필터링 (가짜 기사 제거)
  3. 비양돈 콘텐츠 필터링 (칼럼/한시/신화/의학상식 등 제외)
  4. Gemini로 기사 본문 요약 + 시대 배경 + 인사이트 생성
  5. GCS 저장 + DB INSERT (category='회고')
  6. Slack 알림

[환경변수]
  OPENROUTER_API_KEY  - OpenRouter API 키
  SLACK_WEBHOOK_URL   - Slack Webhook URL
  CLOUD_SQL_PASSWORD  - DB 비밀번호
  DB_HOST             - Cloud SQL 소켓 경로
  DB_NAME             - DB명
  DB_USER             - DB 유저
  GCS_BUCKET          - GCS 버킷명

[진입점]
  run_2016review_pipeline(request) — 10년 전 (수요일 09:00 KST)
  run_2006review_pipeline(request) — 20년 전 (토요일 09:00 KST)
"""

import os
import re
import json
import html as html_mod
import requests
import functions_framework
from datetime import datetime, timezone, timedelta
from google.cloud import storage
from sqlalchemy import text

import db_manager
import notifier

# ──────────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────────
KST            = timezone(timedelta(hours=9))
GCS_BUCKET     = os.getenv("GCS_BUCKET", "handontoday-articles")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
TEXT_MODEL     = "google/gemini-2.5-flash-lite"

PIGTIMES_BASE  = "http://www.pigtimes.co.kr"
DAYS_RANGE     = 4   # 기준일 ±4일

# 비양돈 콘텐츠 제목 필터 (이 키워드로 시작하는 기사 제외)
NON_PIG_PREFIXES = [
    "[칼럼]", "[화요칼럼]", "[월요칼럼]", "[수요칼럼]", "[목요칼럼]", "[금요칼럼]",
    "[한시", "[신화]", "[의학상식]",
    "[짧은소식]", "[기자수첩]", "[독자투고]", "[기고]",
    "[퀴즈]", "[현장25시]",
]

# 비양돈 콘텐츠 본문/제목 포함 키워드 (어디든 포함되면 제외)
NON_PIG_KEYWORDS = [
    "그리스", "로마신화", "아폴론", "아르테미스", "제우스", "헤라클레스", "튀폰",
]

# 히어로 이미지 GCS URL (고정)
HERO_IMAGE = {
    10: f"https://storage.googleapis.com/{GCS_BUCKET}/review/hero_10y.png",
    20: f"https://storage.googleapis.com/{GCS_BUCKET}/review/hero_20y.png",
}


# ──────────────────────────────────────────────────
# 양돈타임스 크롤링
# ──────────────────────────────────────────────────

def _fetch_article_list(sdate: str, edate: str) -> list[int]:
    """날짜 범위로 기사 ID 목록 수집"""
    url = (
        f"{PIGTIMES_BASE}/news/articleList.html"
        f"?sc_sdate={sdate}&sc_edate={edate}&sc_order_by=E&view_type=sm"
    )
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        ids = list(set(re.findall(r'idxno=(\d+)', resp.text)))
        return [int(i) for i in ids]
    except Exception as e:
        print(f"  [크롤링 에러] 목록 수집 실패: {e}")
        return []


def _fetch_article(idxno: int) -> dict | None:
    """개별 기사 fetch — 제목/날짜/본문 반환"""
    url = f"{PIGTIMES_BASE}/news/articleView.html?idxno={idxno}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp_text = resp.text

        # 제목
        title_m = re.search(r'<title>(.*?)</title>', resp_text)
        raw_title = title_m.group(1) if title_m else ""
        # "- 양돈타임스" 제거
        title = re.sub(r'\s*-\s*양돈타임스\s*$', '', raw_title).strip()
        # HTML 엔티티 일괄 처리
        title = html_mod.unescape(title)

        # 발행일 (article:published_time)
        date_m = re.search(
            r'<meta[^>]+article:published_time[^>]+content="([\d]{4}-[\d]{2}-[\d]{2})',
            resp_text
        )
        pub_date = date_m.group(1) if date_m else ""

        # 본문
        body_m = re.search(
            r'id="article-view-content-div"[^>]*>(.*?)</div>',
            resp_text, re.DOTALL
        )
        body_raw = body_m.group(1) if body_m else ""
        body = re.sub(r'<[^>]+>', '', body_raw)
        body = html_mod.unescape(body)
        body = re.sub(r'\s+', ' ', body).strip()[:600]

        return {
            "idxno": idxno,
            "title": title,
            "pub_date": pub_date,
            "body": body,
            "url": url,
        }
    except Exception as e:
        print(f"  [크롤링 에러] idxno={idxno}: {e}")
        return None


def _is_pig_article(title: str) -> bool:
    """비양돈 콘텐츠 필터"""
    for prefix in NON_PIG_PREFIXES:
        # "[현장25시/이름]" 형태도 잡기 위해 startswith(prefix.rstrip("]")) 병행
        if title.startswith(prefix) or title.startswith(prefix.rstrip("]")):
            return False
    for kw in NON_PIG_KEYWORDS:
        if kw in title:
            return False
    return True


def crawl_review_articles(target_year: int, base_date: datetime) -> list[dict]:
    """
    기준일(base_date) 기준 target_year의 ±4일 범위 양돈 기사 수집.
    article:published_time 기준으로 목표 연도 기사만 필터링.
    """
    # target_year의 같은 월/일 ±4일 (윤년 방어)
    try:
        target_base = base_date.replace(year=target_year)
    except ValueError:
        # 2월 29일인데 target_year가 평년인 경우
        target_base = base_date.replace(year=target_year, month=2, day=28)

    sdate = (target_base - timedelta(days=DAYS_RANGE)).strftime("%Y-%m-%d")
    edate = (target_base + timedelta(days=DAYS_RANGE)).strftime("%Y-%m-%d")

    print(f"  [크롤링] {target_year}년 {sdate} ~ {edate}")

    ids = _fetch_article_list(sdate, edate)
    print(f"  [크롤링] ID {len(ids)}개 발견")

    articles = []
    for idxno in ids:
        art = _fetch_article(idxno)
        if not art:
            continue

        # ① 발행일 연도 검증 (핵심: 가짜 기사 제거)
        if not art["pub_date"].startswith(str(target_year)):
            continue

        # ② 비양돈 필터
        if not _is_pig_article(art["title"]):
            continue

        # ③ 본문 없는 기사 제외
        if len(art["body"]) < 30:
            continue

        articles.append(art)
        print(f"  ✅ [{art['pub_date']}] {art['title'][:50]}")

    print(f"  [크롤링] 최종 {len(articles)}건 확보")
    return articles


# ──────────────────────────────────────────────────
# Gemini 기사 생성
# ──────────────────────────────────────────────────

def _build_prompt(articles: list[dict], target_year: int, base_date: datetime) -> str:
    month = base_date.month
    day   = base_date.day

    articles_text = ""
    for i, a in enumerate(articles[:6], 1):
        articles_text += f"\n[기사 {i}] {a['title']}\n{a['body'][:300]}\n"

    return f"""너는 한돈투데이의 회고 기사 작성 에디터야.
아래는 {target_year}년 {month}월 {day}일 전후 양돈타임스에 실린 실제 기사들이야.
이 기사들을 바탕으로 '{target_year}년 그 주 양돈업계'를 돌아보는 블로그형 기사를 작성해줘.

[입력 기사]
{articles_text}

[작성 규칙]
1. deck(부제): 그 주 핵심을 한 문장으로. 독자가 바로 읽고 싶어지게.
2. para1~3: 블로그 문단 3개. 각 150자 내외. 실제 기사 내용 기반.
   - 과거 시제 사용 ("~했습니다", "~던 주였어요")
   - 숫자/사실은 기사에 나온 것만 사용 (추측 금지)
   - 독자에게 말 걸듯 친근하게
3. insight_repeat: 지금도 반복되는 문제 1~2줄
4. insight_change: 지금은 달라진 것 1~2줄
5. korea_events: 그 주 대한민국 시사 이슈 3건 (날짜 포함, Gemini 학습 데이터 기반)
   형식: "YYYY년 MM월 DD일 — 내용"
6. us_events: 그 주 미국 시사 이슈 3건 (날짜 포함)
   형식: "YYYY년 MM월 DD일 — 내용"

[출력 형식 - JSON만, 다른 텍스트 없이]
{{
  "deck": "...",
  "para1": "...",
  "para2": "...",
  "para3": "...",
  "insight_repeat": "...",
  "insight_change": "...",
  "korea_events": ["...", "...", "..."],
  "us_events": ["...", "...", "..."]
}}"""


def generate_review_article(
    articles: list[dict],
    target_year: int,
    base_date: datetime,
) -> dict | None:
    """Gemini로 회고 기사 생성"""
    prompt = _build_prompt(articles, target_year, base_date)

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": TEXT_MODEL,
        "max_tokens": 2000,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    }

    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=60,
        )
        raw = resp.json()["choices"][0]["message"]["content"]
        # JSON 블록 제거
        raw = re.sub(r'^```json\s*', '', raw.strip())
        raw = re.sub(r'\s*```$', '', raw.strip())
        gen = json.loads(raw)

        # 응답 타입 검증 — 리스트가 아니면 빈 리스트로 보정
        if not isinstance(gen.get("korea_events"), list):
            gen["korea_events"] = []
        if not isinstance(gen.get("us_events"), list):
            gen["us_events"] = []

        return gen
    except Exception as e:
        print(f"  [Gemini 에러] {e}")
        return None


# ──────────────────────────────────────────────────
# 기사 조립
# ──────────────────────────────────────────────────

def _build_title(target_year: int, base_date: datetime, deck: str) -> str:
    years_ago = base_date.year - target_year
    return f"{years_ago}년 전 오늘 — {deck}"


def _build_body_html(gen: dict, target_year: int, base_date: datetime) -> str:
    """HTML 본문 조립 (DB body 필드에 저장)"""
    years_ago = base_date.year - target_year

    korea_items = "".join(
        f'<div class="week-item">{e}</div>' for e in gen.get("korea_events", [])
    )
    us_items = "".join(
        f'<div class="week-item">{e}</div>' for e in gen.get("us_events", [])
    )

    return f"""<div class="week-box">
<div class="week-label">📰 그 주 대한민국</div>
{korea_items}
</div>
<div class="week-box us">
<div class="week-label">🇺🇸 그 주 미국</div>
{us_items}
</div>
<div class="blog-para"><div class="blog-num">1</div><div class="blog-text">{gen.get("para1", "")}</div></div>
<div class="blog-para"><div class="blog-num">2</div><div class="blog-text">{gen.get("para2", "")}</div></div>
<div class="blog-para"><div class="blog-num">3</div><div class="blog-text">{gen.get("para3", "")}
<div class="insight">
<div class="insight-label">🔁 {years_ago}년 전과 오늘 — 반복된 것, 달라진 것</div>
<div class="insight-row"><span class="tag tag-r">반복</span>{gen.get("insight_repeat", "")}</div>
<div class="insight-row"><span class="tag tag-c">달라진 것</span>{gen.get("insight_change", "")}</div>
</div>
</div></div>"""


# ──────────────────────────────────────────────────
# GCS 저장
# ──────────────────────────────────────────────────

def _upload_to_gcs(content: str, blob_name: str, content_type: str = "application/json"):
    client = storage.Client()
    bucket = client.bucket(GCS_BUCKET)
    blob = bucket.blob(blob_name)
    blob.upload_from_string(content, content_type=content_type)
    return f"gs://{GCS_BUCKET}/{blob_name}"


# ──────────────────────────────────────────────────
# DB INSERT
# ──────────────────────────────────────────────────

def _insert_review_article(
    title: str,
    deck: str,
    body_html: str,
    slug: str,
    image_url: str,
    published_at: datetime,
) -> int | None:
    engine = db_manager.get_engine()
    try:
        with engine.begin() as conn:
            row = conn.execute(text("""
                INSERT INTO generated_articles
                    (title, deck, body, body_markdown, body_html, category, slug, image_url,
                     published_at, publish_status, created_at)
                VALUES
                    (:title, :deck, :body, :body, :body, '회고', :slug, :image_url,
                     :published_at, 'published', NOW())
                RETURNING id
            """), {
                "title": title,
                "deck": deck,
                "body": body_html,
                "slug": slug,
                "image_url": image_url,
                "published_at": published_at,
            }).fetchone()
            return row[0] if row else None
    except Exception as e:
        print(f"  [DB 에러] {e}")
        return None


def _already_published(slug: str) -> bool:
    """중복 실행 방지"""
    engine = db_manager.get_engine()
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT id FROM generated_articles WHERE slug = :slug LIMIT 1"),
                {"slug": slug}
            ).fetchone()
            return row is not None
    except Exception:
        return False


# ──────────────────────────────────────────────────
# 공통 파이프라인 실행 함수
# ──────────────────────────────────────────────────

def _run_review_pipeline(years_ago: int) -> dict:
    base_date   = datetime.now(KST)
    target_year = base_date.year - years_ago
    slug_prefix = f"{target_year}review"
    slug        = f"{slug_prefix}-{base_date.strftime('%Y-%m-%d')}"

    print(f"\n{'='*60}")
    print(f"  🕐 한돈투데이 {years_ago}년 전 오늘 ({target_year}review)")
    print(f"  기준일: {base_date.strftime('%Y-%m-%d')} → 대상: {target_year}년")
    print(f"  slug: {slug}")
    print(f"{'='*60}")

    result = {
        "slug": slug,
        "target_year": target_year,
        "years_ago": years_ago,
        "success": False,
        "error": None,
    }

    # ① 중복 체크
    if _already_published(slug):
        print(f"  [스킵] 이미 발행된 slug: {slug}")
        result["success"] = True
        result["skipped"] = True
        return result

    # ② 크롤링
    articles = crawl_review_articles(target_year, base_date)
    if not articles:
        result["error"] = f"{target_year}년 양돈 기사 없음 (±{DAYS_RANGE}일 범위)"
        print(f"  ❌ {result['error']}")
        notifier.send_simple_message(f"❌ {years_ago}년 전 오늘 실패: {result['error']}")
        return result

    # ③ Gemini 기사 생성
    gen = generate_review_article(articles, target_year, base_date)
    if not gen:
        result["error"] = "Gemini 기사 생성 실패"
        print(f"  ❌ {result['error']}")
        notifier.send_simple_message(f"❌ {years_ago}년 전 오늘 실패: {result['error']}")
        return result

    # ④ 제목/본문 조립
    title     = _build_title(target_year, base_date, gen.get("deck", ""))
    body_html = _build_body_html(gen, target_year, base_date)
    image_url = HERO_IMAGE[years_ago]

    # ⑤ GCS 저장 (JSON 백업)
    try:
        gcs_path = f"review/{slug}.json"
        _upload_to_gcs(
            json.dumps({"title": title, "gen": gen, "articles": articles},
                       ensure_ascii=False, indent=2),
            gcs_path
        )
        print(f"  ✅ GCS 저장: gs://{GCS_BUCKET}/{gcs_path}")
    except Exception as e:
        print(f"  ⚠️ GCS 저장 실패 (계속 진행): {e}")

    # ⑥ DB INSERT
    published_at = base_date.replace(hour=9, minute=0, second=0, microsecond=0)
    art_id = _insert_review_article(
        title=title,
        deck=gen.get("deck", ""),
        body_html=body_html,
        slug=slug,
        image_url=image_url,
        published_at=published_at,
    )

    if not art_id:
        result["error"] = "DB INSERT 실패"
        print(f"  ❌ {result['error']}")
        notifier.send_simple_message(f"❌ {years_ago}년 전 오늘 DB 저장 실패")
        return result

    # ⑦ Slack 알림
    msg = (
        f"✅ *{years_ago}년 전 오늘* 발행 완료\n"
        f"📰 {title}\n"
        f"🔗 https://handontoday.com/article/{art_id}-{slug}/\n"
        f"📦 기반 기사 {len(articles)}건 ({target_year}년)"
    )
    notifier.send_simple_message(msg)

    result["success"]  = True
    result["title"]    = title
    result["art_id"]   = art_id
    result["articles_count"] = len(articles)
    print(f"\n  🎉 완료 — id={art_id}, 제목: {title[:40]}")

    # ⑧ DB 커넥션 정리
    try:
        db_manager.close_engine()
    except Exception:
        pass

    return result


# ──────────────────────────────────────────────────
# Cloud Functions 진입점
# ──────────────────────────────────────────────────

@functions_framework.http
def run_2016review_pipeline(request):
    """10년 전 오늘 (수요일 09:00 KST)"""
    result = _run_review_pipeline(years_ago=10)
    return (json.dumps(result, ensure_ascii=False), 200, {"Content-Type": "application/json"})


@functions_framework.http
def run_2006review_pipeline(request):
    """20년 전 오늘 (토요일 09:00 KST)"""
    result = _run_review_pipeline(years_ago=20)
    return (json.dumps(result, ensure_ascii=False), 200, {"Content-Type": "application/json"})
