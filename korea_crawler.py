"""
================================================================
  국내 양돈 뉴스 크롤러 — korea_crawler.py
  v3.3.4
  korea_crawler.py
================================================================

[v2 변경사항]
  - 종료 마커 '저작권자' 추가 (한돈뉴스/양돈타임스 본문 정리)
  - 광고 스크립트 변수(var ___BANNER) 자동 제거
  - 본문 끝 메타 정보(기자 보기, 다른기사) 추가 정리

[검증 완료 사이트]
  ① 돼지와사람       (pigpeople.net)
  ② 한돈뉴스         (pignpork.com)
  ③ 양돈타임스       (pigtimes.co.kr)        - HTTP만
  ④ 라이브한돈뉴스   (handonnews.kr)         - RSS+크롤링
"""

import urllib.request
import urllib.parse
import re
import sys
import json
from datetime import datetime


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/121.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

HTML_ENTITIES = {
    "&nbsp;": " ", "&quot;": '"', "&apos;": "'", "&#39;": "'",
    "&lt;": "<", "&gt;": ">", "&amp;": "&", "&middot;": "·",
    "&lsquo;": "'", "&rsquo;": "'", "&ldquo;": '"', "&rdquo;": '"',
    "&hellip;": "…", "&mdash;": "—", "&ndash;": "–", "&euro;": "€",
    "&copy;": "©", "&reg;": "®", "&trade;": "™",
}


def fetch(url, timeout=15):
    req = urllib.request.Request(url, headers=HEADERS)
    res = urllib.request.urlopen(req, timeout=timeout)
    raw = res.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("euc-kr", errors="ignore")


def clean_html_entities(text):
    for entity, char in HTML_ENTITIES.items():
        text = text.replace(entity, char)
    text = re.sub(r'&#(\d+);', lambda m: chr(int(m.group(1))), text)
    return text


def remove_inline_scripts(text):
    """본문에 박힌 광고/스크립트 변수 선언 제거"""
    text = re.sub(r'(?:var|let|const)\s+\w+\s*=\s*[^;\n]+;?', '', text)
    text = re.sub(r'window\.\w+\s*=\s*[^;\n]+;?', '', text)
    return text


def strip_html_tags(html_chunk):
    """HTML 태그 + script/style 제거"""
    text = ""
    in_tag = False
    i = 0
    while i < len(html_chunk):
        if not in_tag and html_chunk[i:i+7].lower() == "<script":
            close = html_chunk.find("</script>", i)
            i = close + 9 if close != -1 else len(html_chunk)
            continue
        if not in_tag and html_chunk[i:i+6].lower() == "<style":
            close = html_chunk.find("</style>", i)
            i = close + 8 if close != -1 else len(html_chunk)
            continue
        c = html_chunk[i]
        if c == "<":
            in_tag = True
        elif c == ">":
            in_tag = False
            text += " "
        elif not in_tag:
            text += c
        i += 1
    
    text = remove_inline_scripts(text)
    text = clean_html_entities(text)
    
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    return "\n".join(lines)


def remove_trailing_metadata(text):
    """본문 끝의 메타 정보 제거"""
    cutoff_patterns = [
        r'\s*[가-힣]+\s*기자\s*의?\s*전체기사\s*보기.*$',
        r'\s*관리자\s+의\s+전체기사\s+보기.*$',
        r'\s*다른기사\s*보기.*$',
        r'저작권자\s*©.*$',
        r'저작권자\s*ⓒ.*$',
        r'무단\s*전재\s*및\s*재배포\s*금지.*$',
        r'\s*\[\s*[가-힣]+\s*기자\s*\]\s*$',
    ]
    
    for pattern in cutoff_patterns:
        match = re.search(pattern, text, re.MULTILINE | re.DOTALL)
        if match:
            text = text[:match.start()].strip()
    
    return text


def extract_article_urls(list_html, url_pattern, base_url, limit=5):
    urls = []
    seen = set()
    i = 0
    while len(urls) < limit:
        idx = list_html.find(url_pattern, i)
        if idx == -1:
            break
        start = list_html.rfind('href="', max(0, idx-200), idx)
        if start == -1:
            i = idx + 1
            continue
        start += 6
        end = list_html.find('"', start)
        href = list_html[start:end]
        if href.startswith("/"):
            href = base_url + href
        elif not href.startswith("http"):
            href = base_url + "/" + href
        if href not in seen:
            urls.append(href)
            seen.add(href)
        i = end
    return urls


