"""
================================================================
  한돈투데이 금요일 웹툰 자동 게시 파이프라인
  weekly_webtoon_fri.py
  v1.0.0
================================================================

[역할]
  매주 금요일 08:00 KST 자동 실행
  1. 이번 주 금요일 웹툰 중복 체크
  2. GCS에서 다음 화 이미지 2~3장 다운로드
  3. Pillow로 세로 병합 + JPG 85% 압축
  4. 합친 이미지를 GCS에 업로드
  5. Claude Sonnet 4.6 → 합친 이미지에서 제목 자동 추출
  6. DB INSERT (category='웹툰', publish_status='published')
  7. Slack 알림

[GCS 파일 규칙]
  원본: gs://handontoday-articles/webtoon/fri/fri_ep01_1.jpg, fri_ep01_2.jpg
  합침: gs://handontoday-articles/webtoon/fri/fri_ep01.jpg

[환경변수]
  OPENROUTER_API_KEY  - OpenRouter API 키 (Claude Sonnet 4.6)
  SLACK_WEBHOOK_URL   - Slack Webhook URL
  CLOUD_SQL_PASSWORD  - DB 비밀번호
  DB_HOST             - Cloud SQL 소켓 경로
  DB_NAME             - DB명
  DB_USER             - DB 유저
  GCS_BUCKET          - GCS 버킷명

[진입점]
  run_webtoon_fri_pipeline(request) — Cloud Functions HTTP 트리거
"""

import os
import io
import json
import base64
import requests
import functions_framework

from datetime import datetime, timezone, timedelta
from google.cloud import storage
from PIL import Image

import db_manager

# ──────────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────────

KST = timezone(timedelta(hours=9))
GCS_BUCKET = os.getenv("GCS_BUCKET", "handontoday-articles")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
TITLE_MODEL = "anthropic/claude-sonnet-4-6"
WEBTOON_FOLDER = "webtoon/fri"
SLUG_PREFIX = "fri_ep"
MAX_EPISODE = 19  # 오돈출 시리즈 총 화수

TITLE_SYSTEM_PROMPT = """이 웹툰 이미지 상단에서 시리즈 제목과 화수+에피소드 제목을 모두 읽어서 반환해줘.

[출력 형식 - JSON만, 다른 텍스트 없이]
{
  "title": "시리즈 제목 N화 - 에피소드 제목"
}

규칙:
- 이미지 상단의 시리즈 제목, 화수, 에피소드 제목을 정확히 그대로 읽을 것
- 예시: 진우농장일기 1화 - 그건 농가 책임입니다
- 임의로 바꾸거나 추가하지 말 것
- JSON만 출력"""


# ──────────────────────────────────────────────────
# 다음 화수 조회
# ──────────────────────────────────────────────────

def get_next_episode_number(engine):
    """DB에서 금요일 웹툰 최신 화수 조회 → 다음 화수 반환"""
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
        return 1

    # slug: fri_ep01 → 1 추출
    try:
        last_ep = int(row[0].replace(SLUG_PREFIX, ""))
        return last_ep + 1
    except Exception:
        return 1


# ──────────────────────────────────────────────────
# 중복 체크
# ──────────────────────────────────────────────────

