"""
================================================================
  한돈투데이 주간 미래 만평 자동 생성 파이프라인
  weekly_future.py
  v1.1.0
================================================================

[변경사항 v1.1.0]
  - Pillow 배지 합성 제거 (페이지 레이아웃에서 2045 표현으로 변경)

[역할]
  매주 화요일 08:00 KST 자동 실행
  1. 이번 주 미래 만평 중복 체크
  2. 이번 주 월요일 만평의 주제(deck) DB 조회
  3. Gemini → 20년 후 풍자 장면 설계 (JSON)
  4. 나노바나나2 → 이미지 생성
  5. GCS 저장: manhwa/future-YYYY-WNN.jpg
  6. DB INSERT (category='만평', slug=future-...)
  7. Slack 알림

[환경변수]
  OPENROUTER_API_KEY  - OpenRouter API 키
  SLACK_WEBHOOK_URL   - Slack Webhook URL
  CLOUD_SQL_PASSWORD  - DB 비밀번호
  DB_HOST             - Cloud SQL 소켓 경로
  DB_NAME             - DB명
  DB_USER             - DB 유저
  GCS_BUCKET          - GCS 버킷명

[진입점]
  run_future_pipeline(request) — Cloud Functions HTTP 트리거
"""

import os
import io
import json
import base64
import requests
import functions_framework

from datetime import datetime, timezone, timedelta
from google.cloud import storage

import db_manager

# ──────────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────────

KST = timezone(timedelta(hours=9))
GCS_BUCKET = os.getenv("GCS_BUCKET", "handontoday-articles")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

TEXT_MODEL  = "google/gemini-2.5-flash-lite"
IMAGE_MODEL = "google/gemini-3.1-flash-image-preview"

SYSTEM_PROMPT = """너는 20년 경력의 한국 시사만평 작가야.
날카로운 풍자와 블랙코미디로 유명하며,
보는 사람이 피식 웃으면서도 뼈아프게 공감하는 만평을 그린다.

현재 양돈 업계 이슈를 받으면,
이 흐름이 2045년에 극단적으로 진행됐을 때의
가장 아이러니하고 풍자적인 장면 하나를 골라라.

[좋은 만평의 조건]
- 과장: 현실을 비틀어 극단까지 밀어붙임
- 아이러니: 말 안 해도 비판이 느껴짐
- 한 방: 보자마자 메시지가 꽂힘
- 해학: 웃기면서 슬프거나, 슬프면서 웃김

[나쁜 만평 (절대 금지)]
- 단순 미래 풍경화
- 설명이 필요한 그림
- 희망차고 긍정적인 광고 이미지
- 뻔한 로봇/AI 클리셰

[장면 설계 규칙]
- 한국적 맥락이 느껴지는 구체적인 장면을 설정할 것
- 한국 돼지고기(한돈)가 등장할 경우 태극기 등으로 명확히 표시
- 인물의 표정과 행동으로 메시지를 전달
- 장면만 봐도 무슨 비판인지 알 수 있어야 함

[스타일 고정]
Painterly editorial cartoon.
한국 웹툰의 선명한 선 + 시사만평의 날카로운 과장.
인물 표정은 코믹하게 과장. NOT photorealistic.

[절대 금지]
- 이미지 안 텍스트·글자·숫자 일체
- 고어·잔인한 장면
- 실존 인물 얼굴

[출력 형식 - JSON만, 다른 텍스트 없이]
{
  "current_issue": "현재 이슈 (한국어, 1문장)",
  "satire_point": "풍자 포인트 — 뭘 비판하는가 (한국어, 1문장)",
  "scene": "2045년 구체적 장면 묘사 (한국어, 3~4문장, 최대한 세밀하게)",
  "mood": "분위기 (예: 블랙코미디, 씁쓸한 유머, 냉소적 풍자)",
  "prompt": "이미지 생성 프롬프트 (한국어+영어 혼용, 200단어 이내)"
}"""


# ──────────────────────────────────────────────────
# 주차 계산
# ──────────────────────────────────────────────────

