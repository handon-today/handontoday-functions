"""
================================================================
  Cloud Functions 진입점 — main.py
  v3.4.0
  main.py
================================================================

[v3 변경사항]
  - overseas_collector v3 반환값 {"asia": [...], "global": [...]} 처리
  - 글로벌 기사 소스: 아시아 6 : 영어권 4 비중 제어
  - is_overseas_pair() 판단 기준에 아시아 소스명 추가
    (article_generator.py도 동일하게 수정 필요)

[v2 내용 유지]
  - Cloud SQL (PostgreSQL) 연동
  - pipeline_runs + generated_articles DB 저장
  - GCS 이중 백업
  - Slack 알림
"""

import os
import json
import random
import traceback
from sqlalchemy import text
from datetime import datetime, timezone, timedelta

import functions_framework
from google.cloud import storage

import korea_crawler
import overseas_collector
import article_generator
import daily_briefing
import notifier
import db_manager


# ──────────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────────

GCS_BUCKET = os.getenv("GCS_BUCKET", "handontoday-articles")
KST = timezone(timedelta(hours=9))

# 글로벌 기사 소스 비중 (아시아 : 영어권 = 6 : 4)
# 회당 all_raw에 넣을 아시아 기사 수
# → 매칭 풀 크기: 아시아 N건 + 영어권 M건 (N:M ≈ 6:4)
ASIA_RATIO   = 0.6
GLOBAL_RATIO = 0.4

# 매칭 풀에 넣을 해외 기사 총 수 (국내 기사와 합쳐 AI 매칭)
OVERSEAS_POOL_SIZE = 12   # 아시아 ~7건 + 영어권 ~5건


def get_kst_timestamp():
    return datetime.now(KST).strftime("%Y%m%d_%H%M")


def get_kst_now():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")


# ──────────────────────────────────────────────────
# 비중 제어: 아시아 6 : 영어권 4
# ──────────────────────────────────────────────────

def build_overseas_pool(overseas: dict, pool_size: int = OVERSEAS_POOL_SIZE) -> list:
    """
    아시아 / 영어권 기사를 6:4 비중으로 섞어 pool_size 건 반환.
    소스가 부족하면 있는 것으로 채움.
    """
    asia_pool   = list(overseas.get("asia", []))
    global_pool = list(overseas.get("global", []))

    # 각 풀을 랜덤 셔플 (매 실행마다 다른 소스 조합)
    random.shuffle(asia_pool)
    random.shuffle(global_pool)

    asia_quota   = round(pool_size * ASIA_RATIO)    # 12건 기준 → 7건
    global_quota = pool_size - asia_quota            # → 5건

    asia_picks   = asia_pool[:asia_quota]
    global_picks = global_pool[:global_quota]

    # 한쪽 소스 부족 시 상대편으로 보충
    asia_shortfall   = asia_quota   - len(asia_picks)
    global_shortfall = global_quota - len(global_picks)

    if asia_shortfall > 0:
        extra = global_pool[global_quota: global_quota + asia_shortfall]
        global_picks = global_picks + extra

    if global_shortfall > 0:
        extra = asia_pool[asia_quota: asia_quota + global_shortfall]
        asia_picks = asia_picks + extra

    pool = asia_picks + global_picks
    random.shuffle(pool)   # 순서 섞어 AI 매칭 패턴 방지

    print(
        f"  해외 풀 구성: 아시아 {len(asia_picks)}건 "
        f"/ 영어권 {len(global_picks)}건 "
        f"(목표 {asia_quota}:{global_quota})"
    )
    return pool


# ──────────────────────────────────────────────────
# Cloud Storage 저장 (기존 그대로)
# ──────────────────────────────────────────────────

def upload_to_gcs(data, blob_name, content_type="application/json"):
    client = storage.Client()
    bucket = client.bucket(GCS_BUCKET)
    blob = bucket.blob(blob_name)

    if isinstance(data, (dict, list)):
        content = json.dumps(data, ensure_ascii=False, indent=2)
    else:
        content = str(data)

    blob.upload_from_string(content, content_type=content_type)
    return f"gs://{GCS_BUCKET}/{blob_name}"