def extract_title_from_title_tag(html, separators):
    ts = html.find("<title>")
    if ts == -1:
        return ""
    te = html.find("</title>", ts)
    raw = html[ts+7:te].strip()
    raw = clean_html_entities(raw)
    for sep in separators:
        if sep in raw:
            raw = raw.split(sep)[0].strip()
            break
    return raw


def extract_body_between(html, start_marker, end_markers):
    start = html.find(start_marker)
    if start == -1:
        return ""
    body_start = html.find(">", start) + 1
    
    body_end = len(html)
    for em in end_markers:
        pos = html.find(em, body_start)
        if pos != -1 and pos < body_end:
            body_end = pos
    
    if body_end - body_start > 60000:
        body_end = body_start + 60000
    
    text = strip_html_tags(html[body_start:body_end])
    text = remove_trailing_metadata(text)
    return text


# ──────────────────────────────────────────────────
# 사이트별 크롤러
# ──────────────────────────────────────────────────

def crawl_pigpeople(limit=5):
    SITE_NAME = "돼지와사람"
    BASE_URL = "https://www.pigpeople.net"
    LIST_URL = f"{BASE_URL}/news/section_list_all.html?sec_no=2"
    
    results = []
    try:
        list_html = fetch(LIST_URL)
        urls = extract_article_urls(list_html, "article.html?no=", BASE_URL, limit)
        
        for url in urls:
            try:
                article_html = fetch(url)
                title = extract_title_from_title_tag(
                    article_html,
                    [" - 돼지와사람", " | 돼지와사람", "::"]
                )
                body = extract_body_between(
                    article_html,
                    'itemprop="articleBody"',
                    [
                        '저작권자',
                        '<ul class="btn_share"', 'class="btn_share"',
                        'id="article-bottom"', 'class="reporter"',
                    ]
                )
                if len(body) >= 200:
                    results.append({
                        "source": SITE_NAME, "url": url,
                        "title": title, "body": body,
                        "scraped_at": datetime.now().isoformat(),
                    })
            except Exception as e:
                print(f"  [기사 추출 실패] {url}: {e}")
    except Exception as e:
        print(f"  [{SITE_NAME} 목록 페이지 오류] {e}")
    return results


def crawl_pignpork(limit=5):
    SITE_NAME = "한돈뉴스"
    BASE_URL = "https://www.pignpork.com"
    LIST_URL = f"{BASE_URL}/news/articleList.html?sc_section_code=S1N1"
    
    results = []
    try:
        list_html = fetch(LIST_URL)
        urls = extract_article_urls(list_html, "articleView.html?idxno=", BASE_URL, limit)
        
        for url in urls:
            try:
                article_html = fetch(url)
                title = extract_title_from_title_tag(
                    article_html,
                    [" < 한돈뉴스", " - 한돈뉴스", "::"]
                )
                body = extract_body_between(
                    article_html,
                    'id="article-view-content-div"',
                    [
                        '저작권자',          # +3,148 위치
                        'var ___BANNER',     # +1,124 - 더 앞에 차단
                        '<ul class="btn_share"', 'class="btn_share"',
                        'id="article-bottom"', 'class="reporter-list"',
                        'class="copyright"',
                    ]
                )
                if len(body) >= 200:
                    results.append({
                        "source": SITE_NAME, "url": url,
                        "title": title, "body": body,
                        "scraped_at": datetime.now().isoformat(),
                    })
            except Exception as e:
                print(f"  [기사 추출 실패] {url}: {e}")
    except Exception as e:
        print(f"  [{SITE_NAME} 목록 페이지 오류] {e}")
    return results


def crawl_pigtimes(limit=5):
    SITE_NAME = "양돈타임스"
    BASE_URL = "http://www.pigtimes.co.kr"
    LIST_URL = f"{BASE_URL}/news/articleList.html?sc_section_code=S1N7"
    
    results = []
    try:
        list_html = fetch(LIST_URL)
        urls = extract_article_urls(list_html, "articleView.html?idxno=", BASE_URL, limit)
        
        for url in urls:
            try:
                article_html = fetch(url)
                title = extract_title_from_title_tag(
                    article_html,
                    [" - 양돈타임스", " | 양돈타임스", " < ", "::"]
                )
                body = extract_body_between(
                    article_html,
                    'id="article-view-content-div"',
                    [
                        '저작권자',          # +1,191 위치
                        'var ___BANNER',     # +1,844
                        '<ul class="btn_share"', 'class="btn_share"',
                        'id="article-bottom"', 'class="reporter-list"',
                        'class="copyright"',
                    ]
                )
                if len(body) >= 200:
                    results.append({
                        "source": SITE_NAME, "url": url,
                        "title": title, "body": body,
                        "scraped_at": datetime.now().isoformat(),
                    })
            except Exception as e:
                print(f"  [기사 추출 실패] {url}: {e}")
    except Exception as e:
        print(f"  [{SITE_NAME} 목록 페이지 오류] {e}")
    return results


