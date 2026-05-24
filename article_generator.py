"""
================================================================
  3단계: 기사 생성 모듈 (OpenRouter + Gemini 2.5 Flash Lite)
  article_generator.py
================================================================

[변경 이력]
  v1~v3 (2026-04 ~): Claude Sonnet 4.5 사용
  v4 (2026-05): 비용 절감 목적으로 Gemini 2.5 Flash Lite로 전환
                - 검증 결과: 사실 정확도 100%, 품질 차이 없음
                - 비용: 약 1/30 수준 (건당 ~1원)
                - OpenRouter API는 그대로 사용 (모델만 교체)

[역할]
  1. 1·2단계에서 수집한 본문을 입력받음
  2. AI에게 보내 주제 매칭 → 짝짓기 결정 (옵션 B)
  3. 각 짝마다 AI API 호출 → 새 한국어 기사 생성
  4. 자동 검수 → 실패 시 1번 재생성
  5. 결과 JSON + 마크다운으로 저장

[API]
  - OpenRouter (https://openrouter.ai)
  - 모델: google/gemini-2.5-flash-lite

[환경변수]
  OPENROUTER_API_KEY="sk-or-v1-..."

[사용 방법]
  # 전체 실행
  python article_generator.py korea_articles_*.json overseas_articles_*.json

  # 테스트 (1쌍만 생성)
  python article_generator.py korea_articles_*.json --test

  # 짝짓기 개수 제한
  python article_generator.py korea_articles_*.json --max-pairs 2

[Windows 환경변수 설정]
  PowerShell:  $env:OPENROUTER_API_KEY="sk-or-v1-..."
  CMD:         set OPENROUTER_API_KEY=sk-or-v1-...
"""

import os
import sys
import json
import re
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from unsplash_helper import get_image_for_article


# ──────────────────────────────────────────────────
# OpenRouter API 설정
# ──────────────────────────────────────────────────

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "google/gemini-2.5-flash-lite"
MAX_TOKENS_MATCHING = 2000     # 짝짓기용
MAX_TOKENS_ARTICLE = 3000      # 기사 생성용
MAX_BODY_LENGTH = 2500         # 본문 자르기 (결정사항)

APP_NAME = "Handon Today"
APP_URL = "https://handontoday.com"


def call_openrouter_api(system_prompt, user_message, max_tokens=3000, retry=1):
    """OpenRouter API 호출 (재시도 포함)"""
    if not OPENROUTER_API_KEY:
        raise ValueError(
            "OPENROUTER_API_KEY 환경변수가 설정되지 않았어요.\n"
            "PowerShell: $env:OPENROUTER_API_KEY=\"sk-or-v1-...\"\n"
            "CMD:        set OPENROUTER_API_KEY=sk-or-v1-..."
        )
    
    payload = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    }
    
    data = json.dumps(payload).encode("utf-8")
    
    last_error = None
    for attempt in range(retry + 1):
        req = urllib.request.Request(
            OPENROUTER_API_URL,
            data=data,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": APP_URL,
                "X-Title": APP_NAME,
            },
            method="POST",
        )
        
        try:
            with urllib.request.urlopen(req, timeout=90) as res:
                result = json.loads(res.read().decode("utf-8"))
                msg = result["choices"][0]["message"]
                usage = result.get("usage", {})
                return {
                    "text": msg["content"],
                    "input_tokens": usage.get("prompt_tokens", 0),
                    "output_tokens": usage.get("completion_tokens", 0),
                }
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")
            last_error = f"HTTP {e.code}: {body[:300]}"
            if attempt < retry:
                print(f"  ⚠️ API 오류, 재시도 {attempt+1}/{retry}: {last_error[:100]}")
                continue
        except Exception as e:
            last_error = str(e)
            if attempt < retry:
                print(f"  ⚠️ 오류, 재시도 {attempt+1}/{retry}: {last_error[:100]}")
                continue
    
    raise RuntimeError(f"API 호출 실패: {last_error}")


def calculate_cost(input_tokens, output_tokens):
    """Gemini 2.5 Flash Lite 단가 (USD)
    입력: $0.10/M, 출력: $0.40/M"""
    return input_tokens / 1_000_000 * 0.10 + output_tokens / 1_000_000 * 0.40


# ──────────────────────────────────────────────────
# 발행일 필터 (24시간 이내 기사만)
# ──────────────────────────────────────────────────

def parse_date_safe(date_str):
    """다양한 형식의 날짜를 datetime으로 파싱"""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        pass
    try:
        return parsedate_to_datetime(date_str)
    except (TypeError, ValueError):
        pass
    return None


