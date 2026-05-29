
## v3.2.0 — 2026-05-24
**리비전**: handon-news-pipeline-00028-loq
**배포일**: 2026-05-24 새벽 (KST)

### 변경 내용
- `article_generator.py` 국내/글로벌 기사 비율 고정 (2:2 반반)
  - 기존: AI 매칭에 국내+해외 전체를 넘겨 글로벌 쏠림 발생
  - 변경: 국내 풀 / 해외 풀 분리 후 각각 독립 매칭
  - 국내 2쌍 + 글로벌 2쌍 = 항상 4건 생성
  - 국내 기사 부족 시 글로벌로 보충해서 합계 4건 유지
- `generate_article_from_pair()`: `_category_override` 우선 적용
  - 매칭 단계에서 명시한 카테고리가 기사 생성까지 정확히 전달됨

### 변경 파일
- `article_generator.py`
  - `generate_article_from_pair()` 상단 카테고리 결정 로직 수정
  - `run_pipeline_from_data()` 전체 재작성

### 백업
- `article_generator.py.bak` (v3.1.7 기준)

## v3.2.1 — 2026-05-24
**리비전**: handon-news-pipeline-00029-rid

### 변경 내용
- 브리핑 Slack 알림 별도 전송 (06시에만)
  - `notifier.py`: `send_briefing_result()` 함수 추가
  - 성공 시: 제목, 기사 ID, URL, 비용 표시
  - 실패 시: 오류 내용 표시
- `daily_briefing.py`: 성공 반환값에 `title`, `slug` 추가 (URL 완성용)
- `main.py`: 예외 발생 시에도 실패 알림 전송되도록 보강

### 변경 파일
- `notifier.py` — `send_briefing_result()` 추가
- `daily_briefing.py` — 반환값 `title`, `slug` 추가
- `main.py` — 예외 처리 보강

## v3.2.2 — 2026-05-24
**리비전**: handon-news-pipeline-00030-cuj

### 변경 내용
- 데일리 브리핑 모바일 가독성 개선 (iPhone 15 Pro 기준)
  - viewport 메타태그 추가 (모바일 스케일링 정상화)
  - 전체 폰트 사이즈 +3~4px 업스케일

### 변경 파일
- `daily_briefing.py` — CSS 폰트 사이즈 전체 업스케일 + viewport 추가

## v3.2.3 — 2026-05-24
**리비전**: (korea_crawler 수정만, 별도 배포 없음)

### 변경 내용
- `korea_crawler.py`: 각 기사에 `source_type="korea"` 필드 추가

## v3.2.4 — 2026-05-24
**리비전**: handon-news-pipeline-00031-lof

### 변경 내용
- `article_generator.py`: 글로벌 매칭을 아시아 1쌍 + 영어권 1쌍으로 분리
- `main.py`: 중복 풀 구성 제거, overseas_result 직접 전달

### 변경 파일
- `article_generator.py` — 글로벌 매칭 아시아/영어권 분리
- `main.py` — overseas_result 직접 전달

## v3.2.5 — 2026-05-24
**리비전**: handon-news-pipeline-00032-raf

### 변경 내용
- 돈가 DB 연동 완료 (ekape API 완전 대체)
  - `dong_price` 테이블 생성
  - `korea_crawler.py`: pigpeople.net 기사에서 돈가 파싱 → DB 저장
  - `daily_briefing.py`: DB에서 조회

### 변경 파일
- `korea_crawler.py` — parse_dongga_from_articles(), save_dongga_to_db() 추가
- `daily_briefing.py` — _fetch_dongga() DB 조회로 교체
- `main.py` — 돈가 파싱 + 저장 단계 추가

## v3.3.0 — 2026-05-25
**리비전**: handon-news-pipeline-00037-boc ~ 00038-gab

### 변경 내용
- `daily_briefing.py`: 브리핑 UI 대폭 개선
  - 시장 지표 소제목 4섹션 추가 (국내증시/해외증시/사료·선물/환율)
  - 돈가 섹션 소제목 추가
  - 헤더 텍스트 수정: "HANDON TODAY · 06:00 KST" → "매일 아침 오전 6시 · 하루를 시작하는 뉴스"
  - 돈가 레이블 중복 수정 ("작년 동일 (작년 동월 평균)" → "작년 동월 평균")

### 변경 파일
- `daily_briefing.py` — 소제목, 헤더, 레이블 수정

## v3.3.1 — 2026-05-25
**리비전**: handon-news-pipeline-00039-gus

### 변경 내용
- `unsplash_helper.py` 완전 재작성
  - 프롬프트 개선: 2~3단어 → 4~5단어, Unsplash 촬영 가능 장면 기반
  - pig/pork/hog/piglet 중 랜덤 강제 포함
  - 폴백 체인 4단계: Gemini키워드 → 마지막단어제거 → 또제거 → 단어1개
  - Unsplash timeout 10초 → 5초

