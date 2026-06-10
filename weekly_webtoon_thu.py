"""
================================================================
  한돈투데이 목요일 웹툰 자동 게시 파이프라인
  weekly_webtoon_thu.py
  v1.0.0
================================================================

[역할]
  매주 목요일 08:00 KST 자동 실행
  1. 이번 주 목요일 웹툰 중복 체크
  2. GCS에서 다음 화 이미지 조회 (thu_ep{N}.png)
  3. Claude Sonnet 4.6 → 이미지 보고 제목 자동 추출
  4. DB INSERT (category='웹툰', publish_status='published')
  5. Slack 알림

[GCS 파일 규칙]
  gs://handontoday-articles/webtoon/thu/thu_ep01.jpg
  gs://handontoday-articles/webtoon/thu/thu_ep02.jpg
  ...

[환경변수]
  OPENROUTER_API_KEY  - OpenRouter API 키 (Claude Sonnet 4.6)
  SLACK_WEBHOOK_URL   - Slack Webhook URL
  CLOUD_SQL_PASSWORD  - DB 비밀번호
  DB_HOST             - Cloud SQL 소켓 경로
  DB_NAME             - DB명
  DB_USER             - DB 유저
  GCS_BUCKET          - GCS 버킷명

[진입점]
  run_webtoon_thu_pipeline(request) — Cloud Functions HTTP 트리거
"""

import os
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
TITLE_MODEL = "anthropic/claude-sonnet-4-6"
WEBTOON_FOLDER = "webtoon/thu"
SLUG_PREFIX = "thu_ep"
MAX_EPISODE = 31  # 진우농장일기 총 화수

TITLE_SYSTEM_PROMPT = """이 웹툰 이미지 상단에 있는 제목 텍스트를 그대로 읽어서 반환해줘.

[출력 형식 - JSON만, 다른 텍스트 없이]
{
  "title": "이미지에서 읽은 제목 그대로"
}

규칙:
- 이미지 상단의 제목 텍스트를 정확히 그대로 읽을 것
- 임의로 바꾸거나 추가하지 말 것
- JSON만 출력"""


# ──────────────────────────────────────────────────
# 다음 화수 조회
# ──────────────────────────────────────────────────

def get_next_episode_number(engine):
    """DB에서 목요일 웹툰 최신 화수 조회 → 다음 화수 반환"""
    from sqlalchemy import text

    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT slug FROM generated_articles
            WHERE category = '웹툰'
              AND slug LIKE :prefix
            ORDER BY published_at DESC
            LIMIT 1
        """), {"prefix": f"{SLUG_PREFIX}%"}).fetchone()

    if not row:
        return 1  # 첫 화

    # slug: thu_ep01 → 1 추출
    try:
        last_ep = int(row[0].replace(SLUG_PREFIX, ""))
        return last_ep + 1
    except Exception:
        return 1


# ──────────────────────────────────────────────────
# 중복 체크
# ──────────────────────────────────────────────────

def check_already_exists(engine, this_thursday):
    """이번 주 목요일 웹툰이 이미 있으면 True"""
    from sqlalchemy import text

    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT id, title FROM generated_articles
            WHERE category = '웹툰'
              AND slug LIKE :prefix
              AND published_at >= :this_thursday
            LIMIT 1
        """), {
            "prefix": f"{SLUG_PREFIX}%",
            "this_thursday": this_thursday.astimezone(timezone.utc),
        }).fetchone()

    if row:
        print(f"  ⚠️ 이번 주 목요일 웹툰 이미 존재: id={row[0]}, title={row[1]}")
        return True
    return False


# ──────────────────────────────────────────────────
# GCS에서 이미지 다운로드
# ──────────────────────────────────────────────────