def filter_recent_articles(articles, hours=24):
    """최근 N시간 이내 기사만 필터링"""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    
    filtered = []
    for a in articles:
        pub = parse_date_safe(a.get("pub_date") or "")
        scraped = parse_date_safe(a.get("scraped_at") or "")
        
        if not pub and not scraped:
            filtered.append(a)
            continue
        
        ref_date = pub or scraped
        if ref_date.tzinfo is None:
            ref_date = ref_date.replace(tzinfo=timezone.utc)
        
        if ref_date >= cutoff:
            filtered.append(a)
    
    return filtered


# ──────────────────────────────────────────────────
# 1단계: 주제 매칭
# ──────────────────────────────────────────────────

MATCHING_SYSTEM_PROMPT = """당신은 양돈 뉴스 편집장입니다.
여러 기사 목록을 받으면, 비슷한 주제끼리 2개씩 짝지어 줍니다.

[짝짓기 규칙]
- 같은 사건/주제를 다루거나, 한 기사가 다른 기사의 배경/맥락이 되면 좋은 짝
- 예: "한국 ASF 발생" + "스페인 ASF 동향" → 좋은 짝
- 예: "돈가 상승" + "수입육 증가" → 좋은 짝 (시장 흐름)
- 예: "탄소배출 연구" + "사료 영양 연구" → 어색한 짝 (다른 주제)

[출력 형식 - JSON만 출력]
{
  "pairs": [
    {"id_a": 1, "id_b": 5, "reason": "ASF 관련 국내외 동향"},
    {"id_a": 2, "id_b": 8, "reason": "정책 동향"}
  ],
  "unmatched": [3, 7]
}

[주의]
- 어색한 짝짓기보다 unmatched에 두는 게 나음
- 같은 id를 두 짝에 사용 금지
- JSON 외 설명·코드블록 금지"""


def match_articles_with_ai(articles, max_pairs=8):
    """주제 매칭 요청"""
    if len(articles) < 2:
        return []
    
    summary = "다음은 양돈 뉴스 기사 목록입니다. 비슷한 주제끼리 짝지어 주세요.\n\n"
    for i, a in enumerate(articles, 1):
        body_preview = (a.get("body") or "")[:200].replace("\n", " ")
        type_label = {
            "full_body": "전문",
            "rss_summary": "요약",
            "headline": "헤드라인",
        }.get(a.get("type", ""), "전문")
        summary += f"[{i}] ({type_label}) {a['source']} | {a['title']}\n"
        summary += f"     {body_preview}...\n\n"
    
    summary += f"\n최대 {max_pairs}쌍까지 만들어 주세요. JSON만 출력하세요."
    
    print(f"\n[주제 매칭] OpenRouter 호출 중... (후보 {len(articles)}건)")
    response = call_openrouter_api(
        MATCHING_SYSTEM_PROMPT, summary,
        max_tokens=MAX_TOKENS_MATCHING, retry=1,
    )
    
    cost = calculate_cost(response["input_tokens"], response["output_tokens"])
    print(f"  토큰: {response['input_tokens']} / {response['output_tokens']}")
    print(f"  비용: ${cost:.4f} (≈{cost*1400:.1f}원)")
    
    text = response["text"].strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    
    try:
        result = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"  ❌ JSON 파싱 실패: {e}")
        print(f"  응답: {text[:300]}")
        return []
    
    pairs = []
    used_ids = set()
    for p in result.get("pairs", []):
        try:
            id_a = int(p["id_a"]) - 1
            id_b = int(p["id_b"]) - 1
            if id_a in used_ids or id_b in used_ids:
                continue
            if 0 <= id_a < len(articles) and 0 <= id_b < len(articles) and id_a != id_b:
                pairs.append({
                    "article_a": articles[id_a],
                    "article_b": articles[id_b],
                    "reason": p.get("reason", ""),
                })
                used_ids.add(id_a)
                used_ids.add(id_b)
        except (KeyError, ValueError, TypeError):
            continue
    
    print(f"  → {len(pairs)}쌍 매칭 완료")
    return pairs


# ──────────────────────────────────────────────────
# 2단계: 기사 생성 시스템 프롬프트
# ──────────────────────────────────────────────────