### 변경 파일
- `unsplash_helper.py` — 전면 재작성

## v3.3.2 — 2026-05-25
**리비전**: handon-news-pipeline-00040-loh

### 변경 내용
- `article_generator.py`: 이미지 병렬처리 버그 수정
  - 기존: run_pipeline()에만 ThreadPoolExecutor 있었음
  - 수정: main.py가 호출하는 run_pipeline_from_data()에 올바르게 추가
  - generate_article_from_pair()에서 image_url 호출 제거 → None으로
- `db_manager.py`: slug 중복 충돌 처리
  - 23505 에러 시 uuid suffix 추가 후 최대 3회 재시도

### 변경 파일
- `article_generator.py` — 이미지 병렬처리 올바른 함수에 적용
- `db_manager.py` — slug 중복 uuid 재시도

## v3.3.3 — 2026-05-25
**리비전**: handon-news-pipeline-00041-quc

### 변경 내용
- `daily_briefing.py`: 브리핑 썸네일 image_url 추가
  - INSERT 쿼리에 image_url 컬럼 추가
  - 브리핑 전용 커버 이미지 고정: https://handontoday.com/static/images/briefing_cover.png

### 변경 파일
- `daily_briefing.py` — image_url INSERT 추가

## v3.3.4 — 2026-05-26
**리비전**: handon-news-pipeline-00043-web ~ 00044-pal

### 변경 내용
- `daily_briefing.py`: 브리핑 이미지 절대 URL 수정
  - 기존: /static/images/briefing_cover.png (상대경로)
  - 수정: https://handontoday.com/static/images/briefing_cover.png (절대 URL)
- `korea_crawler.py`: 돈가 파싱 패턴 개선
  - 모든 소스(돼지와사람, 한돈뉴스 등)에서 파싱 시도
  - 우선순위: 돼지와사람 → 한돈뉴스 → 나머지
  - 1차 단위 패턴(kg당, /㎏) + 2차 맥락 패턴(앞뒤 50자 돼지 키워드 필수)
  - 오탐 방지: 원선/원대/평균가격 패턴 제거, 한우/닭 오탐 차단

### 변경 파일
- `daily_briefing.py` — 절대 URL 수정
- `korea_crawler.py` — 돈가 파싱 패턴 개선

## v3.4.0 — 2026-05-26
**리비전**: handon-news-pipeline-00045-kix ~ 00046-jej

### 변경 내용
- `daily_briefing.py`: 돈가 실시간 검색으로 전환 (가장 큰 변경)
  - 기존: 뉴스 기사 파싱 → 매일 실패
  - 변경: Perplexity Sonar 실시간 웹 검색 (제주·등외 제외 기준 명시)
  - 검색 성공 시 dong_price 테이블에 자동 축적 (1년 후 일별 비교 가능)
  - 작년 동월 평균은 DB에서 조회 유지
- `dong_price` DB 기준값 교체
  - 기존: mtrace.go.kr 전체 등급 (2025년 5월: 5,204원)
  - 변경: pigpeople.net 전광판 기준, 제주·등외 제외 (2025년 5월: 5,812원)
  - 2025년 1~12월 전체 월별 평균값 재입력
- `daily_briefing.py`: BRIEFING_COVER SQL 안에 들어간 버그 수정
  - image_url params 딕셔너리에 누락된 값 추가

### 변경 파일
- `daily_briefing.py` — Perplexity 돈가 검색, 버그 수정
- `dong_price` DB — 기준값 전면 교체 (pigpeople.net 기준)


## v3.4.1 — 2026-05-29
**리비전**: handon-news-pipeline-00052-rel

### 변경 내용
- `daily_briefing.py`: max_tokens 800→2000 (JSON 잘림 방지)
- `daily_briefing.py`: 입력 기사 수 30→15건 제한 (토큰 절약)
- `daily_briefing.py`: slugify ImportError 처리 추가 (Cloud Shell 호환)
- `daily_briefing.py`: error 키 반환 추가 (Slack "알 수 없는 오류" 해소)
- `daily_briefing.py`: category 국내→시황으로 변경
- `daily_briefing.py`: 버전 v1.0.0→v1.1.0

### 변경 파일
- `daily_briefing.py`

## v3.4.2 — 2026-05-29
**리비전**: handon-news-pipeline-00053~

### 변경 내용
- `daily_briefing.py`: 전일 돈가 비교 N/A 수정
  - 기존: chg/chg_pct 하드코딩 "N/A"
  - 변경: dong_price DB에서 day_before 조회 → 실제 전일대비 표시
  - v1.1.0 → v1.2.0

### 변경 파일
- `daily_briefing.py`
