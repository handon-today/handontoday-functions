"""
================================================================
  Cloud SQL (PostgreSQL) 연결 및 저장 관리 모듈
  db_manager.py
================================================================

[v2 변경사항 - 2026.05.11]
  - 트랜잭션 격리: 각 기사 INSERT를 독립 트랜잭션으로 처리
  - 상세 에러 로그: 어떤 기사가 실패했는지 추적 가능
  - 빈 body/title 등 데이터 검증 추가

[역할]
  Cloud Functions에서 Cloud SQL에 접속해서 데이터 저장
  
  1. raw_articles    - 크롤링 원본 (중복 체크 포함)
  2. generated_articles - AI 생성 기사
  3. article_sources  - 생성기사 ↔ 원본기사 연결
  4. pipeline_runs   - 실행 이력

[연결 방식]
  Cloud SQL Python Connector 사용
  - VPC 설정 불필요
  - 자동 SSL/TLS 암호화

[환경변수]
  CLOUD_SQL_CONNECTION_NAME  - 인스턴스 연결 이름
  CLOUD_SQL_PASSWORD         - Secret Manager에서 주입됨
"""

import os
import json
from datetime import datetime
from contextlib import contextmanager

import sqlalchemy
from google.cloud.sql.connector import Connector


# ──────────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────────

CLOUD_SQL_CONNECTION_NAME = os.getenv(
    "CLOUD_SQL_CONNECTION_NAME",
    "handontoday:asia-northeast3:handontoday-db"
)
DB_USER = "postgres"
DB_NAME = "handontoday_db"
DB_PASSWORD = os.getenv("CLOUD_SQL_PASSWORD", "")

# 전역 connector + engine (재사용)
_connector = None
_engine = None


def _get_connection():
    """Cloud SQL Connector를 통해 DB 연결 생성"""
    global _connector
    if _connector is None:
        _connector = Connector()
    
    return _connector.connect(
        CLOUD_SQL_CONNECTION_NAME,
        "pg8000",
        user=DB_USER,
        password=DB_PASSWORD,
        db=DB_NAME,
    )


def get_engine():
    """SQLAlchemy 엔진 반환 (재사용)"""
    global _engine
    if _engine is None:
        if not DB_PASSWORD:
            raise ValueError(
                "CLOUD_SQL_PASSWORD 환경변수가 설정되지 않았습니다."
            )
        
        _engine = sqlalchemy.create_engine(
            "postgresql+pg8000://",
            creator=_get_connection,
            pool_size=2,
            max_overflow=2,
            pool_timeout=30,
            pool_recycle=1800,
        )
    return _engine


@contextmanager
def get_db_connection():
    """컨텍스트 매니저: with 문으로 안전하게 사용"""
    engine = get_engine()
    conn = engine.connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def close_engine():
    """함수 종료 시 정리"""
    global _engine, _connector
    if _engine is not None:
        _engine.dispose()
        _engine = None
    if _connector is not None:
        _connector.close()
        _connector = None


# ──────────────────────────────────────────────────
# 1. pipeline_runs (실행 이력)
# ──────────────────────────────────────────────────

def insert_pipeline_run(stats):
    """파이프라인 실행 이력 저장"""
    sql = sqlalchemy.text("""
        INSERT INTO pipeline_runs (
            started_at, korea_count, overseas_count,
            generated_count, passed_count, total_cost_usd,
            success, errors, gcs_json_path, gcs_markdown_path,
            finished_at, elapsed_seconds
        ) VALUES (
            :started_at, :korea_count, :overseas_count,
            :generated_count, :passed_count, :total_cost_usd,
            :success, :errors, :gcs_json_path, :gcs_markdown_path,
            :finished_at, :elapsed_seconds
        )
        RETURNING id
    """)
    
    params = {
        "started_at": datetime.fromisoformat(stats["started_at_iso"]),
        "finished_at": datetime.fromisoformat(stats["finished_at_iso"]) if stats.get("finished_at_iso") else None,
        "elapsed_seconds": stats.get("elapsed_seconds"),
        "korea_count": stats.get("korea_count", 0),
        "overseas_count": stats.get("overseas_count", 0),
        "generated_count": stats.get("generated_count", 0),
        "passed_count": stats.get("passed_count", 0),
        "total_cost_usd": stats.get("total_cost_usd", 0),
        "success": stats.get("success", True),
        "errors": json.dumps(stats.get("errors", []), ensure_ascii=False) if stats.get("errors") else None,
        "gcs_json_path": stats.get("gcs_urls", {}).get("json"),
        "gcs_markdown_path": stats.get("gcs_urls", {}).get("markdown"),
    }
    
    with get_db_connection() as conn:
        result = conn.execute(sql, params)
        run_id = result.scalar()
        print(f"  📊 pipeline_runs 저장: id={run_id}")
        return run_id