ARTICLE_SYSTEM_PROMPT_KOREA = """당신은 양돈 전문 미디어 '한돈투데이(Handon Today)'의 수석 기자입니다.
아래 규칙을 반드시 지켜 새 한국어 블로그형 기사를 작성하세요.

[★최우선★ 사실 정확성 규칙 - 위반 시 기사 폐기]
- **원문에 명시된 사실만 사용** — 수치, 날짜, 통계, 기관명, 인물명, 발언 등
- **원문에 없는 정보 추가 절대 금지** — 추측, 가정, 일반론, 배경 지식 추가 금지
- 모르는 정보는 **언급하지 말 것** (빈자리를 추측으로 채우지 말 것)
- 추측성 표현 금지: "~로 보입니다", "~할 것으로 예상됩니다", "~가 우려됩니다" 등도 원문에 근거가 있을 때만 사용
- 수치는 원문 그대로 (반올림·요약·변환 금지)
- 인용된 발언은 원문 발언자가 실제로 한 말만 사용

[자기 검증 체크리스트 - 작성 후 반드시 확인]
□ 모든 수치가 원문에 있는가?
□ 모든 기관명·인물명이 원문에 있는가?
□ 인용·발언이 원문에 실제로 있는가?
□ 원문에 없는 배경 설명을 추가하지 않았는가?
하나라도 해당 안 되면 해당 부분 삭제 후 재작성

[고유명사·약자 처리 - 매우 중요]
- 약자(略字)는 **원문에 있는 것만** 사용
- 원문이 "Proposition 12"라고 쓰면 → "Proposition 12" 또는 "프로포지션 12"로 표기
- 원문에 약자가 없으면 **약자를 만들어내지 말 것** (예: MBP 12 같은 임의 약자 금지)
- 인명·기관명·법안명·지명은 원문 표기 정확히 유지
- 영문 고유명사를 한국어로 옮길 때는 표준 표기법 사용 (외래어 표기법)
- 모르는 약자는 그냥 풀어쓰기 (예: "캘리포니아 동물복지 법안")

[저작권 규칙]
- 원문 문장·표현 절대 그대로 사용 금지 (재가공·재구성 필수)
- 직접 인용은 1곳, 15자 이내로만 허용
- 원문 출처 매체명 노출 절대 금지

[기사 구조 - 반드시 이 순서대로]
# 제목 (이모지 1개 포함, 임팩트 있게)
**부제목**: 핵심 내용 2~3줄 요약

도입부 2~3문장

## 1. [소제목] - 배경/현황
## 2. [소제목] - 주요 발표/사건 정리
## 3. [소제목] - 핵심 내용 분석
## 4. [소제목] - 의미·영향
## 5. [소제목] - 향후 전망/농가 시사점

> 📌 **한 줄 요약**: 핵심 메시지

---
*한돈투데이 (Handon Today) | 팜스링크 기자 작성*

[톤·스타일]
- 블로그형 구어체 (딱딱하지 않게)
- 중요 키워드는 **볼드** 처리
- 양돈 농가·업계 실무자가 독자
- 분량: 800~1,200자 내외 (필수 준수)
- 5번 섹션은 **원문에 근거한 시사점**만 (일반론·교과서적 표현 자제)"""