def crawl_handonnews(limit=5):
    SITE_NAME = "라이브한돈뉴스"
    BASE_URL = "https://www.handonnews.kr"
    RSS_URL = f"{BASE_URL}/data/rss/news.xml"
    
    results = []
    try:
        rss = fetch(RSS_URL)
        urls = []
        i = 0
        while len(urls) < limit:
            item_start = rss.find("<item>", i)
            if item_start == -1:
                break
            item_end = rss.find("</item>", item_start)
            item = rss[item_start:item_end]
            
            link_start = item.find("<link>")
            link_end = item.find("</link>", link_start)
            if link_start != -1 and link_end != -1:
                link = item[link_start+6:link_end].replace("<![CDATA[", "").replace("]]>", "").strip()
                if link and link not in urls:
                    urls.append(link)
            i = item_end
        
        for url in urls:
            try:
                article_html = fetch(url)
                title = extract_title_from_title_tag(
                    article_html,
                    [" < 라이브한돈뉴스", " - 라이브한돈뉴스", "::"]
                )
                body = extract_body_between(
                    article_html,
                    'itemprop="articleBody"',
                    [
                        '저작권자',
                        '<ul class="btn_share"', 'class="btn_share"',
                        'id="article-bottom"', 'class="reporter"',
                    ]
                )
                if len(body) >= 100:
                    results.append({
                        "source": SITE_NAME, "url": url,
                        "title": title, "body": body,
                        "scraped_at": datetime.now().isoformat(),
                    })
            except Exception as e:
                print(f"  [기사 추출 실패] {url}: {e}")
    except Exception as e:
        print(f"  [{SITE_NAME} RSS 오류] {e}")
    return results


# ──────────────────────────────────────────────────
# 통합 실행
# ──────────────────────────────────────────────────



# ──────────────────────────────────────────────────
# 돈가 파싱 + DB 저장
# ──────────────────────────────────────────────────

def parse_dongga_from_articles(articles):
    """
    수집된 기사 본문에서 돈가(원/kg) 파싱.
    pigpeople.net, pignpork.com 등 모든 소스에서 시도.

    패턴 설계 원칙:
    - 1차: 단위 명시 패턴 (kg당, /㎏, /kg) — 오탐 최소
    - 2차: 돼지/돈 맥락 패턴 — 앞뒤 50자 내에 돼지 키워드 필수
    """
    import re

    # 1차: 단위가 명확 + 문맥상 돼지 전용 패턴 (오탐 최소)
    unit_patterns = [
        r'kg당\s*([\d,]+)원',   # "kg당 5,480원" — 돈가 기사에서만 씀
        r'㎏당\s*([\d,]+)원',   # "㎏당 5,480원"
    ]

    # 2차: 맥락 의존 패턴 — 앞뒤 50자 내에 돼지 키워드 필수
    context_patterns = [
        r'([\d,]+)원/㎏',
        r'([\d,]+)원/kg',
        r'경락가격[^0-9]*([\d,]+)원',
        r'경락가[^0-9]*([\d,]+)원',
        r'도매가격[^0-9]*([\d,]+)원',
        r'돈가[^0-9]*([\d,]+)원',
        r'돼지값[^0-9]*([\d,]+)원',
    ]
    pig_keywords = re.compile(r'돼지|돈가|한돈|양돈|돈육|돼지고기')

    # 우선순위: 돼지와사람 → 한돈뉴스 → 나머지
    priority = ['돼지와사람', '한돈뉴스']
    sorted_articles = (
        [a for a in articles if a.get('source') in priority] +
        [a for a in articles if a.get('source') not in priority]
    )

    for article in sorted_articles:
        body = article.get('body', '')
        if not body:
            continue

        # 1차: 단위 명시 패턴
        for pattern in unit_patterns:
            for m in re.findall(pattern, body):
                price = int(m.replace(',', ''))
                if 3000 <= price <= 10000:
                    print(f"  [돈가] {article.get('source','?')} 1차 파싱 성공: {price:,}원")
                    return price

        # 2차: 맥락 패턴 — 매칭 위치 앞뒤 50자에 돼지 키워드 있을 때만
        for pattern in context_patterns:
            for match in re.finditer(pattern, body):
                m = match.group(1)
                price = int(m.replace(',', ''))
                if 3000 <= price <= 10000:
                    start = max(0, match.start() - 50)
                    end   = min(len(body), match.end() + 50)
                    context = body[start:end]
                    if pig_keywords.search(context):
                        print(f"  [돈가] {article.get('source','?')} 2차 파싱 성공: {price:,}원")
                        return price

    print("  [돈가] 모든 소스에서 파싱 실패")
    return None