# ──────────────────────────────────────────────────
# 2. raw_articles (원본 기사) - v2: 트랜잭션 격리
# ──────────────────────────────────────────────────

def upsert_raw_articles(articles, source_type):
    """
    원본 기사를 DB에 저장 (URL 중복 체크).
    
    v2: 각 기사별로 독립 트랜잭션 사용.
    한 기사가 실패해도 나머지는 계속 진행.
    
    Args:
        articles: 기사 리스트 (dict 형태)
        source_type: 'korea' 또는 'overseas'
    
    Returns:
        {
            'inserted': int,
            'skipped': int,
            'failed': int,
            'url_to_id': dict
        }
    """
    if not articles:
        return {"inserted": 0, "skipped": 0, "failed": 0, "url_to_id": {}}
    
    insert_sql = sqlalchemy.text("""
        INSERT INTO raw_articles (
            url, source, source_type, article_type, title, body,
            body_length, pub_date, scraped_at
        ) VALUES (
            :url, :source, :source_type, :article_type, :title, :body,
            :body_length, :pub_date, :scraped_at
        )
        ON CONFLICT (url) DO NOTHING
        RETURNING id, url
    """)
    
    lookup_sql = sqlalchemy.text(
        "SELECT id FROM raw_articles WHERE url = :url"
    )
    
    inserted = 0
    skipped = 0
    failed = 0
    url_to_id = {}
    engine = get_engine()
    
    for article in articles:
        url = article.get("url", "")
        if not url:
            failed += 1
            print(f"  ⚠️ URL 없는 기사 건너뜀")
            continue
        
        # 데이터 검증 + 정제
        title = (article.get("title") or "").strip()
        body = (article.get("body") or "").strip()
        
        if not title or not body:
            failed += 1
            print(f"  ⚠️ 제목/본문 비어있음: {url[:60]}")
            continue
        
        params = {
            "url": url,
            "source": (article.get("source") or "").strip()[:100],
            "source_type": source_type,
            "article_type": (article.get("type") or "full_body").strip()[:50],
            "title": title[:500],
            "body": body,
            "body_length": len(body),
            "pub_date": _parse_date(article.get("pub_date")),
            "scraped_at": _parse_date(article.get("scraped_at")) or datetime.now(),
        }
        
        # 각 기사마다 독립 트랜잭션 (engine.begin = auto-commit/rollback)
        try:
            with engine.begin() as conn:
                result = conn.execute(insert_sql, params)
                row = result.fetchone()
                
                if row:
                    # 새로 INSERT 됨
                    url_to_id[row[1]] = row[0]
                    inserted += 1
                else:
                    # 이미 있음 → ID만 조회
                    lookup_result = conn.execute(lookup_sql, {"url": url})
                    existing = lookup_result.fetchone()
                    if existing:
                        url_to_id[url] = existing[0]
                        skipped += 1
        except Exception as e:
            failed += 1
            print(f"  ⚠️ 저장 실패 [{source_type}] {url[:60]}")
            print(f"     원인: {str(e)[:200]}")
    
    print(f"  📰 raw_articles ({source_type}): "
          f"신규 {inserted}건, 기존 {skipped}건, 실패 {failed}건")
    
    return {
        "inserted": inserted,
        "skipped": skipped,
        "failed": failed,
        "url_to_id": url_to_id,
    }


# ──────────────────────────────────────────────────
# 3. generated_articles + article_sources
# ──────────────────────────────────────────────────