ARTICLE_SYSTEM_PROMPT_OVERSEAS = """당신은 양돈 전문 미디어 '한돈투데이(Handon Today)'의 글로벌 데스크 기자입니다.
영문 양돈 기사를 받아 한국어 블로그형 기사로 재가공합니다.

[★최우선★ 사실 정확성 규칙 - 위반 시 기사 폐기]
- **원문(영문)에 명시된 사실만 사용** — 수치, 날짜, 통계, 기관명, 인물명, 발언 등
- **원문에 없는 정보 추가 절대 금지** — 추측, 가정, 일반론, 배경 지식 추가 금지
- 영문이 짧고 정보가 부족하면, **있는 정보만으로 짧게 작성** (억지로 분량 채우려 추측 금지)
- 모르는 정보는 **언급하지 말 것**
- 추측성 표현 금지: "~로 보입니다", "~할 것으로 예상됩니다", "~가 우려됩니다" 등도 원문 근거 있을 때만
- 수치는 원문 그대로 (반올림·요약·변환·단위 변경 금지)
- 인용된 발언은 원문 발언자가 실제로 한 말만 사용
- 영문을 한국어로 옮길 때 **의미를 부풀리거나 과장하지 말 것**
  예) "monopolies" → "독과점" (O), "공룡 업체" (X — 원문에 없는 표현)

[자기 검증 체크리스트 - 작성 후 반드시 확인]
□ 모든 수치가 원문에 있는가?
□ 모든 기관명·인물명이 원문에 있는가?
□ 인용·발언이 원문에 실제로 있는가?
□ 영문 원문을 과장·왜곡하지 않았는가?
□ 한국 시사점은 원문 사실에 기반한 합리적 추론인가?
하나라도 해당 안 되면 해당 부분 삭제 후 재작성

[고유명사·약자 처리 - 매우 중요]
- 약자(略字)는 **원문(영문)에 있는 것만** 사용
- 원문 "Proposition 12"는 → "Proposition 12" 또는 "프로포지션 12"로 (Prop 12도 가능)
- 원문에 약자가 없으면 **약자를 만들어내지 말 것** (예: MBP 12 같은 임의 약자 금지)
- 인명·기관명·법안명·지명은 원문 표기 정확히 유지
- 한국어 표기 시 표준 외래어 표기법 사용
- 모르는 약자는 그냥 풀어쓰기

[저작권 규칙]
- 원문 문장·표현 절대 그대로 사용 금지
- 직접 인용은 1곳, 한국어 15자 이내로만 허용
- 원문 매체명 노출 절대 금지
- 영문 직역 금지, 한국 독자가 이해하기 쉽게

[기사 구조 - 반드시 이 순서대로]
# 🌍 글로벌 | 제목 (이모지 1개 추가 가능)
**부제목**: 핵심 내용 2~3줄 요약

도입부 2~3문장

## 1. [소제목] - 어떤 일이 일어났나
## 2. [소제목] - 주요 사실·수치 정리
## 3. [소제목] - 글로벌 시장 맥락 (원문 정보 한도 내)
## 4. [소제목] - 한국 양돈 산업에 주는 시사점 (합리적 추론)
## 5. [소제목] - 향후 전망 (원문 근거 있을 때만)

> 📌 **한 줄 요약**: 핵심 메시지

---
*한돈투데이 (Handon Today) | 팜스링크 기자 작성*

[톤·스타일]
- 블로그형 구어체로 친근하게
- 한국 독자 입장에서 의미 부여 (단, 사실 왜곡 금지)
- 영문 고유명사: 한국어 + 영문 병기 (예: 타이슨푸드(Tyson Foods))
- 분량: 800~1,200자 내외 — 단, 원문 정보 부족 시 짧게 작성 가능"""


def build_overseas_pool(asia_articles, global_articles, pool_size=12):
    """
    글로벌 기사 후보 풀을 아시아 6 : 영어권 4 비중으로 구성.
    pool_size: 매칭에 넘길 해외 기사 총 수
    """
    import random
    asia   = list(asia_articles)
    glob   = list(global_articles)
    random.shuffle(asia)
    random.shuffle(glob)

    asia_quota   = round(pool_size * 0.6)   # 12건 → 7건
    global_quota = pool_size - asia_quota   # → 5건

    asia_picks   = asia[:asia_quota]
    global_picks = glob[:global_quota]

    # 한쪽 부족하면 상대편으로 보충
    if len(asia_picks) < asia_quota:
        global_picks += glob[global_quota: global_quota + (asia_quota - len(asia_picks))]
    if len(global_picks) < global_quota:
        asia_picks += asia[asia_quota: asia_quota + (global_quota - len(global_picks))]

    pool = asia_picks + global_picks
    random.shuffle(pool)

    print(f"  해외 풀: 아시아 {len(asia_picks)}건 / 영어권 {len(global_picks)}건")
    return pool


def is_overseas_pair(pair):
    """짝 중 하나라도 해외 소스면 글로벌로 분류"""
    overseas_sources = {
        "The Pig Site", "Pig Progress", "National Hog Farmer", "pig333",
        "soozhu.com", "efeedlink.com", "pasusart.com",
        "livestockemag.com", "nguoichannuoi.vn", "nhachannuoi.vn",
    }
    source_a = pair["article_a"]["source"]
    source_b = pair["article_b"]["source"]
    # 둘 중 하나라도 해외 소스면 글로벌
    return source_a in overseas_sources and source_b in overseas_sources


