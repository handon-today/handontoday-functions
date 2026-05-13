"""
================================================================
  Cloud Functions 진입점 (Phase 1B - DB 통합 버전)
  main.py
================================================================

[v2 변경사항 - Phase 1B]
  - Cloud SQL (PostgreSQL) 연동 추가
  - 원본 기사 DB 저장 (URL 기반 중복 체크)
  - 생성 기사 DB 저장 (자동 발행 + 사후 모니터링 정책)
  - 파이프라인 실행 이력 기록
  - GCS 백업도 그대로 유지 (이중 안전망)

[역할]
  Cloud Scheduler에서 HTTP 요청 → 이 함수가 실행됨
  1. 국내 뉴스 크롤링
  2. 해외 뉴스 수집
  3. AI 기사 생성
  4. DB 저장 (Cloud SQL) + GCS 저장 (이중 백업)
  5. Slack 알림

[배포 명령]
  gcloud functions deploy handon-news-pipeline \\
    --gen2 \\
    --runtime python311 \\
    --trigger-http \\
    --entry-point=run_pipeline \\
    --memory=512MB \\
    --timeout=540s \\
    --region=asia-northeast3 \\
    --set-secrets=OPENROUTER_API_KEY=openrouter-api-key:latest,SLACK_WEBHOOK_URL=slack-webhook-url:latest,CLOUD_SQL_PASSWORD=cloud-sql-password:latest \\
    --set-env-vars=GCS_BUCKET=handontoday-articles,CLOUD_SQL_CONNECTION_NAME=handontoday:asia-northeast3:handontoday-db
"""

import os
import json
import traceback
from datetime import datetime, timezone, timedelta

import functions_framework
from google.cloud import storage

# 기존 모듈
import korea_crawler
import overseas_collector
import article_generator
import notifier

# 신규 모듈 (Phase 1B)
import db_manager


# ──────────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────────

GCS_BUCKET = os.getenv("GCS_BUCKET", "handontoday-articles")
KST = timezone(timedelta(hours=9))


def get_kst_timestamp():
    """KST 타임스탬프 (파일명용: 20260510_1430)"""
    return datetime.now(KST).strftime("%Y%m%d_%H%M")


def get_kst_now():
    """KST 현재 시각 (사람이 읽기 좋은 형식)"""
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")


# ──────────────────────────────────────────────────
# Cloud Storage 저장 (기존 그대로 유지)
# ──────────────────────────────────────────────────

def upload_to_gcs(data, blob_name, content_type="application/json"):
    """Cloud Storage에 데이터 업로드"""
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
    """생성된 기사를 GCS에 저장 (JSON + Markdown)"""
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


def save_raw_articles_to_gcs(korea_articles, overseas_articles, timestamp):
    """크롤링 원본 데이터도 GCS에 백업"""
    korea_path = f"raw/{timestamp[:8]}/korea_articles_{timestamp}.json"
    overseas_path = f"raw/{timestamp[:8]}/overseas_articles_{timestamp}.json"
    
    upload_to_gcs(korea_articles, korea_path)
    upload_to_gcs(overseas_articles, overseas_path)
    
    return korea_path, overseas_path


# ──────────────────────────────────────────────────
# Cloud Functions 진입점
# ──────────────────────────────────────────────────