def get_this_week_monday(dt):
    """이번 주 월요일 00:00 KST 반환"""
    days_since_monday = dt.weekday()
    monday = dt - timedelta(days=days_since_monday)
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def get_week_info(dt):
    """미래 만평 제목/슬러그/GCS키 반환"""
    year = dt.year
    month = dt.month
    first_of_month = dt.replace(day=1)
    days_until_first_monday = (7 - first_of_month.weekday()) % 7
    first_monday_day = 1 + days_until_first_monday
    week_of_month = ((dt.day - first_monday_day) // 7) + 1
    iso_year, iso_week, _ = dt.isocalendar()

    title = f"{year}년 {month}월 {week_of_month}주차 미래 만평"
    slug = f"future-{year}-{month:02d}-w{week_of_month}"
    gcs_key = f"future-{iso_year}-W{iso_week:02d}"

    return title, slug, gcs_key


# ──────────────────────────────────────────────────
# 중복 체크
# ──────────────────────────────────────────────────

def check_already_exists(engine, this_monday):
    """이번 주 미래 만평이 이미 있으면 True"""
    from sqlalchemy import text

    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT id, title FROM generated_articles
            WHERE category = '만평'
              AND slug LIKE 'future-%'
              AND published_at >= :this_monday
            LIMIT 1
        """), {"this_monday": this_monday.astimezone(timezone.utc)}).fetchone()

    if row:
        print(f"  ⚠️ 이번 주 미래 만평 이미 존재: id={row[0]}, title={row[1]}")
        return True
    return False


# ──────────────────────────────────────────────────
# 월요일 만평 주제 조회
# ──────────────────────────────────────────────────

def fetch_this_week_manhwa_topic(engine, this_monday):
    """이번 주 월요일 만평의 주제(deck) 조회"""
    from sqlalchemy import text

    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT id, title, deck
            FROM generated_articles
            WHERE category = '만평'
              AND slug NOT LIKE 'future-%'
              AND published_at >= :this_monday
            ORDER BY published_at ASC
            LIMIT 1
        """), {"this_monday": this_monday.astimezone(timezone.utc)}).fetchone()

    if not row:
        return None

    return {
        "id": row[0],
        "title": row[1],
        "topic": row[2],
    }


# ──────────────────────────────────────────────────
# 미래 장면 생성
# ──────────────────────────────────────────────────

def generate_future_prompt(topic):
    """Gemini로 2045년 풍자 장면 설계"""
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://handontoday.com",
            "X-Title": "Handon Today Future Manhwa",
        },
        json={
            "model": TEXT_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"현재 이슈: {topic}"},
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
    """나노바나나2로 이미지 생성 → bytes 반환"""
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://handontoday.com",
            "X-Title": "Handon Today Future Manhwa",
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
    content = msg.get("content") or []
    if isinstance(content, list):
        for part in content:
            if part.get("type") == "image_url":
                url = part["image_url"]["url"]
                if url.startswith("data:image"):
                    return base64.b64decode(url.split(",", 1)[1])
                return requests.get(url, timeout=60).content

    for img in msg.get("images", []):
        url = img.get("image_url", {}).get("url", "")
        if url.startswith("data:image"):
            return base64.b64decode(url.split(",", 1)[1])
        elif url:
            return requests.get(url, timeout=60).content

    raise ValueError(f"이미지 URL 없음. message keys: {list(msg.keys())}")


# ──────────────────────────────────────────────────
# GCS 저장
# ──────────────────────────────────────────────────

def upload_to_gcs(image_bytes, gcs_key):
    """GCS에 미래 만평 이미지 저장 → 공개 URL 반환"""
    client = storage.Client()
    bucket = client.bucket(GCS_BUCKET)
    blob_name = f"manhwa/{gcs_key}.jpg"
    blob = bucket.blob(blob_name)
    blob.upload_from_string(image_bytes, content_type="image/jpeg")
    return f"https://storage.googleapis.com/{GCS_BUCKET}/{blob_name}"


# ──────────────────────────────────────────────────
# DB 저장
# ──────────────────────────────────────────────────

def save_to_db(engine, result, manhwa_topic, image_url, published_at, title, slug):
    """미래 만평 기사를 generated_articles에 저장"""
    from sqlalchemy import text

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
            "deck": manhwa_topic,
            "slug": slug,
            "image_url": image_url,
            "body": result.get("satire_point", ""),
            "body_markdown": result.get("scene", ""),
            "body_html": f"<p>{result.get('scene', '')}</p>",
            "match_reason": result.get("satire_point", ""),
            "published_at": published_at.astimezone(timezone.utc),
        })
        article_id = row.scalar()

    return article_id