def generate_article_from_pair(pair):
    """한 쌍으로 새 기사 생성"""
    a = pair["article_a"]
    b = pair["article_b"]
    
    # _category_override가 있으면 우선 사용 (매칭 단계에서 명시적으로 지정된 경우)
    if "_category_override" in pair:
        is_overseas = (pair["_category_override"] == "글로벌")
    else:
        is_overseas = is_overseas_pair(pair)
    system_prompt = ARTICLE_SYSTEM_PROMPT_OVERSEAS if is_overseas else ARTICLE_SYSTEM_PROMPT_KOREA
    
    user_message = f"""다음 두 기사를 참고해 새로운 한국어 양돈 기사를 작성해줘.

[참고 기사 1]
출처: {a['source']}
제목: {a['title']}
본문:
{a['body'][:MAX_BODY_LENGTH]}

---

[참고 기사 2]
출처: {b['source']}
제목: {b['title']}
본문:
{b['body'][:MAX_BODY_LENGTH]}

---

위 규칙에 맞게 새 기사를 작성해줘. 마크다운으로 출력해줘.
원문 매체명은 절대 노출하지 마."""
    
    print(f"\n[기사 생성] {'🌍 글로벌' if is_overseas else '🇰🇷 국내'}")
    print(f"  A: {a['title'][:50]}")
    print(f"  B: {b['title'][:50]}")
    print(f"  사유: {pair['reason']}")
    
    response = call_openrouter_api(
        system_prompt, user_message,
        max_tokens=MAX_TOKENS_ARTICLE, retry=1,
    )
    
    cost = calculate_cost(response["input_tokens"], response["output_tokens"])
    print(f"  토큰: {response['input_tokens']} / {response['output_tokens']}")
    print(f"  비용: ${cost:.4f} (≈{cost*1400:.1f}원)")
    
    article_text = response["text"].strip()
    title_match = re.search(r'^#\s+(.+)$', article_text, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else "제목 없음"
    
    # Step 9: 새 컬럼 자동 생성
    deck = extract_deck(article_text)
    tags = extract_tags_simple(title, article_text)
    read_minutes = calculate_read_minutes(article_text)
    body_html = markdown_to_html_simple(article_text)
    slug = generate_slug_simple(title)
    
    return {
        "title": title,
        "deck": deck,
        "slug": slug,
        "category": "글로벌" if is_overseas else "국내",
        "image_url": get_image_for_article("글로벌" if is_overseas else "국내", title),
        "body": article_text,  # 레거시 (나중에 제거 예정)
        "body_markdown": article_text,
        "body_html": body_html,
        "tags": tags,
        "source_titles": [a["title"], b["title"]],
        "source_urls": [a["url"], b["url"]],
        "match_reason": pair["reason"],
        "read_minutes": read_minutes,
        "generated_at": datetime.now().isoformat(),
        "tokens": {
            "input": response["input_tokens"],
            "output": response["output_tokens"],
        },
        "cost_usd": cost,
    }


# ──────────────────────────────────────────────────
# 3단계: 자동 검수
# ──────────────────────────────────────────────────

def validate_article(article):
    """생성된 기사 자동 검수"""
    body = article["body"]
    title = article["title"]
    issues = []
    
    body_length = len(body)
    if body_length < 600:
        issues.append(f"본문이 너무 짧음 ({body_length}자)")
    if body_length > 3000:
        issues.append(f"본문이 너무 김 ({body_length}자)")
    
    section_count = len(re.findall(r'^##\s+\d+\.', body, re.MULTILINE))
    if section_count < 5:
        issues.append(f"섹션이 {section_count}개뿐 (5개 필요)")
    
    if "한 줄 요약" not in body:
        issues.append("'한 줄 요약' 블록 없음")
    
    pig_keywords = ["양돈", "돼지", "한돈", "농가", "사료", "축산", "ASF", "도체", "양돈장", "돈가"]
    if not any(kw in body for kw in pig_keywords):
        issues.append("양돈 관련 키워드 없음")
    
    if article["category"] == "글로벌":
        if "🌍" not in title and "글로벌" not in title:
            issues.append("해외 기사인데 글로벌 레이블 없음")
    
    if "한돈투데이" not in body:
        issues.append("한돈투데이 푸터 없음")
    
    if "팜스링크 기자 작성" not in body:
        issues.append("AI 작성 표시 없음")
    
    return {"passed": len(issues) == 0, "issues": issues}


def generate_with_validation(pair, max_retries=1):
    """검수 통과까지 생성 (실패 시 1번 재생성)"""
    last_article = None
    
    for attempt in range(max_retries + 1):
        if attempt > 0:
            print(f"\n  🔄 검수 실패로 재생성 ({attempt}/{max_retries})")
        
        article = generate_article_from_pair(pair)
        validation = validate_article(article)
        article["validation"] = validation
        
        if validation["passed"]:
            print(f"  ✅ 검수 통과")
            return article
        else:
            print(f"  ⚠️ 검수 이슈:")
            for issue in validation["issues"]:
                print(f"    - {issue}")
            last_article = article
    
    print(f"  ❌ 재생성도 실패. 이슈 있는 채로 저장")
    return last_article


# ──────────────────────────────────────────────────
# 통합 실행
# ──────────────────────────────────────────────────

def load_articles(filenames):
    all_articles = []
    for fn in filenames:
        with open(fn, "r", encoding="utf-8") as f:
            all_articles.extend(json.load(f))
    return all_articles


def save_results(articles, filename=None):
    if filename is None:
        filename = f"generated_articles_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)
    print(f"  📁 JSON 저장: {filename}")
    return filename


def save_markdown(articles, filename=None):
    if filename is None:
        filename = f"generated_articles_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
    
    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"# 자동 생성 기사 모음 ({datetime.now().strftime('%Y-%m-%d %H:%M')})\n\n")
        for i, art in enumerate(articles, 1):
            f.write(f"\n---\n\n## 📄 기사 {i} ({art['category']})\n\n")
            f.write(f"**짝짓기 사유**: {art['match_reason']}\n\n")
            f.write(f"**원본 제목 (내부 참고용)**:\n")
            for ttl in art['source_titles']:
                f.write(f"- {ttl}\n")
            
            v = art.get('validation', {})
            if v.get('passed'):
                f.write(f"\n**검수**: ✅ 합격\n\n")
            else:
                f.write(f"\n**검수**: ⚠️ {', '.join(v.get('issues', []))}\n\n")
            
            f.write(f"**비용**: ${art.get('cost_usd', 0):.4f}\n\n")
            f.write(f"---\n\n{art['body']}\n\n")
    
    print(f"  📁 Markdown 저장: {filename}")
    return filename