@functions_framework.http
def run_pipeline(request):
    """
    Cloud Scheduler에서 HTTP 트리거로 호출됨.
    파이프라인 전체를 실행 + DB 저장 + GCS 백업.
    """
    timestamp = get_kst_timestamp()
    start_time = datetime.now(KST)
    
    print(f"\n{'='*60}")
    print(f"  🐷 한돈투데이 자동화 파이프라인 시작 (Phase 1B)")
    print(f"  실행 시각: {get_kst_now()}")
    print(f"{'='*60}")
    
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
        # Phase 1B 추가
        "db_stats": {
            "raw_inserted": 0,
            "raw_skipped": 0,
            "generated_saved": 0,
        },
    }
    
    # URL → raw_articles.id 매핑 (생성 기사 저장 시 사용)
    url_to_id_map = {}
    
    try:
        # ──────────────────────────────────────────────────
        # 1단계: 국내 뉴스 크롤링
        # ──────────────────────────────────────────────────
        print("\n[1/5] 국내 뉴스 크롤링 시작")
        try:
            korea_articles = korea_crawler.crawl_all(limit_per_site=3)
            stats["korea_count"] = len(korea_articles)
            print(f"  ✅ 국내 {len(korea_articles)}건 수집")
        except Exception as e:
            err = f"국내 크롤링 오류: {str(e)}"
            print(f"  ❌ {err}")
            stats["errors"].append(err)
            korea_articles = []
        
        # ──────────────────────────────────────────────────
        # 2단계: 해외 뉴스 수집
        # ──────────────────────────────────────────────────
        print("\n[2/5] 해외 뉴스 수집 시작")
        try:
            overseas_articles = overseas_collector.crawl_all()
            stats["overseas_count"] = len(overseas_articles)
            print(f"  ✅ 해외 {len(overseas_articles)}건 수집")
        except Exception as e:
            err = f"해외 수집 오류: {str(e)}"
            print(f"  ❌ {err}")
            stats["errors"].append(err)
            overseas_articles = []
        
        # 원본 GCS 백업 (기존)
        all_raw = korea_articles + overseas_articles
        if all_raw:
            try:
                save_raw_articles_to_gcs(korea_articles, overseas_articles, timestamp)
                print(f"  📁 원본 데이터 GCS 백업 완료")
            except Exception as e:
                print(f"  ⚠️ 원본 GCS 백업 실패: {e}")
        
        # ──────────────────────────────────────────────────
        # 2.5단계: DB에 원본 기사 저장 (Phase 1B 신규)
        # ──────────────────────────────────────────────────
        print("\n[2.5/5] 원본 기사 DB 저장")
        try:
            if korea_articles:
                kr_result = db_manager.upsert_raw_articles(korea_articles, "korea")
                stats["db_stats"]["raw_inserted"] += kr_result["inserted"]
                stats["db_stats"]["raw_skipped"] += kr_result["skipped"]
                url_to_id_map.update(kr_result["url_to_id"])
            
            if overseas_articles:
                ov_result = db_manager.upsert_raw_articles(overseas_articles, "overseas")
                stats["db_stats"]["raw_inserted"] += ov_result["inserted"]
                stats["db_stats"]["raw_skipped"] += ov_result["skipped"]
                url_to_id_map.update(ov_result["url_to_id"])
            
            print(f"  ✅ 원본 DB 저장: 신규 {stats['db_stats']['raw_inserted']}건, "
                  f"기존 {stats['db_stats']['raw_skipped']}건")
        except Exception as e:
            err = f"원본 DB 저장 오류: {str(e)}"
            print(f"  ❌ {err}")
            stats["errors"].append(err)
            # 계속 진행 (GCS에는 저장됨)
        
        # 수집 데이터 부족하면 종료
        if len(all_raw) < 2:
            err = "수집된 기사가 2건 미만, 파이프라인 종료"
            print(f"\n⚠️ {err}")
            stats["errors"].append(err)
            stats["finished_at"] = get_kst_now()
            stats["finished_at_iso"] = datetime.now(KST).isoformat()
            stats["elapsed_seconds"] = (datetime.now(KST) - start_time).total_seconds()
            stats["success"] = False
            _save_pipeline_run(stats)
            notifier.send_pipeline_result(stats, success=False)
            return _make_response(stats, 200)
        
        # ──────────────────────────────────────────────────
        # 3단계: AI 기사 생성
        # ──────────────────────────────────────────────────
        print(f"\n[3/5] AI 기사 생성 시작 (입력 {len(all_raw)}건)")
        try:
            generated = article_generator.run_pipeline_from_data(
                all_raw,
                test_mode=False,
                max_pairs=4,
                recent_hours=24,
                save_files=False,
            )
            stats["generated_count"] = len(generated)
            stats["passed_count"] = sum(
                1 for a in generated if a.get('validation', {}).get('passed')
            )
            stats["total_cost_usd"] = sum(a.get('cost_usd', 0) for a in generated)
            print(f"  ✅ {len(generated)}건 생성, {stats['passed_count']}건 검수 통과")
        except Exception as e:
            err = f"기사 생성 오류: {str(e)}\n{traceback.format_exc()[:500]}"
            print(f"  ❌ {err}")
            stats["errors"].append(err)
            generated = []
        
        # ──────────────────────────────────────────────────
        # 4단계: GCS 저장 (이중 백업)
        # ──────────────────────────────────────────────────
        print(f"\n[4/5] 결과 GCS 저장")
        if generated:
            try:
                json_url, md_url = save_articles_to_gcs(generated, timestamp)
                stats["gcs_urls"]["json"] = json_url
                stats["gcs_urls"]["markdown"] = md_url
                print(f"  ✅ GCS 저장: {json_url}")
            except Exception as e:
                err = f"GCS 저장 오류: {str(e)}"
                print(f"  ❌ {err}")
                stats["errors"].append(err)
        
        # 시간 계산 (DB 저장 전에 미리)
        elapsed = (datetime.now(KST) - start_time).total_seconds()
        stats["elapsed_seconds"] = round(elapsed, 1)
        stats["finished_at"] = get_kst_now()
        stats["finished_at_iso"] = datetime.now(KST).isoformat()
        stats["success"] = stats["generated_count"] > 0 and not any(
            "오류" in e for e in stats["errors"]
        )
        
        # ──────────────────────────────────────────────────
        # 5단계: DB에 pipeline_runs + generated_articles 저장
        # ──────────────────────────────────────────────────
        print(f"\n[5/5] DB 저장 + 알림")
        pipeline_run_id = _save_pipeline_run(stats)
        
        if generated and pipeline_run_id:
            print(f"  ✍️  생성 기사 DB 저장 시작...")
            for art in generated:
                gen_id = db_manager.insert_generated_article(
                    art,
                    pipeline_run_id=pipeline_run_id,
                    url_to_id_map=url_to_id_map,
                    auto_publish=True,  # 자동 발행 정책
                )
                if gen_id:
                    stats["db_stats"]["generated_saved"] += 1
            print(f"  ✅ 생성 기사 DB 저장: {stats['db_stats']['generated_saved']}건")
        
        # Slack 알림
        notifier.send_pipeline_result(stats, success=stats["success"])
        
        # 연결 정리
        try:
            db_manager.close_engine()
        except Exception:
            pass
        
        print(f"\n{'='*60}")
        print(f"  🎉 파이프라인 완료 ({elapsed:.1f}초)")
        print(f"  └ 원본 신규 {stats['db_stats']['raw_inserted']}건, "
              f"생성 {stats['db_stats']['generated_saved']}건 DB 저장")
        print(f"{'='*60}\n")
        
        return _make_response(stats, 200)
    
    except Exception as e:
        # 최후의 안전망
        err_msg = f"치명적 오류: {str(e)}\n{traceback.format_exc()}"
        print(f"\n❌❌❌ {err_msg}")
        stats["errors"].append(err_msg)
        stats["success"] = False
        try:
            stats["finished_at"] = get_kst_now()
            stats["finished_at_iso"] = datetime.now(KST).isoformat()
            stats["elapsed_seconds"] = (datetime.now(KST) - start_time).total_seconds()
            _save_pipeline_run(stats)
            notifier.send_pipeline_result(stats, success=False)
        except Exception:
            pass
        return _make_response(stats, 500)


def _save_pipeline_run(stats):
    """pipeline_runs 테이블에 저장 (오류 시 None 반환)"""
    try:
        run_id = db_manager.insert_pipeline_run(stats)
        return run_id
    except Exception as e:
        print(f"  ⚠️ pipeline_runs 저장 실패: {e}")
        return None


def _make_response(stats, status_code):
    """HTTP 응답 생성"""
    return (
        json.dumps(stats, ensure_ascii=False, indent=2, default=str),
        status_code,
        {"Content-Type": "application/json; charset=utf-8"},
    )