def save_articles_to_gcs(generated_articles, timestamp):
    json_path = f"generated/{timestamp[:8]}/generated_articles_{timestamp}.json"
    json_url = upload_to_gcs(generated_articles, json_path)

    md_lines = [f"# 자동 생성 기사 모음 ({get_kst_now()})\n"]
    for i, art in enumerate(generated_articles, 1):
        md_lines.append(f"\n---\n\n## 📄 기사 {i} ({art.get('category', '미분류')})\n")
        md_lines.append(f"**짝짓기 사유**: {art.get('match_reason', '')}\n")
        md_lines.append(f"**원본 제목**:\n")
        for ttl in art.get('source_titles', []):
            md_lines.append(f"- {ttl}\n")
        v = art.get('validation', {})
        if v.get('passed'):
            md_lines.append(f"\n**검수**: ✅ 합격\n")
        else:
            md_lines.append(f"\n**검수**: ⚠️ {', '.join(v.get('issues', []))}\n")
        md_lines.append(f"\n**비용**: ${art.get('cost_usd', 0):.4f}\n")
        md_lines.append(f"\n---\n\n{art.get('body', '')}\n")

    md_content = "".join(md_lines)
    md_path = f"generated/{timestamp[:8]}/generated_articles_{timestamp}.md"
    md_url = upload_to_gcs(md_content, md_path, content_type="text/markdown")

    return json_url, md_url


def save_raw_articles_to_gcs(korea_articles, overseas_pool, timestamp):
    korea_path   = f"raw/{timestamp[:8]}/korea_articles_{timestamp}.json"
    overseas_path = f"raw/{timestamp[:8]}/overseas_articles_{timestamp}.json"
    upload_to_gcs(korea_articles, korea_path)
    upload_to_gcs(overseas_pool,  overseas_path)
    return korea_path, overseas_path


# ──────────────────────────────────────────────────
# Cloud Functions 진입점
# ──────────────────────────────────────────────────