def save_dongga_to_db(engine, price, date=None):
    """돈가를 DB에 저장 (이미 있으면 스킵)"""
    from sqlalchemy import text
    from datetime import datetime, timedelta, timezone

    KST = timezone(timedelta(hours=9))
    if date is None:
        # 어제 날짜 (기사에 나오는 돈가는 전날 기준)
        date = (datetime.now(KST) - timedelta(days=1)).date()

    try:
        with engine.connect() as conn:
            existing = conn.execute(text(
                "SELECT price FROM dong_price WHERE date = :date"
            ), {"date": date}).fetchone()

            if existing:
                print(f"  [돈가] {date} 이미 존재 ({existing[0]:,}원) — 스킵")
                return False

            conn.execute(text(
                "INSERT INTO dong_price (date, price, source) VALUES (:date, :price, 'crawled')"
            ), {"date": date, "price": price})
            conn.commit()
            print(f"  [돈가] {date} 저장 완료: {price:,}원/kg")
            return True
    except Exception as e:
        print(f"  [돈가 DB 저장 오류] {e}")
        return False

CRAWLERS = {
    "pigpeople":   ("돼지와사람",       crawl_pigpeople),
    "pignpork":    ("한돈뉴스",         crawl_pignpork),
    "pigtimes":    ("양돈타임스",       crawl_pigtimes),
    "handonnews":  ("라이브한돈뉴스",   crawl_handonnews),
}


def crawl_all(limit_per_site=5):
    print(f"\n{'='*60}")
    print(f"  국내 양돈 뉴스 수집: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")
    
    all_articles = []
    for key, (name, func) in CRAWLERS.items():
        print(f"\n[{name}] 수집 중...")
        articles = func(limit=limit_per_site)
        # article_generator의 분리 로직에 필요한 source_type 필드 추가
        for a in articles:
            a["source_type"] = "korea"
        print(f"  → {len(articles)}건 수집 완료")
        all_articles.extend(articles)
    
    print(f"\n{'='*60}")
    print(f"  총 {len(all_articles)}건 수집됨")
    print(f"{'='*60}\n")
    return all_articles


def save_to_file(articles, filename=None):
    if filename is None:
        filename = f"korea_articles_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)
    print(f"  📁 저장: {filename}")
    return filename


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in CRAWLERS:
        key = sys.argv[1]
        name, func = CRAWLERS[key]
        print(f"\n[{name}] 단독 테스트")
        print("=" * 60)
        articles = func(limit=3)
        for i, a in enumerate(articles, 1):
            print(f"\n--- 기사 {i} ---")
            print(f"제목: {a['title']}")
            print(f"URL: {a['url']}")
            print(f"본문 길이: {len(a['body'])}자")
            print(f"본문 처음 300자:\n{a['body'][:300]}")
            print(f"본문 마지막 200자:\n{a['body'][-200:]}")
        print(f"\n총 {len(articles)}건")
    else:
        articles = crawl_all(limit_per_site=3)
        
        from collections import Counter
        counter = Counter(a['source'] for a in articles)
        print("\n[사이트별 수집 현황]")
        for source, count in counter.items():
            print(f"  • {source}: {count}건")
        
        if articles:
            lengths = [len(a['body']) for a in articles]
            print(f"\n[본문 길이]")
            print(f"  • 최소: {min(lengths):,}자")
            print(f"  • 최대: {max(lengths):,}자")
            print(f"  • 평균: {sum(lengths)//len(lengths):,}자")
        
        if articles:
            save_to_file(articles)
        print()
