"""
Unsplash 이미지 검색 헬퍼
- 기사 제목 → Gemini로 영문 키워드 추출 → Unsplash 검색 → 랜덤 선택
- 폴백 체인: 1차(Gemini 키워드) → 2차(마지막 단어 제거) → 3차(또 제거) → 4차(단어 1개)
"""
import os
import random
import urllib.request
import urllib.parse
import json

# pig 계열 단어 목록 (랜덤 선택용)
PIG_WORDS = ["pig", "pork", "hog", "piglet"]

PROMPT = """다음 한국어 양돈 기사 제목을 보고 Unsplash 이미지 검색 키워드를 4~5단어로 만들어라.

Unsplash는 실제 사진가들이 찍은 사진 플랫폼이다. 사진에 실제로 보이는 것(피사체, 장소, 행위, 사물)을 기준으로 검색된다.

규칙:
1. 4~5단어로 출력할 것
2. 숫자, 연도, 국가명, 추상 개념(policy, index, population, crisis, opportunity 등)은 사용 금지
3. 영문으로만 출력, 설명 없이 키워드만

제목: {title}"""


def _random_pig_word():
    return random.choice(PIG_WORDS)


def _force_pig_word(keyword):
    """키워드에 pig 계열 단어가 없으면 랜덤으로 하나 앞에 추가"""
    if not any(w in keyword.lower() for w in PIG_WORDS):
        keyword = f"{_random_pig_word()} {keyword}"
    return keyword


def _search_unsplash(api_key, query):
    """Unsplash 검색 — 결과 있으면 랜덤 URL 반환, 없으면 None"""
    try:
        url = (f"https://api.unsplash.com/search/photos"
               f"?query={urllib.parse.quote(query)}&per_page=10&orientation=landscape")
        req = urllib.request.Request(
            url, headers={"Authorization": f"Client-ID {api_key}"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            results = data.get("results", [])
            if results:
                return random.choice(results)["urls"]["regular"]
    except Exception as e:
        print(f"  ⚠️ Unsplash 검색 오류 ({query}): {e}")
    return None


def get_keyword_from_title(title):
    """Gemini로 제목 → 영문 키워드 4~5단어 추출"""
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        return _force_pig_word("farm barn rural field")

    payload = {
        "model": "google/gemini-2.5-flash-lite",
        "max_tokens": 30,
        "messages": [{"role": "user", "content": PROMPT.format(title=title)}],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            result = json.loads(res.read().decode("utf-8"))
            keyword = result["choices"][0]["message"]["content"].strip().split("\n")[0].strip()
            keyword = _force_pig_word(keyword)
            print(f"  🔑 키워드: {keyword}")
            return keyword
    except Exception as e:
        print(f"  ⚠️ 키워드 추출 실패: {e}")
        return _force_pig_word("farm barn rural field")


def get_image_for_article(category, title):
    """
    기사 이미지 URL 반환 — 항상 이미지를 반환 (None 없음)

    폴백 체인:
      1차: Gemini 키워드 (4~5단어, pig 계열 강제 포함)
      2차: 1차에서 마지막 단어 제거
      3차: 2차에서 마지막 단어 제거
      4차: pig/pork/hog/piglet 중 랜덤 1단어
    """
    api_key = os.getenv("UNSPLASH_API_KEY", "").strip()
    if not api_key:
        print("  ⚠️ UNSPLASH_API_KEY 없음")
        return None

    # 1차: Gemini 키워드
    keywords = get_keyword_from_title(title).split()

    for step in range(len(keywords)):
        current = " ".join(keywords[:len(keywords) - step])
        # pig 계열 단어 보장
        if not any(w in current.lower() for w in PIG_WORDS):
            current = f"{_random_pig_word()} {current}"

        image_url = _search_unsplash(api_key, current)
        if image_url:
            label = ["1차", "2차", "3차"][min(step, 2)]
            print(f"  ✅ Unsplash 이미지 ({label}, '{current}'): {image_url[:50]}...")
            return image_url

        print(f"  🔄 {'1차' if step==0 else '2차' if step==1 else '3차'} 실패 → 단어 축소: '{current}'")

    # 4차: 단어 1개만
    final_word = _random_pig_word()
    image_url = _search_unsplash(api_key, final_word)
    if image_url:
        print(f"  ✅ Unsplash 이미지 (4차, '{final_word}'): {image_url[:50]}...")
        return image_url

    print("  ⚠️ 모든 폴백 실패")
    return None