def check_already_exists(engine, this_friday):
    """이번 주 금요일 웹툰이 이미 있으면 True"""
    from sqlalchemy import text

    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT id, title FROM generated_articles
            WHERE category = '웹툰'
              AND slug LIKE :prefix
              AND published_at >= :this_friday
            LIMIT 1
        """), {
            "prefix": f"{SLUG_PREFIX}%",
            "this_friday": this_friday.astimezone(timezone.utc),
        }).fetchone()

    if row:
        print(f"  ⚠️ 이번 주 금요일 웹툰 이미 존재: id={row[0]}, title={row[1]}")
        return True
    return False


# ──────────────────────────────────────────────────
# GCS에서 이미지 다운로드 (2~3장)
# ──────────────────────────────────────────────────

def download_images_from_gcs(episode_num):
    """GCS에서 해당 화 이미지 2~3장 다운로드 → [(bytes, ext), ...] 반환"""
    client = storage.Client()
    bucket = client.bucket(GCS_BUCKET)
    images = []

    for part_num in [1, 2, 3]:
        found = False
        for ext in ["jpg", "jpeg", "png"]:
            blob_name = f"{WEBTOON_FOLDER}/{SLUG_PREFIX}{episode_num:02d}_{part_num}.{ext}"
            blob = bucket.blob(blob_name)
            if blob.exists():
                image_bytes = blob.download_as_bytes()
                images.append((image_bytes, ext))
                print(f"  ✅ 다운로드: {blob_name} ({len(image_bytes):,} bytes)")
                found = True
                break

        # _1, _2는 필수, _3은 선택
        if not found and part_num <= 2:
            raise FileNotFoundError(
                f"GCS에서 {SLUG_PREFIX}{episode_num:02d}_{part_num} 파일을 찾을 수 없습니다. "
                f"경로: gs://{GCS_BUCKET}/{WEBTOON_FOLDER}/"
            )
        elif not found:
            break  # _3이 없으면 정상 종료

    return images


# ──────────────────────────────────────────────────
# Pillow: 세로 병합 + 압축
# ──────────────────────────────────────────────────

def merge_images(image_list):
    """이미지 2~3장을 세로로 병합 → JPG bytes 반환"""
    pil_images = []
    for img_bytes, ext in image_list:
        img = Image.open(io.BytesIO(img_bytes)).convert('RGB')
        pil_images.append(img)

    # 최대 너비에 맞춰 리사이즈
    max_w = max(img.width for img in pil_images)
    resized = []
    for img in pil_images:
        if img.width != max_w:
            ratio = max_w / img.width
            new_h = int(img.height * ratio)
            img = img.resize((max_w, new_h), Image.LANCZOS)
        resized.append(img)

    # 세로 합치기
    total_h = sum(img.height for img in resized)
    combined = Image.new('RGB', (max_w, total_h))
    y_offset = 0
    for img in resized:
        combined.paste(img, (0, y_offset))
        y_offset += img.height

    # JPG 85% 압축
    buf = io.BytesIO()
    combined.save(buf, format='JPEG', quality=85, optimize=True)
    result_bytes = buf.getvalue()

    print(f"  ✅ 병합 완료: {max_w}x{total_h}, {len(result_bytes)//1024}KB")
    return result_bytes


# ──────────────────────────────────────────────────
# GCS 업로드 (합친 이미지)
# ──────────────────────────────────────────────────

def upload_merged_to_gcs(image_bytes, episode_num):
    """합친 이미지를 GCS에 업로드 → 공개 URL 반환"""
    client = storage.Client()
    bucket = client.bucket(GCS_BUCKET)
    blob_name = f"{WEBTOON_FOLDER}/{SLUG_PREFIX}{episode_num:02d}.jpg"
    blob = bucket.blob(blob_name)
    blob.upload_from_string(image_bytes, content_type="image/jpeg")
    return f"https://storage.googleapis.com/{GCS_BUCKET}/{blob_name}"


# ──────────────────────────────────────────────────
# 제목 추출 (Claude Sonnet 4.6)
# ──────────────────────────────────────────────────

def extract_title(image_bytes):
    """Claude Sonnet 4.6으로 합친 이미지 상단에서 제목 추출"""
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
                            "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}
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
            "match_reason": f"금요일 웹툰 자동 게시 {episode_num}화",
            "published_at": published_at.astimezone(timezone.utc),
        })
        article_id = row.scalar()

    return article_id


# ──────────────────────────────────────────────────
# 이번 주 금요일 00:00 KST 계산
# ──────────────────────────────────────────────────

def get_this_friday(dt):
    """이번 주 금요일 00:00 KST 반환 (금=4)"""
    days_since_monday = dt.weekday()
    monday = dt - timedelta(days=days_since_monday)
    friday = monday + timedelta(days=4)
    return friday.replace(hour=0, minute=0, second=0, microsecond=0)


# ──────────────────────────────────────────────────
# Cloud Functions 진입점
# ──────────────────────────────────────────────────

@functions_framework.http
def run_webtoon_fri_pipeline(request):
    """금요일 웹툰 파이프라인 — 매주 금요일 08:00 KST 실행"""
    now_kst = datetime.now(KST)
    print(f"\n{'='*50}")
    print(f"  📖 한돈투데이 금요일 웹툰 파이프라인 v1.0.0")
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
        this_friday = get_this_friday(now_kst)

        # 0. 중복 체크
        print(f"\n[0/5] 중복 체크 (이번 주 금요일: {this_friday.strftime('%Y-%m-%d')})...")
        if check_already_exists(engine, this_friday):
            result["error"] = "이번 주 금요일 웹툰이 이미 존재합니다. 스킵합니다."
            _send_slack_result(result)
            return result, 200

        # 1. 다음 화수 조회
        print("\n[1/5] 다음 화수 조회...")
        episode_num = get_next_episode_number(engine)

        # 완결 체크
        if episode_num > MAX_EPISODE:
            result["error"] = f"연재 완료 ({MAX_EPISODE}화 완결). 더 이상 게시할 화가 없습니다."
            print(f"\n🎉 연재 완료! {MAX_EPISODE}화 전체 게시됨.")
            _send_slack_result(result)
            return result, 200

        slug = f"{SLUG_PREFIX}{episode_num:02d}"
        print(f"  ✅ 다음 화: {episode_num}화 (slug: {slug})")

        # 2. GCS에서 이미지 다운로드 (2~3장)
        print(f"\n[2/5] GCS에서 {slug} 이미지 다운로드...")
        image_list = download_images_from_gcs(episode_num)
        print(f"  총 {len(image_list)}장")

        # 3. 이미지 세로 병합 + 압축
        print("\n[3/5] 이미지 세로 병합 + JPG 85% 압축...")
        merged_bytes = merge_images(image_list)

        # 4. 합친 이미지 GCS 업로드 + 제목 추출
        print("\n[4/5] GCS 업로드 + 제목 추출...")
        image_url = upload_merged_to_gcs(merged_bytes, episode_num)
        print(f"  ✅ 업로드: {image_url}")

        title = extract_title(merged_bytes)
        print(f"  ✅ 제목: {title}")

        # 5. DB 저장
        print("\n[5/5] DB 저장...")
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

    if result.get("error") == "이번 주 금요일 웹툰이 이미 존재합니다. 스킵합니다.":
        header = "📖 금요일 웹툰 — 중복 스킵"
        body = "*⚠️ 이번 주 금요일 웹툰이 이미 존재하여 스킵합니다.*"
    elif f"연재 완료 ({MAX_EPISODE}화 완결)" in result.get("error", ""):
        header = "📖 금요일 웹툰 — 🎉 연재 완료"
        body = f"*🎉 오돈출 시리즈 {MAX_EPISODE}화 완결!*\n더 이상 게시할 화가 없습니다. 스케줄러를 비활성화해주세요."
    elif result["success"]:
        header = "📖 금요일 웹툰 — 게시 완료"
        body = (
            f"*✅ 웹툰 발행 완료*\n"
            f"• 제목: {result['title']}\n"
            f"• 화수: {result['episode']}화\n"
            f"• 기사 ID: {result['article_id']}\n"
            f"• 이미지: {result['image_url']}"
        )
    elif "찾을 수 없습니다" in result.get("error", ""):
        header = "📖 금요일 웹툰 — ⚠️ 이미지 없음"
        body = f"*❌ GCS에 다음 화 이미지가 없습니다.*\n{result['error']}\n\n이미지를 업로드해주세요."
    else:
        header = "📖 금요일 웹툰 — ⚠️ 게시 실패"
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
