"""
Unsplash 이미지 검색 헬퍼
- 기사 제목 → Gemini로 영문 키워드 추출 → Unsplash 검색 → 랜덤 선택
"""
import os
import random
import urllib.request
import urllib.parse
import json


def get_keyword_from_title(title):
    """Gemini로 제목 → 영문 키워드 3단어 추출"""
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        return "pig farming agriculture"
    
    payload = {
        "model": "google/gemini-2.5-flash-lite",
        "max_tokens": 20,
        "messages": [
            {
                "role": "user",
                "content": f"다음 한국어 기사 제목을 보고 Unsplash 이미지 검색에 쓸 영문 키워드 2~3단어만 출력해. 설명 없이 키워드만.\n\n제목: {title}"
            }
        ]
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
        with urllib.request.urlopen(req, timeout=10) as res:
            result = json.loads(res.read().decode("utf-8"))
            keyword = result["choices"][0]["message"]["content"].strip()
            # 혹시 긴 응답 오면 첫 줄만
            keyword = keyword.split("\n")[0].strip()
            print(f"  🔑 키워드: {keyword}")
            return keyword
    except Exception as e:
        print(f"  ⚠️ 키워드 추출 실패: {e}")
        return "pig farming agriculture"


def get_image_for_article(category, title):
    """
    기사 제목으로 Unsplash 이미지 검색
    
    Returns:
        str: 이미지 URL 또는 None
    """
    api_key = os.getenv("UNSPLASH_API_KEY", "").strip()
    
    if not api_key:
        print("  ⚠️ UNSPLASH_API_KEY 환경변수 없음")
        return None
    
    # 제목에서 이모지 제거 후 키워드 추출
    query = get_keyword_from_title(title)
    
    try:
        url = f"https://api.unsplash.com/search/photos?query={urllib.parse.quote(query)}&per_page=10&orientation=landscape"
        
        req = urllib.request.Request(
            url,
            headers={'Authorization': f'Client-ID {api_key}'}
        )
        
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            
            if data.get('results') and len(data['results']) > 0:
                # 랜덤 선택
                photo = random.choice(data['results'])
                image_url = photo['urls']['regular']
                print(f"  ✅ Unsplash 이미지: {image_url[:50]}...")
                return image_url
        
        print("  ⚠️ Unsplash에서 이미지를 찾지 못함")
        return None
    
    except Exception as e:
        print(f"  ⚠️ Unsplash 이미지 검색 실패: {e}")
        return None