@functions_framework.http
def run_pipeline(request):
    timestamp  = get_kst_timestamp()
    start_time = datetime.now(KST)

    print(f"\n{'='*60}")
    print(f"  🐷 한돈투데이 자동화 파이프라인 v3")
    print(f"  실행 시각: {get_kst_now()}")
    print(f"{'='*60}")

    # ── 중복 실행 방지: 최근 30분 내 실행 여부 확인 + 즉시 선점 ──
    _guard_run_id = None
    try:
        with db_manager.get_engine().begin() as _conn:
            recent = _conn.execute(text("""
                SELECT id, started_at FROM pipeline_runs
                WHERE started_at >= NOW() - INTERVAL '30 minutes'
                ORDER BY started_at DESC LIMIT 1
            """)).fetchone()
            if recent:
                print(f"\n[중복 방지] 최근 30분 내 실행 존재 (id={recent[0]}, {recent[1]}) — 종료")
                return (
                    '{"skipped": true, "reason": "duplicate run within 30 minutes"}',
                    200,
                    {"Content-Type": "application/json"},
                )
            # 즉시 선점 레코드 삽입 (실행 시작 표시)
            row = _conn.execute(text("""
                INSERT INTO pipeline_runs (started_at, korea_count, overseas_count,
                    generated_count, passed_count, total_cost_usd, success, errors)
                VALUES (NOW(), 0, 0, 0, 0, 0, false, '[]')
                RETURNING id
            """)).fetchone()
            _guard_run_id = row[0]
            print(f"\n[중복 방지] 선점 레코드 생성 (id={_guard_run_id})")
    except Exception as e:
        print(f"  ⚠️ 중복 체크 실패 (계속 진행): {e}")
    # ─────────────────────────────────────────────────────────

    stats = {
        "timestamp": timestamp,
        "started_at": get_kst_now(),
        "started_at_iso": start_time.isoformat(),
        "korea_count": 0,
        "overseas_count": 0,
        "generated_count": 0,
        "passed_count": 0,
        "total_cost_usd": 0.0,
        "errors": [],
        "gcs_urls": {},
        "db_stats": {
            "raw_inserted": 0,
            "raw_skipped": 0,
            "generated_saved": 0,
        },
    }

    url_to_id_map = {}

    try:
        # ── 0. 오전 6시 KST: 일일 시황 브리핑 ──────────
        now_kst = datetime.now(KST)
        if now_kst.hour == 6:
            try:
                today_start = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
                with db_manager.get_engine().connect() as _conn:
                    existing = _conn.execute(text("""
                        SELECT id FROM generated_articles
                        WHERE title LIKE '🐷 한돈투데이 모닝 브리핑%'
                          AND published_at >= :today
                        LIMIT 1
                    """), {"today": today_start.astimezone(timezone.utc)}).fetchone()
                if existing:
                    print(f"\n[브리핑] 오늘 브리핑 이미 존재 (id={existing[0]}) — 스킵")
                else:
                    print(f"\n[브리핑] 오전 6시 — 일일 시황 브리핑 생성")
                    briefing_result = daily_briefing.run_daily_briefing(
                        db_manager.get_engine()
                    )
                    if briefing_result["success"]:
                        stats["total_cost_usd"] += briefing_result["cost_usd"]
                    notifier.send_briefing_result(briefing_result)
            except Exception as e:
                print(f"[브리핑 에러] {e}")
                stats["errors"].append(f"브리핑 오류: {str(e)}")
                notifier.send_briefing_result({"success": False, "error": str(e), "cost_usd": 0})
        # ─────────────────────────────────────────────────

        # ── 1. 국내 크롤링 ────────────────────────────
        print("\n[1/5] 국내 뉴스 크롤링")
        try:
            korea_articles = korea_crawler.crawl_all(limit_per_site=3)
            stats["korea_count"] = len(korea_articles)
            print(f"  ✅ 국내 {len(korea_articles)}건")
        except Exception as e:
            err = f"국내 크롤링 오류: {e}"
            print(f"  ❌ {err}")
            stats["errors"].append(err)
            korea_articles = []

        # ── 1.5. 돈가 파싱 + DB 저장 ────────────────────
        try:
            dongga_price = korea_crawler.parse_dongga_from_articles(korea_articles)
            if dongga_price:
                korea_crawler.save_dongga_to_db(db_manager.get_engine(), dongga_price)
            else:
                print("  [돈가] 기사에서 돈가 파싱 실패")
        except Exception as e:
            print(f"  [돈가 저장 오류] {e}")

        # ── 2. 해외 수집 (v3: 아시아 + 영어권 분리 반환) ─
        print("\n[2/5] 해외 뉴스 수집 (아시아 + 영어권)")
        try:
            overseas_result  = overseas_collector.crawl_all()
            # 아시아 6:영어권 4 비중으로 풀 구성
            overseas_pool    = build_overseas_pool(overseas_result, OVERSEAS_POOL_SIZE)
            stats["overseas_count"] = len(overseas_pool)
            # 전체 수집량도 기록 (참고용)
            stats["overseas_asia_total"]   = len(overseas_result.get("asia", []))
            stats["overseas_global_total"] = len(overseas_result.get("global", []))
            print(f"  ✅ 해외 풀 {len(overseas_pool)}건 (아시아 수집 "
                  f"{stats['overseas_asia_total']}건 / "
                  f"영어권 수집 {stats['overseas_global_total']}건)")
        except Exception as e:
            err = f"해외 수집 오류: {e}"
            print(f"  ❌ {err}")
            stats["errors"].append(err)
            overseas_pool   = []
            overseas_result = {"asia": [], "global": []}

        # 원본 GCS 백업
        all_raw = korea_articles + overseas_pool
        if all_raw:
            try:
                save_raw_articles_to_gcs(korea_articles, overseas_pool, timestamp)
                print(f"  📁 원본 GCS 백업 완료")
            except Exception as e:
                print(f"  ⚠️ 원본 GCS 백업 실패: {e}")

        # ── 2.5. 원본 DB 저장 ─────────────────────────
        print("\n[2.5/5] 원본 기사 DB 저장")
        try:
            if korea_articles:
                kr = db_manager.upsert_raw_articles(korea_articles, "korea")
                stats["db_stats"]["raw_inserted"] += kr["inserted"]
                stats["db_stats"]["raw_skipped"]  += kr["skipped"]
                url_to_id_map.update(kr["url_to_id"])

            if overseas_pool:
                ov = db_manager.upsert_raw_articles(overseas_pool, "overseas")
                stats["db_stats"]["raw_inserted"] += ov["inserted"]
                stats["db_stats"]["raw_skipped"]  += ov["skipped"]
                url_to_id_map.update(ov["url_to_id"])

            print(f"  ✅ 원본 DB: 신규 {stats['db_stats']['raw_inserted']}건, "
                  f"기존 {stats['db_stats']['raw_skipped']}건")
        except Exception as e:
            err = f"원본 DB 저장 오류: {e}"
            print(f"  ❌ {err}")
            stats["errors"].append(err)

        if len(all_raw) < 2:
            err = "수집 기사 2건 미만, 종료"
            print(f"\n⚠️ {err}")
            stats["errors"].append(err)
            stats["success"] = False
            _finalize_stats(stats, start_time)
            _save_pipeline_run(stats)
            notifier.send_pipeline_result(stats, success=False)
            return _make_response(stats, 200)

        # ── 3. AI 기사 생성 ───────────────────────────
        # overseas_result의 asia/global을 article_generator에 그대로 넘겨
        # 내부에서 국내2쌍 + 아시아1쌍 + 영어권1쌍으로 분리 매칭
        all_for_generator = (
            korea_articles
            + overseas_result.get("asia", [])
            + overseas_result.get("global", [])
        )
        print(f"\n[3/5] AI 기사 생성 (국내 {len(korea_articles)}건 + "
              f"아시아 {len(overseas_result.get('asia',[]))}건 + "
              f"영어권 {len(overseas_result.get('global',[]))}건)")
        try:
            generated = article_generator.run_pipeline_from_data(
                all_for_generator,
                test_mode=False,
                max_pairs=4,
                recent_hours=24,
                save_files=False,
            )
            stats["generated_count"] = len(generated)
            stats["passed_count"]    = sum(
                1 for a in generated if a.get("validation", {}).get("passed")
            )
            stats["total_cost_usd"]  = sum(a.get("cost_usd", 0) for a in generated)
            print(f"  ✅ {len(generated)}건 생성, {stats['passed_count']}건 검수 통과")
        except Exception as e:
            err = f"기사 생성 오류: {e}\n{traceback.format_exc()[:500]}"
            print(f"  ❌ {err}")
            stats["errors"].append(err)
            generated = []

        # ── 4. GCS 저장 ───────────────────────────────
        print(f"\n[4/5] 결과 GCS 저장")
        if generated:
            try:
                json_url, md_url = save_articles_to_gcs(generated, timestamp)
                stats["gcs_urls"]["json"]     = json_url
                stats["gcs_urls"]["markdown"] = md_url
                print(f"  ✅ {json_url}")
            except Exception as e:
                err = f"GCS 저장 오류: {e}"
                print(f"  ❌ {err}")
                stats["errors"].append(err)

        # ── 5. DB 저장 + 알림 ─────────────────────────
        print(f"\n[5/5] DB 저장 + 알림")
        _finalize_stats(stats, start_time)
        pipeline_run_id = _save_pipeline_run(stats)

        if generated and pipeline_run_id:
            for art in generated:
                gen_id = db_manager.insert_generated_article(
                    art,
                    pipeline_run_id=pipeline_run_id,
                    url_to_id_map=url_to_id_map,
                    auto_publish=True,
                )
                if gen_id:
                    stats["db_stats"]["generated_saved"] += 1
            print(f"  ✅ 생성 기사 DB 저장: {stats['db_stats']['generated_saved']}건")

        notifier.send_pipeline_result(stats, success=stats["success"])

        try:
            db_manager.close_engine()
        except Exception:
            pass

        elapsed = stats.get("elapsed_seconds", 0)
        print(f"\n{'='*60}")
        print(f"  🎉 완료 ({elapsed:.1f}초) — "
              f"DB 저장 {stats['db_stats']['generated_saved']}건")
        print(f"{'='*60}\n")

        return _make_response(stats, 200)

    except Exception as e:
        err_msg = f"치명적 오류: {e}\n{traceback.format_exc()}"
        print(f"\n❌❌❌ {err_msg}")
        stats["errors"].append(err_msg)
        stats["success"] = False
        try:
            _finalize_stats(stats, start_time)
            _save_pipeline_run(stats)
            notifier.send_pipeline_result(stats, success=False)
        except Exception:
            pass
        return _make_response(stats, 500)