def run_pipeline(input_files, test_mode=False, max_pairs=4, recent_hours=24):
    print(f"\n{'='*60}")
    print(f"  한돈투데이 기사 생성: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  모델: {MODEL}")
    print(f"{'='*60}")
    
    articles = load_articles(input_files)
    print(f"\n[로드] 총 {len(articles)}건")
    
    articles = filter_recent_articles(articles, hours=recent_hours)
    print(f"[필터] 최근 {recent_hours}시간: {len(articles)}건")
    
    if len(articles) < 2:
        print("\n[종료] 기사 부족")
        return []
    
    pairs = match_articles_with_ai(articles, max_pairs=max_pairs)
    if not pairs:
        print("\n[종료] 짝짓기 결과 없음")
        return []
    
    if test_mode:
        pairs = pairs[:1]
        print(f"\n[테스트 모드] 1쌍만 생성")
    
    generated = []
    total_cost = 0.0
    
    for i, pair in enumerate(pairs, 1):
        print(f"\n{'─'*60}")
        print(f"  [{i}/{len(pairs)}] 기사 생성")
        print(f"{'─'*60}")
        try:
            article = generate_with_validation(pair, max_retries=1)
            total_cost += article.get("cost_usd", 0)
            generated.append(article)
        except Exception as e:
            print(f"  ❌ 생성 실패: {e}")
    
    print(f"\n{'='*60}")
    print(f"  생성 완료: {len(generated)}건")
    print(f"  검수 통과: {sum(1 for a in generated if a['validation']['passed'])}건")
    print(f"  총 비용: ${total_cost:.4f} (≈{total_cost*1400:.0f}원)")
    print(f"{'='*60}")
    
    if generated:
        save_results(generated)
        save_markdown(generated)
    
    return generated


# ──────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────

def parse_args(args):
    test_mode = False
    max_pairs = 4
    files = []
    
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--test":
            test_mode = True
        elif arg == "--max-pairs":
            i += 1
            if i < len(args):
                max_pairs = int(args[i])
        elif not arg.startswith("--"):
            files.append(arg)
        i += 1
    
    return files, test_mode, max_pairs


def print_usage():
    print("""사용법:
  python article_generator.py <korea_articles.json> [overseas_articles.json] [옵션]

옵션:
  --test            테스트 모드 (1쌍만 생성)
  --max-pairs N     최대 짝 수 (기본 4)

예시:
  python article_generator.py korea_articles_20260505.json overseas_articles_20260505.json
  python article_generator.py korea_articles_*.json --test

환경변수:
  PowerShell:  $env:OPENROUTER_API_KEY="sk-or-v1-..."
  CMD:         set OPENROUTER_API_KEY=sk-or-v1-...
""")


if __name__ == "__main__":
    files, test_mode, max_pairs = parse_args(sys.argv[1:])
    
    if not files:
        print_usage()
        sys.exit(1)
    
    for f in files:
        if not os.path.exists(f):
            print(f"❌ 파일 없음: {f}")
            sys.exit(1)
    
    if not OPENROUTER_API_KEY:
        print("❌ OPENROUTER_API_KEY 환경변수가 설정되지 않았어요.\n")
        print("[Windows PowerShell]")
        print('  $env:OPENROUTER_API_KEY="sk-or-v1-..."')
        print("\n[Windows CMD]")
        print("  set OPENROUTER_API_KEY=sk-or-v1-...")
        print(f"\n그런 다음:\n  python article_generator.py {' '.join(files)}")
        sys.exit(1)
    
    if test_mode:
        max_pairs = 1
    
    run_pipeline(files, test_mode=test_mode, max_pairs=max_pairs)
