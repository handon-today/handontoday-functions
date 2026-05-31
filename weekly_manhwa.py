"""
================================================================
  한돈투데이 주간 만평 자동 생성 파이프라인
  weekly_manhwa.py
  v1.0.0
================================================================

[역할]
  매주 월요일 09:00 KST 자동 실행
  1. 지난 7일 기사 DB 조회 → 주제 추출
  2. Gemini 2.5 Flash Lite → 논평 문구 + 이미지 프롬프트 생성 (JSON)
  3. 나노바나나2 (gemini-3.1-flash-image-preview) → 이미지 생성
  4. GCS 저장
  5. DB INSERT (category='만평')
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
  run_manhwa_pipeline(request) — Cloud Functions HTTP 트리거
"""

import os
import json
import base64
import requests
import functions_framework

from datetime import datetime, timezone, timedelta
from google.cloud import storage

import db_manager
import notifier

# ──────────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────────

KST = timezone(timedelta(hours=9))
GCS_BUCKET = os.getenv("GCS_BUCKET", "handontoday-articles")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

TEXT_MODEL  = "google/gemini-2.5-flash-lite"
IMAGE_MODEL = "google/gemini-3.1-flash-image-preview"

SYSTEM_PROMPT = """너는 15년 경력의 한국 시사만평 전문 편집자이자 일러스트레이터야.
한국 신문 시사만평의 날카로운 비판 정신과
웹툰 특유의 감성적이고 몰입감 있는 연출을 모두 구사할 수 있어.

이번 주 한돈(양돈) 업계 핵심 주제가 주어지면,
이 주제에 대한 1컷 논평 이미지 프롬프트를 작성해줘.

[구도 선택 - 가장 임팩트 있는 것 하나 선택]
A. 인물 중심 — 감정이입, 공감, 분노 유발이 필요할 때
B. 풍경/환경 — 규모, 압도감, 상황의 무게감 표현
C. 오브젝트/상징 — 추상 개념, 정책 비판, 메타포
D. 대비 구도 — 두 대상의 극명한 차이 (예: 대기업 vs 농가)

[톤 선택 - 하나 선택]
- 분노/고발: 강한 색, 역동적 구도
- 슬픔/비극: 차분한 색, 정적인 구도
- 풍자/냉소: 과장된 연출, 아이러니한 장면
- 경고/긴장: 어두운 색, 불안한 구도

[스타일 고정 - 항상 적용]
Painterly editorial illustration.
한국 웹툰의 선명한 선과 감성 + 서양 시사만평의 날카로운 구도.
항상 드라마틱하고 영화적인 구성.
절대 photorealistic하게 그리지 말 것.

[절대 금지]
- 이미지 안에 텍스트, 글자, 숫자 일체
- 고어, 잔인한 장면 (동물 폐사는 빈 우리·그림자 등 암시로만)
- 특정 실존 인물 얼굴
- 과도하게 귀엽고 아기자기한 chibi 스타일

[출력 형식 - 반드시 JSON만 출력, 다른 텍스트 없이]
{
  "topic": "이번 주 핵심 주제 (한국어, 1문장)",
  "caption": "논평 문구 (한국어, 15자 이내, 날카롭고 임팩트 있게)",
  "composition_type": "A/B/C/D 중 하나",
  "mood": "선택한 톤",
  "reasoning": "이 구도와 톤을 선택한 이유 (한국어, 1문장)",
  "prompt": "이미지 생성 프롬프트 (영어, 150단어 이내)"
}"""


# ──────────────────────────────────────────────────
# 주제 추출
# ──────────────────────────────────────────────────

def fetch_recent_articles(engine, days=7):
    """지난 N일 발행 기사 제목 목록 조회"""
    from sqlalchemy import text

    cutoff = datetime.now(KST) - timedelta(days=days)
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT title, category, published_at
            FROM generated_articles
            WHERE publish_status = 'published'
              AND published_at >= :cutoff
              AND category IN ('국내', '글로벌', '시황')
            ORDER BY published_at DESC
            LIMIT 50
        """), {"cutoff": cutoff.astimezone(timezone.utc)}).fetchall()

    return [{"title": r[0], "category": r[1]} for r in rows]


def generate_prompt(articles):
    """Gemini로 주제 분석 + 이미지 프롬프트 생성"""
    titles_text = "\n".join(
        f"[{a['category']}] {a['title']}" for a in articles[:30]
    )
    user_message = f"""지난 주 한돈 업계 주요 기사 목록:
{titles_text}