def download_image_from_gcs(episode_num):
    """GCS에서 해당 화 이미지 다운로드 → bytes 반환"""
    client = storage.Client()
    bucket = client.bucket(GCS_BUCKET)

    # png, jpg 순서로 시도
    for ext in ["png", "jpg", "jpeg"]:
        blob_name = f"{WEBTOON_FOLDER}/{SLUG_PREFIX}{episode_num:02d}.{ext}"
        blob = bucket.blob(blob_name)
        if blob.exists():
            image_bytes = blob.download_as_bytes()
            print(f"  ✅ 이미지 다운로드: {blob_name} ({len(image_bytes):,} bytes)")
            return image_bytes, blob_name, ext

    raise FileNotFoundError(
        f"GCS에서 {SLUG_PREFIX}{episode_num:02d}.png/jpg 파일을 찾을 수 없습니다. "
        f"경로: gs://{GCS_BUCKET}/{WEBTOON_FOLDER}/"
    )


# ──────────────────────────────────────────────────
# 제목 추출 (Claude Sonnet 4.6)
# ──────────────────────────────────────────────────

def extract_title(image_bytes, ext):
    """Claude Sonnet 4.6으로 이미지 상단 제목 추출"""
    media_type = f"image/{'jpeg' if ext in ['jpg', 'jpeg'] else 'png'}"
    img_b64 = base64.b64encode(image_bytes).decode()

    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://handontoday.com",
            "X-Title": "Handon Today Webtoon",
        },
        json={
            "model": TITLE_MODEL,
            "messages": [
                {"role": "system", "content": TITLE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{media_type};base64,{img_b64}"}
                        },
                        {"type": "text", "text": "이미지 상단 제목을 읽어줘."}
                    ]
                }
            ],
        },
        timeout=60,
    )
    resp.raise_for_status()

    text = resp.json()["choices"][0]["message"]["content"]
    text = text.strip().replace("```json", "").replace("```", "").strip()
    result = json.loads(text)
    return result.get("title", "")


# ──────────────────────────────────────────────────
# GCS 공개 URL 반환 (이미지는 이미 업로드되어 있음)
# ──────────────────────────────────────────────────

def get_public_url(blob_name):
    return f"https://storage.googleapis.com/{GCS_BUCKET}/{blob_name}"


# ──────────────────────────────────────────────────
# DB 저장
# ──────────────────────────────────────────────────

