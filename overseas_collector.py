"""
================================================================
  해외 양돈 뉴스 수집 모듈 v2
  overseas_collector.py
================================================================

[v2 변경사항]
  - The Pig Site: 본문 끝 'Our Partners' 광고 영역 자동 제거
  - 공통: 본문 끝 메타 정보 정리 강화

[검증 완료 사이트]
  ⑤ The Pig Site    (thepigsite.com)         - 직접 크롤링
  ⑥ Pig Progress    (pigprogress.net)        - RSS description (페이월)
  ⑦ NHF             (nationalhogfarmer.com)  - RSS 헤드라인만
  ⑧ pig333          (pig333.com)             - RSS+본문 크롤링
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
    "Accept-Language": "en-US,en;q=0.9",
}

HTML_ENTITIES = {
    "&nbsp;": " ", "&quot;": '"', "&apos;": "'", "&#39;": "'",
    "&lt;": "<", "&gt;": ">", "&amp;": "&", "&middot;": "·",
    "&lsquo;": "'", "&rsquo;": "'", "&ldquo;": '"', "&rdquo;": '"',
    "&hellip;": "…", "&mdash;": "—", "&ndash;": "–", "&euro;": "€",
    "&copy;": "©", "&reg;": "®", "&trade;": "™",
}


# ──────────────────────────────────────────────────
# 공통 유틸리티
# ──────────────────────────────────────────────────

def safe_url(url):
    try:
        url.encode('ascii')
        return url
    except UnicodeEncodeError:
        parsed = urllib.parse.urlsplit(url)
        path = urllib.parse.quote(parsed.path, safe='/')
        query = urllib.parse.quote(parsed.query, safe='=&')
        return urllib.parse.urlunsplit((
            parsed.scheme, parsed.netloc, path, query, parsed.fragment
        ))


def fetch(url, timeout=15):
    req = urllib.request.Request(safe_url(url), headers=HEADERS)
    res = urllib.request.urlopen(req, timeout=timeout)
    return res.read().decode("utf-8", errors="ignore")


def clean_html_entities(text):
    for entity, char in HTML_ENTITIES.items():
        text = text.replace(entity, char)
    text = re.sub(r'&#(\d+);', lambda m: chr(int(m.group(1))), text)
    return text


def strip_html_tags(html_chunk):
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
    
    text = clean_html_entities(text)
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    return "\n".join(lines)


def parse_rss_items(rss_xml, limit=5):
    items = []
    i = 0
    while len(items) < limit:
        item_start = rss_xml.find("<item>", i)
        if item_start == -1:
            item_start = rss_xml.find("<item ", i)
        if item_start == -1:
            break
        item_end = rss_xml.find("</item>", item_start)
        if item_end == -1:
            break
        item = rss_xml[item_start:item_end]
        
        ts = item.find("<title>")
        te = item.find("</title>", ts)
        title = item[ts+7:te].replace("<![CDATA[", "").replace("]]>", "").strip() if ts != -1 else ""
        title = clean_html_entities(title)
        
        ls = item.find("<link>")
        le = item.find("</link>", ls)
        link = item[ls+6:le].replace("<![CDATA[", "").replace("]]>", "").strip() if ls != -1 else ""
        
        ds = item.find("<description>")
        de = item.find("</description>", ds)
        desc_raw = item[ds+13:de].replace("<![CDATA[", "").replace("]]>", "").strip() if ds != -1 else ""
        desc = re.sub(r'<[^>]+>', ' ', desc_raw)
        desc = clean_html_entities(re.sub(r'\s+', ' ', desc).strip())
        
        ps = item.find("<pubDate>")
        pe = item.find("</pubDate>", ps)
        pub_date = item[ps+9:pe].strip()[:25] if ps != -1 else ""
        
        if title and link:
            items.append({
                "title": title, "link": link,
                "description": desc, "pub_date": pub_date,
            })
        i = item_end
    return items


# ──────────────────────────────────────────────────
# ⑤ The Pig Site (직접 크롤링) - v2: 노이즈 정리
# ──────────────────────────────────────────────────

def clean_thepigsite_body(text):
    """The Pig Site 본문 끝 광고/메타 정보 제거"""
    # 'Our Partners' 이후 모두 제거
    cutoff_patterns = [
        r'\nOur Partners\n.*$',
        r'\nMore from this author.*$',
    ]
    for pattern in cutoff_patterns:
        text = re.sub(pattern, '', text, flags=re.DOTALL | re.IGNORECASE)
    return text.strip()


def crawl_thepigsite(limit=5):
    SITE_NAME = "The Pig Site"
    BASE_URL = "https://www.thepigsite.com"
    LIST_URL = f"{BASE_URL}/latest?section=news"
    
    results = []
    try:
        list_html = fetch(LIST_URL)
        
        urls = []
        seen = set()
        i = 0
        while len(urls) < limit:
            idx = -1
            for year in ['/news/2026/', '/news/2025/', '/news/2024/']:
                p = list_html.find(year, i)
                if p != -1 and (idx == -1 or p < idx):
                    idx = p
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
                href = BASE_URL + href
            if href.count("/") < 6:
                i = end
                continue
            if href not in seen:
                urls.append(href)
                seen.add(href)
            i = end
        
        for url in urls:
            try:
                article_html = fetch(url)
                
                title = ""
                og = article_html.find('property="og:title"')
                if og != -1:
                    cs = article_html.rfind('content="', max(0, og-300), og)
                    if cs == -1:
                        cs = article_html.find('content="', og)
                    cs += 9
                    ce = article_html.find('"', cs)
                    title = article_html[cs:ce].strip()
                if not title:
                    h1 = article_html.find("<h1")
                    if h1 != -1:
                        gt = article_html.find(">", h1)
                        end = article_html.find("</h1>", gt)
                        if gt != -1 and end != -1:
                            title = strip_html_tags(article_html[gt+1:end]).strip()
                
                start = article_html.find('id="content"')
                if start == -1:
                    continue
                body_start = article_html.find(">", start) + 1
                
                end_markers = [
                    'class="related', 'class="share', 'class="comments',
                    '<footer', 'class="newsletter', 'class="advertisement',
                    'class="author-bio',
                ]
                body_end = len(article_html)
                for em in end_markers:
                    pos = article_html.find(em, body_start)
                    if pos != -1 and pos < body_end:
                        body_end = pos
                if body_end - body_start > 50000:
                    body_end = body_start + 50000
                
                body = strip_html_tags(article_html[body_start:body_end])
                # v2: The Pig Site 광고/메타 정보 제거
                body = clean_thepigsite_body(body)
                
                if len(body) >= 300:
                    results.append({
                        "source": SITE_NAME,
                        "type": "full_body",
                        "url": url,
                        "title": title,
                        "body": body,
                        "scraped_at": datetime.now().isoformat(),
                    })
            except Exception as e:
                print(f"  [기사 추출 실패] {url[:60]}: {e}")
    except Exception as e:
        print(f"  [{SITE_NAME} 목록 페이지 오류] {e}")
    
    return results


# ──────────────────────────────────────────────────
# ⑥ Pig Progress (RSS description만)
# ──────────────────────────────────────────────────

def crawl_pigprogress(limit=5):
    SITE_NAME = "Pig Progress"
    RSS_URL = "https://www.pigprogress.net/feed/"
    
    results = []
    try:
        rss = fetch(RSS_URL)
        items = parse_rss_items(rss, limit=limit)
        
        for item in items:
            if len(item['description']) >= 100:
                results.append({
                    "source": SITE_NAME,
                    "type": "rss_summary",
                    "url": item['link'],
                    "title": item['title'],
                    "body": item['description'],
                    "pub_date": item['pub_date'],
                    "scraped_at": datetime.now().isoformat(),
                })
    except Exception as e:
        print(f"  [{SITE_NAME} RSS 오류] {e}")
    
    return results


# ──────────────────────────────────────────────────
# ⑦ National Hog Farmer (RSS 헤드라인만)
# ──────────────────────────────────────────────────

def crawl_nhf(limit=10):
    SITE_NAME = "National Hog Farmer"
    RSS_URL = "https://www.nationalhogfarmer.com/rss.xml"
    
    results = []
    try:
        rss = fetch(RSS_URL)
        items = parse_rss_items(rss, limit=limit)
        
        for item in items:
            results.append({
                "source": SITE_NAME,
                "type": "headline",
                "url": item['link'],
                "title": item['title'],
                "body": item['description'],
                "pub_date": item['pub_date'],
                "scraped_at": datetime.now().isoformat(),
            })
    except Exception as e:
        print(f"  [{SITE_NAME} RSS 오류] {e}")
    
    return results


# ──────────────────────────────────────────────────
# ⑧ pig333 (RSS + 본문 크롤링)
# ──────────────────────────────────────────────────

def crawl_pig333(limit=5):
    SITE_NAME = "pig333"
    RSS_URL = "https://www.pig333.com/rss/articles"
    
    results = []
    try:
        rss = fetch(RSS_URL)
        items = parse_rss_items(rss, limit=limit)
        
        for item in items:
            try:
                article_html = fetch(item['link'])
                
                art_start = article_html.find("<article")
                if art_start == -1:
                    continue
                art_end = article_html.find("</article>", art_start)
                if art_end == -1:
                    art_end = len(article_html)
                
                article_zone = article_html[art_start:art_end]
                
                paragraphs = []
                i = 0
                while True:
                    p_start = article_zone.find("<p", i)
                    if p_start == -1:
                        break
                    next_char = article_zone[p_start+2] if p_start+2 < len(article_zone) else ''
                    if next_char not in ('>', ' ', '\t'):
                        i = p_start + 1
                        continue
                    
                    gt = article_zone.find(">", p_start)
                    p_end = article_zone.find("</p>", gt)
                    if p_end == -1:
                        break
                    
                    chunk = article_zone[gt+1:p_end]
                    text = strip_html_tags(chunk).strip()
                    text = " ".join(text.split())
                    
                    if len(text) > 50:
                        paragraphs.append(text)
                    i = p_end + 4
                
                body = "\n\n".join(paragraphs)
                
                # "Access restricted to..." 메시지 제거
                body = re.sub(
                    r'Access restricted to.*?logged in\.?\s*$',
                    '',
                    body,
                    flags=re.DOTALL
                ).strip()
                
                if len(body) >= 300:
                    results.append({
                        "source": SITE_NAME,
                        "type": "full_body",
                        "url": item['link'],
                        "title": item['title'],
                        "body": body,
                        "pub_date": item['pub_date'],
                        "scraped_at": datetime.now().isoformat(),
                    })
            except Exception as e:
                print(f"  [기사 추출 실패] {item['link'][:60]}: {e}")
    except Exception as e:
        print(f"  [{SITE_NAME} RSS 오류] {e}")
    
    return results


# ──────────────────────────────────────────────────
# 통합 실행
# ──────────────────────────────────────────────────

CRAWLERS = {
    "thepigsite":  ("The Pig Site",          crawl_thepigsite,    5),
    "pigprogress": ("Pig Progress",          crawl_pigprogress,   5),
    "nhf":         ("National Hog Farmer",   crawl_nhf,          10),
    "pig333":      ("pig333",                crawl_pig333,        5),
}


def crawl_all():
    print(f"\n{'='*60}")
    print(f"  해외 양돈 뉴스 수집: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")
    
    all_articles = []
    for key, (name, func, limit) in CRAWLERS.items():
        print(f"\n[{name}] 수집 중... (limit={limit})")
        articles = func(limit=limit)
        print(f"  → {len(articles)}건 수집 완료")
        all_articles.extend(articles)
    
    print(f"\n{'='*60}")
    print(f"  총 {len(all_articles)}건 수집됨")
    print(f"{'='*60}\n")
    return all_articles


def save_to_file(articles, filename=None):
    if filename is None:
        filename = f"overseas_articles_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)
    print(f"  📁 저장: {filename}")
    return filename


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in CRAWLERS:
        key = sys.argv[1]
        name, func, limit = CRAWLERS[key]
        print(f"\n[{name}] 단독 테스트")
        print("=" * 60)
        articles = func(limit=3 if key != "nhf" else 5)
        for i, a in enumerate(articles, 1):
            print(f"\n--- 기사 {i} (type: {a['type']}) ---")
            print(f"제목: {a['title']}")
            print(f"URL: {a['url'][:80]}")
            print(f"본문 길이: {len(a['body'])}자")
            print(f"본문 처음 300자:\n{a['body'][:300]}")
            print(f"\n본문 마지막 150자:\n{a['body'][-150:]}")
        print(f"\n총 {len(articles)}건")
    else:
        articles = crawl_all()
        
        from collections import Counter
        source_counter = Counter(a['source'] for a in articles)
        type_counter = Counter(a['type'] for a in articles)
        
        print("\n[소스별 수집 현황]")
        for source, count in source_counter.items():
            print(f"  • {source}: {count}건")
        
        print("\n[타입별 수집 현황]")
        for type_name, count in type_counter.items():
            type_label = {
                "full_body":   "풀본문",
                "rss_summary": "RSS 요약",
                "headline":    "헤드라인 큐레이션",
            }.get(type_name, type_name)
            print(f"  • {type_label} ({type_name}): {count}건")
        
        if articles:
            for type_name in type_counter:
                lengths = [len(a['body']) for a in articles if a['type'] == type_name]
                if lengths:
                    print(f"\n[{type_name} 본문 길이]")
                    print(f"  • 최소: {min(lengths):,}자")
                    print(f"  • 최대: {max(lengths):,}자")
                    print(f"  • 평균: {sum(lengths)//len(lengths):,}자")
        
        if articles:
            save_to_file(articles)
        print()
