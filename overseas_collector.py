"""
================================================================
  해외 양돈 뉴스 수집 모듈 v3
  overseas_collector.py
================================================================

[v3 변경사항]
  - 아시아 소스 6개 추가:
      soozhu.com (중국), efeedlink.com (아시아 전역 영문),
      pasusart.com (태국), livestockemag.com (태국),
      nguoichannuoi.vn (베트남), nhachannuoi.vn (베트남)
  - crawl_all() 반환값 변경:
      기존: 리스트 []
      신규: {"asia": [...], "global": [...]}
  - 각 기사에 region 필드 추가 ('asia' | 'global')

[기존 v2 내용 유지]
  - The Pig Site: 본문 끝 'Our Partners' 광고 영역 자동 제거
  - 공통: 본문 끝 메타 정보 정리 강화
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
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.8,th;q=0.7,vi;q=0.6,zh;q=0.5",
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


def fetch(url, timeout=20):
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


def _make_article(url, title, body, source, region, article_type="full_body", pub_date=""):
    """기사 딕셔너리 공통 생성"""
    return {
        "source": source,
        "type": article_type,
        "region": region,          # 'asia' | 'global'
        "url": url,
        "title": title.strip(),
        "body": re.sub(r'\s+', ' ', body).strip(),
        "pub_date": pub_date,
        "scraped_at": datetime.now().isoformat(),
    }


def _extract_wp_body(html, min_length=200):
    """WordPress 기반 사이트 본문 추출 (공통)"""
    # entry-content → article → 전체 body 순으로 시도
    for selector in ['class="entry-content"', 'class="post-content"',
                     'class="article-content"', '<article']:
        start = html.find(selector)
        if start == -1:
            continue
        body_start = html.find(">", start) + 1
        # 종료 태그 탐색
        end_markers = ['class="comments', 'class="related', '<footer',
                       'class="sidebar', 'id="sidebar']
        body_end = len(html)
        for em in end_markers:
            pos = html.find(em, body_start)
            if pos != -1 and pos < body_end:
                body_end = pos
        body = strip_html_tags(html[body_start:min(body_end, body_start + 50000)])
        if len(body) >= min_length:
            return body
    return ""


# ──────────────────────────────────────────────────
# ① 아시아 소스 크롤러 (신규 추가)
# ──────────────────────────────────────────────────

# ─ 1. soozhu.com (搜猪网, 중국) ─────────────────

def crawl_soozhu(limit=5):
    SITE_NAME = "soozhu.com"
    BASE_URL = "https://www.soozhu.com"
    results = []
    try:
        list_html = fetch(f"{BASE_URL}/c/xinwen/")

        # 기사 URL 추출: /article/숫자/ 패턴
        urls = []
        seen = set()
        for m in re.finditer(r'href="(/article/\d+/?)"', list_html):
            full = BASE_URL + m.group(1)
            if full not in seen:
                urls.append(full)
                seen.add(full)
            if len(urls) >= limit * 3:
                break

        for url in urls:
            if len(results) >= limit:
                break
            try:
                html = fetch(url)
                # 제목
                title = ""
                for pat in [r'<h1[^>]*>(.+?)</h1>', r'property="og:title" content="([^"]+)"']:
                    m = re.search(pat, html, re.DOTALL)
                    if m:
                        title = strip_html_tags(m.group(1)).strip()
                        break
                # 본문
                body = _extract_wp_body(html, min_length=150)
                if not body:
                    # soozhu 전용: .article-content 또는 .content
                    for cls in ['class="article-content"', 'class="content"',
                                'class="news-content"']:
                        start = html.find(cls)
                        if start != -1:
                            body = strip_html_tags(html[start:start + 30000])
                            break

                if title and len(body) >= 150:
                    results.append(_make_article(url, title, body, SITE_NAME, "asia"))
            except Exception as e:
                print(f"  [soozhu 기사 오류] {url[:60]}: {e}")
    except Exception as e:
        print(f"  [soozhu 목록 오류] {e}")

    print(f"  soozhu: {len(results)}건")
    return results


# ─ 2. efeedlink.com (아시아 전역 영문) ──────────

def crawl_efeedlink(limit=5):
    SITE_NAME = "efeedlink.com"
    BASE_URL = "https://www.efeedlink.com"
    results = []
    try:
        list_html = fetch(f"{BASE_URL}/swine/")

        urls = []
        seen = set()
        for m in re.finditer(r'href="([^"]*?/contents/[^"]+\.html)"', list_html):
            href = m.group(1)
            full = href if href.startswith("http") else BASE_URL + href
            if full not in seen:
                urls.append(full)
                seen.add(full)
            if len(urls) >= limit * 3:
                break

        for url in urls:
            if len(results) >= limit:
                break
            try:
                html = fetch(url)
                # 제목
                title = ""
                m = re.search(r'<h1[^>]*>(.+?)</h1>', html, re.DOTALL)
                if m:
                    title = strip_html_tags(m.group(1)).strip()
                if not title:
                    m = re.search(r'property="og:title" content="([^"]+)"', html)
                    if m:
                        title = m.group(1).strip()
                # 본문
                body = _extract_wp_body(html, min_length=150)
                if not body:
                    for cls in ['class="news-content"', 'class="article-body"',
                                'id="content"']:
                        start = html.find(cls)
                        if start != -1:
                            body = strip_html_tags(html[start:start + 30000])
                            break

                if title and len(body) >= 150:
                    results.append(_make_article(url, title, body, SITE_NAME, "asia"))
            except Exception as e:
                print(f"  [efeedlink 기사 오류] {url[:60]}: {e}")
    except Exception as e:
        print(f"  [efeedlink 목록 오류] {e}")

    print(f"  efeedlink: {len(results)}건")
    return results


# ─ 3. pasusart.com (태국) ───────────────────────

def crawl_pasusart(limit=5):
    SITE_NAME = "pasusart.com"
    results = []
    try:
        # 돼지(สุกร/Pig) 카테고리
        cat_url = safe_url(
            "https://pasusart.com/category/"
            "\u0e2a\u0e38\u0e01\u0e23-pig/"      # สุกร-pig
        )
        list_html = fetch(cat_url)

        urls = []
        seen = set()
        # h2/h3 > a 패턴으로 기사 링크 추출
        for m in re.finditer(r'<h[23][^>]*>\s*<a\s+href="(https://pasusart\.com/[^"]+)"',
                             list_html):
            url = m.group(1)
            if url not in seen:
                urls.append(url)
                seen.add(url)
            if len(urls) >= limit * 3:
                break

        for url in urls:
            if len(results) >= limit:
                break
            try:
                html = fetch(url)
                # 제목
                title = ""
                m = re.search(r'<h1[^>]*class="[^"]*entry-title[^"]*"[^>]*>(.+?)</h1>',
                              html, re.DOTALL)
                if not m:
                    m = re.search(r'<h1[^>]*>(.+?)</h1>', html, re.DOTALL)
                if m:
                    title = strip_html_tags(m.group(1)).strip()
                # 본문
                body = _extract_wp_body(html, min_length=150)

                if title and len(body) >= 150:
                    results.append(_make_article(url, title, body, SITE_NAME, "asia"))
            except Exception as e:
                print(f"  [pasusart 기사 오류] {url[:60]}: {e}")
    except Exception as e:
        print(f"  [pasusart 목록 오류] {e}")

    print(f"  pasusart: {len(results)}건")
    return results


# ─ 4. livestockemag.com (태국) ──────────────────

def crawl_livestockemag(limit=5):
    SITE_NAME = "livestockemag.com"
    results = []
    try:
        # หมู/สุกร 카테고리
        cat_url = safe_url(
            "https://livestockemag.com/category/"
            "\u0e2b\u0e21\u0e39-\u0e2a\u0e38\u0e01\u0e23/"  # หมู-สุกร
        )
        list_html = fetch(cat_url)

        urls = []
        seen = set()
        for m in re.finditer(
            r'<h[23][^>]*>\s*<a\s+href="(https://livestockemag\.com/[^"]+)"',
            list_html
        ):
            url = m.group(1)
            if url not in seen:
                urls.append(url)
                seen.add(url)
            if len(urls) >= limit * 3:
                break

        for url in urls:
            if len(results) >= limit:
                break
            try:
                html = fetch(url)
                title = ""
                m = re.search(r'<h1[^>]*>(.+?)</h1>', html, re.DOTALL)
                if m:
                    title = strip_html_tags(m.group(1)).strip()
                body = _extract_wp_body(html, min_length=150)

                if title and len(body) >= 150:
                    results.append(_make_article(url, title, body, SITE_NAME, "asia"))
            except Exception as e:
                print(f"  [livestockemag 기사 오류] {url[:60]}: {e}")
    except Exception as e:
        print(f"  [livestockemag 목록 오류] {e}")

    print(f"  livestockemag: {len(results)}건")
    return results


# ─ 5. nguoichannuoi.vn (베트남) ─────────────────

def crawl_nguoichannuoi(limit=5):
    SITE_NAME = "nguoichannuoi.vn"
    BASE_URL = "https://nguoichannuoi.vn"
    results = []
    try:
        list_html = fetch(f"{BASE_URL}/tin-tuc-su-kien/chan-nuoi-trong-nuoc/")

        urls = []
        seen = set()
        for m in re.finditer(r'href="(https://nguoichannuoi\.vn/[^"]+/)"', list_html):
            url = m.group(1)
            # 카테고리 페이지 제외
            if url in seen or url.rstrip('/') == BASE_URL:
                continue
            if any(skip in url for skip in ['/category/', '/tag/', '/page/', '/author/']):
                continue
            urls.append(url)
            seen.add(url)
            if len(urls) >= limit * 3:
                break

        for url in urls:
            if len(results) >= limit:
                break
            try:
                html = fetch(url)
                title = ""
                m = re.search(r'<h1[^>]*>(.+?)</h1>', html, re.DOTALL)
                if m:
                    title = strip_html_tags(m.group(1)).strip()
                body = _extract_wp_body(html, min_length=150)

                if title and len(body) >= 150:
                    results.append(_make_article(url, title, body, SITE_NAME, "asia"))
            except Exception as e:
                print(f"  [nguoichannuoi 기사 오류] {url[:60]}: {e}")
    except Exception as e:
        print(f"  [nguoichannuoi 목록 오류] {e}")

    print(f"  nguoichannuoi: {len(results)}건")
    return results


# ─ 6. nhachannuoi.vn (베트남) ───────────────────

def crawl_nhachannuoi(limit=5):
    SITE_NAME = "nhachannuoi.vn"
    BASE_URL = "https://nhachannuoi.vn"
    results = []
    try:
        list_html = fetch(f"{BASE_URL}/chuyen-muc/tin-tuc/")

        urls = []
        seen = set()
        for m in re.finditer(r'href="(https://nhachannuoi\.vn/[^"]+/)"', list_html):
            url = m.group(1)
            if url in seen:
                continue
            if any(skip in url for skip in ['/chuyen-muc/', '/tag/', '/page/', '/author/']):
                continue
            urls.append(url)
            seen.add(url)
            if len(urls) >= limit * 3:
                break

        for url in urls:
            if len(results) >= limit:
                break
            try:
                html = fetch(url)
                title = ""
                m = re.search(r'<h1[^>]*>(.+?)</h1>', html, re.DOTALL)
                if m:
                    title = strip_html_tags(m.group(1)).strip()
                body = _extract_wp_body(html, min_length=150)

                if title and len(body) >= 150:
                    results.append(_make_article(url, title, body, SITE_NAME, "asia"))
            except Exception as e:
                print(f"  [nhachannuoi 기사 오류] {url[:60]}: {e}")
    except Exception as e:
        print(f"  [nhachannuoi 목록 오류] {e}")

    print(f"  nhachannuoi: {len(results)}건")
    return results


# ──────────────────────────────────────────────────
# ② 영어권 소스 크롤러 (기존 v2 그대로)
# ──────────────────────────────────────────────────

def clean_thepigsite_body(text):
    """The Pig Site 본문 끝 광고/메타 정보 제거"""
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
                body = clean_thepigsite_body(body)

                if len(body) >= 300:
                    results.append(_make_article(url, title, body, SITE_NAME, "global",
                                                 article_type="full_body"))
            except Exception as e:
                print(f"  [thepigsite 기사 오류] {url[:60]}: {e}")
    except Exception as e:
        print(f"  [thepigsite 목록 오류] {e}")

    print(f"  thepigsite: {len(results)}건")
    return results


def crawl_pigprogress(limit=5):
    SITE_NAME = "Pig Progress"
    RSS_URL = "https://www.pigprogress.net/feed/"

    results = []
    try:
        rss = fetch(RSS_URL)
        items = parse_rss_items(rss, limit=limit)

        for item in items:
            if len(item['description']) >= 100:
                results.append(_make_article(
                    item['link'], item['title'], item['description'],
                    SITE_NAME, "global",
                    article_type="rss_summary", pub_date=item['pub_date']
                ))
    except Exception as e:
        print(f"  [pigprogress RSS 오류] {e}")

    print(f"  pigprogress: {len(results)}건")
    return results


def crawl_nhf(limit=10):
    SITE_NAME = "National Hog Farmer"
    RSS_URL = "https://www.nationalhogfarmer.com/rss.xml"

    results = []
    try:
        rss = fetch(RSS_URL)
        items = parse_rss_items(rss, limit=limit)

        for item in items:
            results.append(_make_article(
                item['link'], item['title'], item['description'],
                SITE_NAME, "global",
                article_type="headline", pub_date=item['pub_date']
            ))
    except Exception as e:
        print(f"  [nhf RSS 오류] {e}")

    print(f"  nhf: {len(results)}건")
    return results


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
                body = re.sub(
                    r'Access restricted to.*?logged in\.?\s*$',
                    '', body, flags=re.DOTALL
                ).strip()

                if len(body) >= 300:
                    results.append(_make_article(
                        item['link'], item['title'], body,
                        SITE_NAME, "global",
                        article_type="full_body", pub_date=item['pub_date']
                    ))
            except Exception as e:
                print(f"  [pig333 기사 오류] {item['link'][:60]}: {e}")
    except Exception as e:
        print(f"  [pig333 RSS 오류] {e}")

    print(f"  pig333: {len(results)}건")
    return results


# ──────────────────────────────────────────────────
# 크롤러 목록
# ──────────────────────────────────────────────────

CRAWLERS_ASIA = {
    "soozhu":        ("soozhu.com",        crawl_soozhu,        5),
    "efeedlink":     ("efeedlink.com",     crawl_efeedlink,     5),
    "pasusart":      ("pasusart.com",      crawl_pasusart,      5),
    "livestockemag": ("livestockemag.com", crawl_livestockemag, 5),
    "nguoichannuoi": ("nguoichannuoi.vn",  crawl_nguoichannuoi, 5),
    "nhachannuoi":   ("nhachannuoi.vn",    crawl_nhachannuoi,   5),
}

CRAWLERS_GLOBAL = {
    "thepigsite":  ("The Pig Site",        crawl_thepigsite,  5),
    "pigprogress": ("Pig Progress",        crawl_pigprogress, 5),
    "nhf":         ("National Hog Farmer", crawl_nhf,        10),
    "pig333":      ("pig333",              crawl_pig333,      5),
}


# ──────────────────────────────────────────────────
# 통합 실행
# ──────────────────────────────────────────────────

def crawl_all():
    """
    전체 해외 소스 수집.

    반환값 (v3 변경):
        {
            "asia":   [...],   # 아시아 소스 기사 리스트
            "global": [...],   # 영어권 소스 기사 리스트
        }

    ※ 하위 호환: main.py에서 asia + global을 합쳐 all_raw로 사용
    """
    print(f"\n{'='*60}")
    print(f"  해외 양돈 뉴스 수집 v3: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    # ── 아시아 소스 ──
    print("\n[아시아 소스]")
    asia_articles = []
    for key, (name, func, limit) in CRAWLERS_ASIA.items():
        try:
            articles = func(limit=limit)
            asia_articles.extend(articles)
        except Exception as e:
            print(f"  [{name}] 실패: {e}")

    # ── 영어권 소스 ──
    print("\n[영어권 소스]")
    global_articles = []
    for key, (name, func, limit) in CRAWLERS_GLOBAL.items():
        try:
            articles = func(limit=limit)
            global_articles.extend(articles)
        except Exception as e:
            print(f"  [{name}] 실패: {e}")

    print(f"\n{'='*60}")
    print(f"  아시아 {len(asia_articles)}건 | 영어권 {len(global_articles)}건 "
          f"| 총 {len(asia_articles)+len(global_articles)}건")
    print(f"{'='*60}\n")

    return {
        "asia": asia_articles,
        "global": global_articles,
    }


def save_to_file(articles, filename=None):
    if filename is None:
        filename = f"overseas_articles_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)
    print(f"  📁 저장: {filename}")
    return filename


# ──────────────────────────────────────────────────
# CLI (테스트용)
# ──────────────────────────────────────────────────

if __name__ == "__main__":
    ALL_CRAWLERS = {**CRAWLERS_ASIA, **CRAWLERS_GLOBAL}

    if len(sys.argv) > 1 and sys.argv[1] in ALL_CRAWLERS:
        key = sys.argv[1]
        name, func, limit = ALL_CRAWLERS[key]
        print(f"\n[{name}] 단독 테스트")
        print("=" * 60)
        articles = func(limit=3)
        for i, a in enumerate(articles, 1):
            print(f"\n--- 기사 {i} (region: {a['region']}) ---")
            print(f"제목: {a['title']}")
            print(f"URL: {a['url'][:80]}")
            print(f"본문 길이: {len(a['body'])}자")
            print(f"본문 처음 300자:\n{a['body'][:300]}")
            print(f"\n본문 마지막 150자:\n{a['body'][-150:]}")
        print(f"\n총 {len(articles)}건")
    else:
        result = crawl_all()
        asia = result["asia"]
        glob = result["global"]

        print(f"\n[아시아 소스별]")
        from collections import Counter
        for src, cnt in Counter(a['source'] for a in asia).items():
            print(f"  • {src}: {cnt}건")

        print(f"\n[영어권 소스별]")
        for src, cnt in Counter(a['source'] for a in glob).items():
            print(f"  • {src}: {cnt}건")

        all_articles = asia + glob
        if all_articles:
            save_to_file(all_articles)
        print()
