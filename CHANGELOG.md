
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
    - 헤더 eye: 10→13px, 제목: 16→20px, 부제: 12→15px
    - 지표 이름/값: 13→15/16px, 등락: 11→14px, %: 10→13px
    - 돈가 레이블: 10→13px, 값: 14→17px
    - 뉴스 카테고리: 10→13px, 제목: 13→16px, 설명: 11→14px
    - 포인트/요약: 12→15px, 한줄요약: 12→15px
    - byline/note: 10→13px

### 변경 파일
- `daily_briefing.py` — CSS 폰트 사이즈 전체 업스케일 + viewport 추가

## v3.2.3 — 2026-05-24
**리비전**: (korea_crawler 수정만, 별도 배포 없음)

### 변경 내용
- `korea_crawler.py`: 각 기사에 `source_type="korea"` 필드 추가
  - article_generator 내부 국내/글로벌 분리 로직의 핵심 수정

## v3.2.4 — 2026-05-24
**리비전**: handon-news-pipeline-00031-lof

### 변경 내용
- `article_generator.py`: 글로벌 매칭을 아시아 1쌍 + 영어권 1쌍으로 분리
  - 기존: 해외 풀 전체를 AI에 넘겨 글로벌 2쌍 생성
  - 변경: 아시아 풀 따로, 영어권 풀 따로 매칭 → 균형 보장
- `main.py`: 중복 풀 구성 제거
  - 기존: main.py에서 overseas_pool 구성 후 article_generator에 전달
  - 변경: overseas_result(asia/global 분리된 원본)를 직접 전달
  - article_generator 내부에서 국내/아시아/영어권 3분리 매칭

### 변경 파일
- `article_generator.py` — 글로벌 매칭 아시아/영어권 분리
- `main.py` — overseas_result 직접 전달

## v3.2.5 — 2026-05-24
**리비전**: handon-news-pipeline-00032-raf

### 변경 내용
- 돈가 DB 연동 완료 (ekape API 완전 대체)
  - `dong_price` 테이블 생성 (date, price, source)
  - `korea_crawler.py`: pigpeople.net 기사에서 돈가 파싱 → DB 저장
  - `daily_briefing.py`: ekape API 대신 DB에서 조회
    - 오늘 돈가: 어제 날짜 DB 조회
    - 전년 비교: 전년 동월 평균 조회
  - `main.py`: 국내 크롤링 후 돈가 파싱 + DB 저장 추가

### 변경 파일
- `korea_crawler.py` — parse_dongga_from_articles(), save_dongga_to_db() 추가
- `daily_briefing.py` — _fetch_dongga() DB 조회로 교체, collect_market_data() engine 전달
- `main.py` — 돈가 파싱 + 저장 단계 추가 (Step 1.5)