# ──────────────────────────────────────────────────
# Cloud Functions 진입점
# ──────────────────────────────────────────────────

@functions_framework.http
def run_future_pipeline(request):
    """미래 만평 파이프라인 — 매주 화요일 08:00 KST 실행"""
    now_kst = datetime.now(KST)
    print(f"\n{'='*50}")
    print(f"  🔮 한돈투데이 미래 만평 파이프라인 v1.1.0")
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
        this_monday = get_this_week_monday(now_kst)

        # 0. 중복 체크
        print(f"\n[0/5] 중복 체크 (이번 주 월요일: {this_monday.strftime('%Y-%m-%d')})...")
        if check_already_exists(engine, this_monday):
            result["error"] = "이번 주 미래 만평이 이미 존재합니다. 스킵합니다."
            _send_slack_result(result)
            return result, 200

        # 1. 이번 주 월요일 만평 주제 조회
        print("\n[1/5] 이번 주 만평 주제 조회...")
        manhwa = fetch_this_week_manhwa_topic(engine, this_monday)
        if not manhwa:
            raise ValueError("이번 주 월요일 만평이 없습니다. 월요일 만평 생성 후 재시도하세요.")
        print(f"  ✅ 주제: {manhwa['topic']}")

        # 2. 2045년 풍자 장면 설계
        print("\n[2/5] Gemini가 2045년 풍자 장면 설계 중...")
        future_result = generate_future_prompt(manhwa["topic"])
        print(f"  풍자: {future_result.get('satire_point')}")
        print(f"  장면: {future_result.get('scene', '')[:80]}...")
        print(f"  분위기: {future_result.get('mood')}")

        # 3. 이미지 생성
        print("\n[3/5] 이미지 생성 (나노바나나2)...")
        image_bytes = generate_image(future_result["prompt"])
        print(f"  ✅ {len(image_bytes):,} bytes")

        # 4. GCS 저장
        print("\n[4/5] GCS 저장...")
        title, slug, gcs_key = get_week_info(now_kst)
        image_url = upload_to_gcs(image_bytes, gcs_key)
        print(f"  ✅ {image_url}")

        # 5. DB 저장 (화요일 08:00 KST)
        print("\n[5/5] DB 저장...")
        published_at = now_kst.replace(hour=8, minute=0, second=0, microsecond=0)
        article_id = save_to_db(
            engine, future_result, manhwa["topic"],
            image_url, published_at, title, slug
        )
        print(f"  ✅ 기사 ID: {article_id}")

        result.update({
            "success": True,
            "article_id": article_id,
            "image_url": image_url,
            "title": title,
            "cost_usd": 0.07,
        })

    except Exception as e:
        import traceback
        result["error"] = str(e)
        print(f"\n❌ 오류: {e}")
        traceback.print_exc()

    finally:
        db_manager.close_engine()

    _send_slack_result(result)
    status = 200 if result["success"] else 500
    return result, status


def _send_slack_result(result):
    """미래 만평 결과 Slack 알림"""
    webhook_url = os.getenv("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        return

    if result.get("error") == "이번 주 미래 만평이 이미 존재합니다. 스킵합니다.":
        header = "🔮 한돈투데이 미래 만평 — 중복 스킵"
        body = "*⚠️ 이번 주 미래 만평이 이미 존재하여 스킵합니다.*"
    elif result["success"]:
        header = "🔮 한돈투데이 미래 만평 — 생성 완료"
        body = (
            f"*✅ 미래 만평 발행 완료*\n"
            f"• 제목: {result['title']}\n"
            f"• 기사 ID: {result['article_id']}\n"
            f"• 이미지: {result['image_url']}\n"
            f"• 비용: ${result['cost_usd']:.3f} (≈{result['cost_usd']*1400:.0f}원)"
        )
    else:
        header = "🔮 한돈투데이 미래 만평 — ⚠️ 생성 실패"
        body = f"*❌ 오류*\n{result['error']}"

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
            webhook_url, data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        print("  ✅ Slack 알림 전송")
    except Exception as e:
        print(f"  ⚠️ Slack 알림 실패: {e}")