def save_to_db(engine, title, slug, image_url, published_at, episode_num):
    """웹툰 기사를 generated_articles에 저장"""
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
                '웹툰', '[]',
                :match_reason, '[]', '[]',
                true,
                'published', :published_at,
                NOW(), NOW(), NOW()
            )
            RETURNING id
        """), {
            "title": title,
            "deck": "",
            "slug": slug,
            "image_url": image_url,
            "body": "",
            "body_markdown": "",
            "body_html": "",
            "match_reason": f"목요일 웹툰 자동 게시 {episode_num}화",
            "published_at": published_at.astimezone(timezone.utc),
        })
        article_id = row.scalar()

    return article_id


# ──────────────────────────────────────────────────
# 이번 주 목요일 00:00 KST 계산
# ──────────────────────────────────────────────────

def get_this_thursday(dt):
    """이번 주 목요일 00:00 KST 반환 (목=3)"""
    days_since_monday = dt.weekday()  # 월=0 ... 목=3
    monday = dt - timedelta(days=days_since_monday)
    thursday = monday + timedelta(days=3)
    return thursday.replace(hour=0, minute=0, second=0, microsecond=0)


# ──────────────────────────────────────────────────
# Cloud Functions 진입점
# ──────────────────────────────────────────────────

@functions_framework.http
def run_webtoon_thu_pipeline(request):
    """목요일 웹툰 파이프라인 — 매주 목요일 08:00 KST 실행"""
    now_kst = datetime.now(KST)
    print(f"\n{'='*50}")
    print(f"  📖 한돈투데이 목요일 웹툰 파이프라인 v1.0.0")
    print(f"  실행 시각: {now_kst.strftime('%Y-%m-%d %H:%M KST')}")
    print(f"{'='*50}")

    result = {
        "success": False,
        "error": "",
        "article_id": None,
        "image_url": "",
        "title": "",
        "episode": None,
    }

    try:
        engine = db_manager.get_engine()
        this_thursday = get_this_thursday(now_kst)

        # 0. 중복 체크
        print(f"\n[0/4] 중복 체크 (이번 주 목요일: {this_thursday.strftime('%Y-%m-%d')})...")
        if check_already_exists(engine, this_thursday):
            result["error"] = "이번 주 목요일 웹툰이 이미 존재합니다. 스킵합니다."
            _send_slack_result(result)
            return result, 200

        # 1. 다음 화수 조회
        print("\n[1/4] 다음 화수 조회...")
        episode_num = get_next_episode_number(engine)

        # 완결 체크
        if episode_num > MAX_EPISODE:
            result["error"] = f"연재 완료 ({MAX_EPISODE}화 완결). 더 이상 게시할 화가 없습니다."
            print(f"\n🎉 연재 완료! {MAX_EPISODE}화 전체 게시됨.")
            _send_slack_result(result)
            return result, 200

        slug = f"{SLUG_PREFIX}{episode_num:02d}"
        print(f"  ✅ 다음 화: {episode_num}화 (slug: {slug})")

        # 2. GCS에서 이미지 다운로드
        print(f"\n[2/4] GCS에서 {slug} 이미지 다운로드...")
        image_bytes, blob_name, ext = download_image_from_gcs(episode_num)
        image_url = get_public_url(blob_name)

        # 3. 제목 추출 (Claude Sonnet 4.6)
        print("\n[3/4] 제목 추출 (Claude Sonnet 4.6)...")
        title = extract_title(image_bytes, ext)
        print(f"  ✅ 제목: {title}")

        # 4. DB 저장
        print("\n[4/4] DB 저장...")
        published_at = now_kst.replace(hour=8, minute=0, second=0, microsecond=0)
        article_id = save_to_db(
            engine, title, slug, image_url, published_at, episode_num
        )
        print(f"  ✅ 기사 ID: {article_id}")

        result.update({
            "success": True,
            "article_id": article_id,
            "image_url": image_url,
            "title": title,
            "episode": episode_num,
        })

    except FileNotFoundError as e:
        result["error"] = str(e)
        print(f"\n⚠️ 이미지 없음: {e}")

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
    """웹툰 게시 결과 Slack 알림"""
    webhook_url = os.getenv("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        return

    if result.get("error") == "이번 주 목요일 웹툰이 이미 존재합니다. 스킵합니다.":
        header = "📖 목요일 웹툰 — 중복 스킵"
        body = "*⚠️ 이번 주 목요일 웹툰이 이미 존재하여 스킵합니다.*"
    elif f"연재 완료 ({MAX_EPISODE}화 완결)" in result.get("error", ""):
        header = "📖 목요일 웹툰 — 🎉 연재 완료"
        body = f"*🎉 진우농장일기 {MAX_EPISODE}화 완결!*\n더 이상 게시할 화가 없습니다. 스케줄러를 비활성화해주세요."
    elif result["success"]:
        header = "📖 목요일 웹툰 — 게시 완료"
        body = (
            f"*✅ 웹툰 발행 완료*\n"
            f"• 제목: {result['title']}\n"
            f"• 화수: {result['episode']}화\n"
            f"• 기사 ID: {result['article_id']}\n"
            f"• 이미지: {result['image_url']}"
        )
    elif "이미지 없음" in result.get("error", "") or "찾을 수 없습니다" in result.get("error", ""):
        header = "📖 목요일 웹툰 — ⚠️ 이미지 없음"
        body = f"*❌ GCS에 다음 화 이미지가 없습니다.*\n{result['error']}\n\n이미지를 업로드해주세요."
    else:
        header = "📖 목요일 웹툰 — ⚠️ 게시 실패"
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
