"""
Unsplash 이미지 검색 헬퍼
"""
import os
import urllib.request
import urllib.parse
import json

def get_image_for_article(category, title):
    """
    기사 카테고리와 제목으로 Unsplash 이미지 검색
    
    Returns:
        str: 이미지 URL 또는 None
    """
    api_key = os.getenv("UNSPLASH_API_KEY", "")
    
    if not api_key:
        print("  ⚠️ UNSPLASH_API_KEY 환경변수 없음")
        return None
    
    # 카테고리별 키워드 매핑
    keywords = {
        '국내': 'korean pig farm agriculture',
        '글로벌': 'international livestock farming',
        '시장': 'market agriculture business chart',
        '정책': 'government agriculture policy',
        '기술': 'farm technology innovation'
    }
    
    # 쿼리 생성
    query = keywords.get(category, 'pig farming agriculture')
    
    try:
        # Unsplash API 호출
        url = f"https://api.unsplash.com/search/photos?query={urllib.parse.quote(query)}&per_page=1&orientation=landscape"
        
        req = urllib.request.Request(
            url,
            headers={'Authorization': f'Client-ID {api_key}'}
        )
        
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            
            if data.get('results') and len(data['results']) > 0:
                photo = data['results'][0]
                image_url = photo['urls']['regular']
                print(f"  ✅ Unsplash 이미지: {image_url[:50]}...")
                return image_url
        
        print("  ⚠️ Unsplash에서 이미지를 찾지 못함")
        return None
    
    except Exception as e:
        print(f"  ⚠️ Unsplash 이미지 검색 실패: {e}")
        return None