def _finalize_stats(stats, start_time):
    elapsed = (datetime.now(KST) - start_time).total_seconds()
    stats["elapsed_seconds"]  = round(elapsed, 1)
    stats["finished_at"]      = get_kst_now()
    stats["finished_at_iso"]  = datetime.now(KST).isoformat()
    stats["success"] = (
        stats["generated_count"] > 0
        and not any("오류" in e for e in stats["errors"])
    )


def _save_pipeline_run(stats):
    try:
        return db_manager.insert_pipeline_run(stats)
    except Exception as e:
        print(f"  ⚠️ pipeline_runs 저장 실패: {e}")
        return None


def _make_response(stats, status_code):
    return (
        json.dumps(stats, ensure_ascii=False, indent=2, default=str),
        status_code,
        {"Content-Type": "application/json; charset=utf-8"},
    )

# 만평 파이프라인 진입점 노출
from weekly_manhwa import run_manhwa_pipeline

# 미래 만평 파이프라인 진입점 노출
from weekly_future import run_future_pipeline

# 목요일 웹툰 파이프라인 진입점
from weekly_webtoon_thu import run_webtoon_thu_pipeline


# ── N년 전 오늘 회고 파이프라인 ──────────────────
from review_collector import run_2016review_pipeline, run_2006review_pipeline