# ──────────────────────────────────────────────────
# Cloud Functions용 추가 함수 (메모리 내 데이터 직접 받기)
# ──────────────────────────────────────────────────

def run_pipeline_from_data(articles, test_mode=False, max_pairs=4, recent_hours=24, save_files=False):
    """
    파일 대신 메모리에 있는 articles 리스트를 직접 받아 처리.
    Cloud Functions에서 사용.

    국내 2쌍 + 글로벌 2쌍 = 항상 4건 생성 (반반 균형).
    국내 기사 부족 시 글로벌로 보충해서 합계 4건 유지.
    """
    print(f"\n{'='*60}")
    print(f"  한돈투데이 기사 생성: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  모델: {MODEL}")
    print(f"{'='*60}")

    print(f"\n[입력] 총 {len(articles)}건")

    # 국내 / 아시아 / 영어권 분리 (main.py에서 region 필드로 분리해서 넘김)
    korea_articles  = [a for a in articles if a.get("source_type") == "korea"]
    asia_articles   = [a for a in articles if a.get("region") == "asia"]
    global_articles = [a for a in articles if a.get("region") == "global"]

    # 각각 최신 필터 적용
    korea_pool  = filter_recent_articles(korea_articles,  hours=recent_hours)
    asia_pool   = filter_recent_articles(asia_articles,   hours=recent_hours)
    global_pool = filter_recent_articles(global_articles, hours=recent_hours)

    print(f"[분리] 국내 {len(korea_pool)}건 / 아시아 {len(asia_pool)}건 / 영어권 {len(global_pool)}건 (필터 후)")

    # ── 목표 쌍 수 계산 ───────────────────────────────────────
    TOTAL_PAIRS      = 4
    target_korea     = 2
    max_korea_pairs  = len(korea_pool) // 2
    actual_korea     = min(target_korea, max_korea_pairs)
    actual_global    = TOTAL_PAIRS - actual_korea

    print(f"[목표] 국내 {actual_korea}쌍 + 글로벌 {actual_global}쌍 = {TOTAL_PAIRS}건")

    # ── 국내 매칭 ─────────────────────────────────────────────
    korea_pairs = []
    if actual_korea > 0 and len(korea_pool) >= 2:
        print(f"\n[국내 매칭] {len(korea_pool)}건 → {actual_korea}쌍 요청")
        korea_pairs = match_articles_with_ai(korea_pool, max_pairs=actual_korea)
        korea_pairs = korea_pairs[:actual_korea]  # 초과 방지
        for p in korea_pairs:
            p["_category_override"] = "국내"
        print(f"  → {len(korea_pairs)}쌍 확정")

    # 국내 매칭이 목표보다 적으면 글로벌로 추가 보충
    shortfall      = actual_korea - len(korea_pairs)
    actual_global += shortfall
    if shortfall > 0:
        print(f"  ⚠️ 국내 {shortfall}쌍 부족 → 글로벌 {actual_global}쌍으로 보충")

    # ── 글로벌 매칭: 아시아 1쌍 + 영어권 1쌍으로 분리 ──────────
    global_pairs = []
    if actual_global > 0:
        # actual_global 배분: 아시아 ceil, 영어권 floor
        import math
        asia_global_pairs = math.ceil(actual_global / 2)
        eng_global_pairs  = actual_global - asia_global_pairs

        print(f"\n[글로벌 매칭] 아시아 {asia_global_pairs}쌍 + 영어권 {eng_global_pairs}쌍 목표")

        # 아시아 매칭
        if asia_global_pairs > 0 and len(asia_pool) >= 2:
            print(f"  [아시아] {len(asia_pool)}건 → {asia_global_pairs}쌍 요청")
            asia_pairs = match_articles_with_ai(asia_pool, max_pairs=asia_global_pairs)
            asia_pairs = asia_pairs[:asia_global_pairs]  # 초과 방지
            for p in asia_pairs:
                p["_category_override"] = "글로벌"
            global_pairs.extend(asia_pairs)
            print(f"  → {len(asia_pairs)}쌍 확정")
        else:
            asia_pairs = []

        # 영어권 매칭
        if eng_global_pairs > 0 and len(global_pool) >= 2:
            print(f"  [영어권] {len(global_pool)}건 → {eng_global_pairs}쌍 요청")
            eng_pairs = match_articles_with_ai(global_pool, max_pairs=eng_global_pairs)
            eng_pairs = eng_pairs[:eng_global_pairs]  # 초과 방지
            for p in eng_pairs:
                p["_category_override"] = "글로벌"
            global_pairs.extend(eng_pairs)
            print(f"  → {len(eng_pairs)}쌍 확정")
        else:
            eng_pairs = []

        print(f"  글로벌 총 {len(global_pairs)}쌍 확정")

    pairs = korea_pairs + global_pairs

    if not pairs:
        print("\n[종료] 짝짓기 결과 없음")
        return []

    if test_mode:
        pairs = pairs[:1]
        print(f"\n[테스트 모드] 1쌍만 생성")

    generated = []
    total_cost = 0.0

    for i, pair in enumerate(pairs, 1):
        print(f"\n{'─'*60}")
        print(f"  [{i}/{len(pairs)}] 기사 생성")
        print(f"{'─'*60}")
        try:
            article = generate_with_validation(pair, max_retries=1)
            total_cost += article.get("cost_usd", 0)
            generated.append(article)
        except Exception as e:
            print(f"  ❌ 생성 실패: {e}")

    korea_count  = sum(1 for a in generated if a.get("category") == "국내")
    global_count = sum(1 for a in generated if a.get("category") == "글로벌")

    print(f"\n{'='*60}")
    print(f"  생성 완료: {len(generated)}건 (국내 {korea_count} / 글로벌 {global_count})")
    print(f"  검수 통과: {sum(1 for a in generated if a['validation']['passed'])}건")
    print(f"  총 비용: ${total_cost:.4f} (≈{total_cost*1400:.0f}원)")
    print(f"{'='*60}")

    if save_files and generated:
        save_results(generated)
        save_markdown(generated)

    return generated