위 기사들을 분석하여 가장 중요한 주제 하나를 선택하고,
그에 맞는 만평 이미지 프롬프트를 생성해줘."""

    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://handontoday.com",
            "X-Title": "Handon Today Manhwa",
        },
        json={
            "model": TEXT_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
        },
        timeout=60,
    )
    resp.raise_for_status()

    text = resp.json()["choices"][0]["message"]["content"]
    text = text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(text)


# ──────────────────────────────────────────────────
# 이미지 생성
# ──────────────────────────────────────────────────

def generate_image(image_prompt):
    """나노바나나2로 이미지 생성 → base64 반환"""
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://handontoday.com",
            "X-Title": "Handon Today Manhwa",
        },
        json={
            "model": IMAGE_MODEL,
            "messages": [{"role": "user", "content": image_prompt}],
            "modalities": ["image", "text"],
        },
        timeout=180,
    )
    resp.raise_for_status()

    msg = resp.json()["choices"][0]["message"]

    # Gemini 구조: message.content 리스트
    content = msg.get("content") or []
    if isinstance(content, list):
        for part in content:
            if part.get("type") == "image_url":
                url = part["image_url"]["url"]
                if url.startswith("data:image"):
                    return base64.b64decode(url.split(",", 1)[1])
                else:
                    img_resp = requests.get(url, timeout=60)
                    return img_resp.content

    # message.images 구조 (Seedream 등)
    for img in msg.get("images", []):
        url = img.get("image_url", {}).get("url", "")
        if url.startswith("data:image"):
            return base64.b64decode(url.split(",", 1)[1])
        elif url:
            img_resp = requests.get(url, timeout=60)
            return img_resp.content

    raise ValueError(f"이미지 URL 없음. message keys: {list(msg.keys())}")


# ──────────────────────────────────────────────────
# GCS 저장
# ──────────────────────────────────────────────────

def upload_to_gcs(image_bytes, week_str):
    """GCS에 만평 이미지 저장 → 공개 URL 반환"""
    client = storage.Client()
    bucket = client.bucket(GCS_BUCKET)
    blob_name = f"manhwa/{week_str}.jpg"
    blob = bucket.blob(blob_name)
    blob.upload_from_string(image_bytes, content_type="image/jpeg")
    return f"https://storage.googleapis.com/{GCS_BUCKET}/{blob_name}"


# ──────────────────────────────────────────────────
# DB 저장
# ──────────────────────────────────────────────────

def save_manhwa_to_db(engine, result, image_url, published_at, title, slug):
    """만평 기사를 generated_articles에 저장"""
    from sqlalchemy import text

    caption = result.get("caption", "")
    topic = result.get("topic", "")

    with engine.begin() as conn:
        row = conn.execute(text("""
            INSERT INTO generated_articles (
                title, deck, slug, image_url,
                body, body_markdown, body_html,
                category, tags,
                match_reason, source_titles, source_urls,
                validation_passed,
                publish_status, published_at,
                generated_at, created_at, updated_at
            ) VALUES (
                :title, :deck, :slug, :image_url,
                :body, :body_markdown, :body_html,
                '만평', '[]',
                :match_reason, '[]', '[]',
                true,
                'published', :published_at,
                NOW(), NOW(), NOW()
            )
            RETURNING id
        """), {
            "title": title,
            "deck": topic,
            "slug": slug,
            "image_url": image_url,
            "body": caption,
            "body_markdown": caption,
            "body_html": f"<p>{caption}</p>",
            "match_reason": result.get("reasoning", ""),
            "published_at": published_at.astimezone(timezone.utc),
        })
        article_id = row.scalar()

    return article_id


# ──────────────────────────────────────────────────
# 주차 계산
# ──────────────────────────────────────────────────

def get_week_info(dt):
    """
    날짜에서 '0월 0주차' 표현 및 GCS용 문자열 반환
    예: (2026년 6월 1주차, 2026-W23, 2026-06-w1)
    """
    year = dt.year
    month = dt.month
    # 해당 월의 첫 번째 월요일 기준 주차 계산
    first_day = dt.replace(day=1)
    # 이 달의 몇 번째 주인지 (1~5)
    week_of_month = (dt.day + first_day.weekday()) // 7 + 1
    iso_week = dt.isocalendar()[1]

    title = f"{year}년 {month}월 {week_of_month}주차 한돈 만평"
    slug = f"{year}-{month:02d}-w{week_of_month}"
    gcs_key = f"{year}-W{iso_week:02d}"

    return title, slug, gcs_key


# ──────────────────────────────────────────────────
# Cloud Functions 진입점
# ──────────────────────────────────────────────────

@functions_framework.http
def run_manhwa_pipeline(request):
    """주간 만평 파이프라인 — 매주 월요일 09:00 KST 실행"""
    now_kst = datetime.now(KST)
    print(f"\n{'='*50}")
    print(f"  🎨 한돈투데이 주간 만평 파이프라인 v1.0.0")
    print(f"  실행 시각: {now_kst.strftime('%Y-%m-%d %H:%M KST')}")
    print(f"{'='*50}")

    result = {
        "success": False,
        "error": "",
        "article_id": None,
        "image_url": "",
        "title": "",
        "cost_usd": 0.0,
    }

    try:
        engine = db_manager.get_engine()

        # 1. 지난 주 기사 조회
        print("\n[1/5] 지난 주 기사 조회...")
        articles = fetch_recent_articles(engine, days=7)
        if not articles:
            raise ValueError("지난 주 기사가 없습니다.")
        print(f"  ✅ {len(articles)}건 조회")

        # 2. 주제 추출 + 프롬프트 생성
        print("\n[2/5] 주제 분석 + 프롬프트 생성...")
        prompt_result = generate_prompt(articles)
        print(f"  구도: {prompt_result.get('composition_type')}")
        print(f"  톤:   {prompt_result.get('mood')}")
        print(f"  주제: {prompt_result.get('topic')}")
        print(f"  논평: {prompt_result.get('caption')}")

        # 3. 이미지 생성
        print("\n[3/5] 이미지 생성 (나노바나나2)...")
        image_bytes = generate_image(prompt_result["prompt"])
        print(f"  ✅ 이미지 생성 완료 ({len(image_bytes):,} bytes)")

        # 4. GCS 저장
        print("\n[4/5] GCS 저장...")
        title, slug, gcs_key = get_week_info(now_kst)
        image_url = upload_to_gcs(image_bytes, gcs_key)
        print(f"  ✅ {image_url}")

        # 5. DB 저장 (published_at = 이번 주 월요일 09:00 KST)
        print("\n[5/5] DB 저장...")
        published_at = now_kst.replace(hour=9, minute=0, second=0, microsecond=0)
        article_id = save_manhwa_to_db(
            engine, prompt_result, image_url, published_at, title, slug
        )
        print(f"  ✅ 기사 ID: {article_id}")

        result.update({
            "success": True,
            "article_id": article_id,
            "image_url": image_url,
            "title": title,
            "cost_usd": 0.07,  # 텍스트 ~$0.01 + 이미지 ~$0.06
        })

    except Exception as e:
        import traceback
        result["error"] = str(e)
        print(f"\n❌ 오류 발생: {e}")
        traceback.print_exc()

    finally:
        db_manager.close_engine()

    # Slack 알림
    _send_slack_result(result)

    status = 200 if result["success"] else 500
    return result, status


def _send_slack_result(result):
    """만평 생성 결과 Slack 알림"""
    webhook_url = os.getenv("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        return

    if result["success"]:
        header = "🎨 한돈투데이 만평 — 생성 완료"
        body = (
            f"*✅ 만평 발행 완료*\n"
            f"• 제목: {result['title']}\n"
            f"• 기사 ID: {result['article_id']}\n"
            f"• 이미지: {result['image_url']}\n"
            f"• 비용: ${result['cost_usd']:.3f} (≈{result['cost_usd']*1400:.0f}원)"
        )
    else:
        header = "🎨 한돈투데이 만평 — ⚠️ 생성 실패"
        body = f"*❌ 오류 내용*\n{result['error']}"

    import urllib.request
    payload = {
        "text": header,
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": header, "emoji": True}},
            {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        ],
    }
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        print("  ✅ Slack 알림 전송")
    except Exception as e:
        print(f"  ⚠️ Slack 알림 실패: {e}")