def insert_generated_article(article, pipeline_run_id, url_to_id_map, auto_publish=True):
    """
    생성된 기사를 DB에 저장 + 원본 연결.
    각 기사가 독립 트랜잭션.
    [Step 10] 새 컬럼 추가: deck, slug, body_markdown, body_html, tags, read_minutes
    """
    validation_passed = article.get("validation", {}).get("passed", False)
    if auto_publish and validation_passed:
        publish_status = "published"
        published_at = datetime.now()
    else:
        publish_status = "draft"
        published_at = None
    
    insert_sql = sqlalchemy.text("""
        INSERT INTO generated_articles (
            title, deck, slug,
            body, body_markdown, body_html,
            category, tags,
            match_reason, source_titles, source_urls,
            validation_passed, validation_issues,
            input_tokens, output_tokens, cost_usd,
            publish_status, published_at,
            is_featured, is_hot,
            read_minutes, view_count,
            pipeline_run_id, generated_at,
            created_at, updated_at
        ) VALUES (
            :title, :deck, :slug,
            :body, :body_markdown, :body_html,
            :category, :tags,
            :match_reason, :source_titles, :source_urls,
            :validation_passed, :validation_issues,
            :input_tokens, :output_tokens, :cost_usd,
            :publish_status, :published_at,
            :is_featured, :is_hot,
            :read_minutes, :view_count,
            :pipeline_run_id, :generated_at,
            :created_at, :updated_at
        )
        RETURNING id
    """)
    
    link_sql = sqlalchemy.text("""
        INSERT INTO article_sources (generated_id, raw_id, position)
        VALUES (:generated_id, :raw_id, :position)
        ON CONFLICT DO NOTHING
    """)
    
    update_count_sql = sqlalchemy.text("""
        UPDATE raw_articles SET used_count = used_count + 1
        WHERE id = :raw_id
    """)
    
    title = (article.get("title") or "제목 없음").strip()[:500]
    body = (article.get("body") or article.get("body_markdown") or "").strip()
    body_markdown = (article.get("body_markdown") or body).strip()
    
    if not body_markdown:
        print(f"  ⚠️ 본문 없는 생성기사 건너뜀: {title[:50]}")
        return None
    
    # Step 10: 새 컬럼 값 가져오기
    deck = article.get("deck")
    slug = article.get("slug") or f"article-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    body_html = article.get("body_html")
    tags = json.dumps(article.get("tags", []), ensure_ascii=False)
    source_titles = json.dumps(article.get("source_titles", []), ensure_ascii=False)
    source_urls_json = json.dumps(article.get("source_urls", []), ensure_ascii=False)
    read_minutes = article.get("read_minutes", 0)
    
    now = datetime.now()
    generated_at = _parse_date(article.get("generated_at")) or now
    
    params = {
        "title": title,
        "deck": deck,
        "slug": slug,
        "body": body,  # 레거시
        "body_markdown": body_markdown,
        "body_html": body_html,
        "category": article.get("category", "국내"),
        "tags": tags,
        "match_reason": article.get("match_reason"),
        "source_titles": source_titles,
        "source_urls": source_urls_json,
        "validation_passed": validation_passed,
        "validation_issues": json.dumps(
            article.get("validation", {}).get("issues", []),
            ensure_ascii=False
        ),
        "input_tokens": article.get("tokens", {}).get("input"),
        "output_tokens": article.get("tokens", {}).get("output"),
        "cost_usd": article.get("cost_usd", 0),
        "publish_status": publish_status,
        "published_at": published_at,
        "is_featured": article.get("is_featured", False),
        "is_hot": article.get("is_hot", False),
        "read_minutes": read_minutes,
        "view_count": 0,
        "pipeline_run_id": pipeline_run_id,
        "generated_at": generated_at,
        "created_at": now,
        "updated_at": now,
    }
    
    engine = get_engine()
    
    try:
        with engine.begin() as conn:
            result = conn.execute(insert_sql, params)
            generated_id = result.scalar()
            
            # 원본 연결
            source_urls = article.get("source_urls", [])
            for position, url in enumerate(source_urls, 1):
                raw_id = url_to_id_map.get(url)
                if raw_id is None:
                    print(f"  ⚠️ 원본 못 찾음: {url[:60]}")
                    continue
                
                conn.execute(link_sql, {
                    "generated_id": generated_id,
                    "raw_id": raw_id,
                    "position": position,
                })
                conn.execute(update_count_sql, {"raw_id": raw_id})
            
            print(f"  ✍️  generated_articles 저장: id={generated_id} slug={slug[:40]} ({publish_status})")
            return generated_id
    except Exception as e:
        print(f"  ❌ generated_articles 저장 실패: {title[:50]}")
        print(f"     원인: {str(e)[:200]}")
        return None


def _parse_date(date_str):
    """ISO 형식 또는 다양한 날짜 문자열을 datetime으로"""
    if not date_str:
        return None
    if isinstance(date_str, datetime):
        return date_str
    try:
        return datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None


# ──────────────────────────────────────────────────
# 헬스체크
# ──────────────────────────────────────────────────

def healthcheck():
    """DB 연결 정상 작동 확인"""
    try:
        with get_db_connection() as conn:
            result = conn.execute(sqlalchemy.text("SELECT 1"))
            assert result.scalar() == 1
            
            counts = {}
            for table in ["pipeline_runs", "raw_articles",
                          "generated_articles", "article_sources"]:
                r = conn.execute(sqlalchemy.text(f"SELECT COUNT(*) FROM {table}"))
                counts[table] = r.scalar()
            
            return {"status": "ok", "counts": counts}
    except Exception as e:
        return {"status": "error", "message": str(e)}