# ──────────────────────────────────────────────────
# Step 9 추가: 새 컬럼 자동 생성
# ──────────────────────────────────────────────────

def extract_deck(body_markdown):
    """부제목 추출: **...** 형식의 첫 줄"""
    match = re.search(r'\*\*(.+?)\*\*', body_markdown)
    if match:
        deck = match.group(1).strip()
        # 너무 길면 자르기
        if len(deck) > 200:
            deck = deck[:197] + "..."
        return deck
    return None


def extract_tags_simple(title, body_markdown):
    """간단한 키워드 추출 (AI 호출 없이)"""
    # 양돈 관련 주요 키워드
    keywords = [
        "ASF", "아프리카돼지열병", "양돈", "돼지", "한돈", "사료", 
        "축산", "농가", "돈가", "도체", "수입육", "글로벌",
        "정책", "질병", "동물복지", "탄소배출", "친환경"
    ]
    
    text = title + " " + body_markdown
    found_tags = []
    
    for kw in keywords:
        if kw in text and kw not in found_tags:
            found_tags.append(kw)
        if len(found_tags) >= 5:  # 최대 5개
            break
    
    return found_tags


def calculate_read_minutes(body_markdown):
    """읽는 시간 계산 (분당 200자 가정)"""
    char_count = len(body_markdown)
    minutes = max(1, char_count // 200)
    return minutes


def markdown_to_html_simple(body_markdown):
    """간단한 마크다운 → HTML 변환"""
    import html
    
    # HTML 이스케이프
    text = html.escape(body_markdown)
    
    # 제목
    text = re.sub(r'^# (.+)$', r'<h1>\1</h1>', text, flags=re.MULTILINE)
    text = re.sub(r'^## (.+)$', r'<h2>\1</h2>', text, flags=re.MULTILINE)
    text = re.sub(r'^### (.+)$', r'<h3>\1</h3>', text, flags=re.MULTILINE)
    
    # 볼드
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    
    # 블록쿼트
    text = re.sub(r'^&gt; (.+)$', r'<blockquote>\1</blockquote>', text, flags=re.MULTILINE)
    
    # 줄바꿈 → <p>
    paragraphs = text.split('\n\n')
    html_parts = []
    for p in paragraphs:
        p = p.strip()
        if p:
            # 이미 태그로 시작하면 그대로
            if p.startswith('<'):
                html_parts.append(p)
            else:
                # 줄바꿈을 <br>로
                p = p.replace('\n', '<br>\n')
                html_parts.append(f'<p>{p}</p>')
    
    return '\n'.join(html_parts)


def generate_slug_simple(title):
    """제목에서 간단한 slug 생성"""
    from slugify import slugify
    # 이모지 제거
    title_clean = re.sub(r'[^\w\s가-힣a-zA-Z0-9-]', '', title)
    return slugify(title_clean, max_length=100, word_boundary=True)
