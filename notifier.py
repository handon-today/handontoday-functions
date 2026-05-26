"""
================================================================
  Slack 알림 모듈 — notifier.py
  v3.2.1
  notifier.py
================================================================

[역할]
  파이프라인 실행 결과를 Slack 채널로 전송
  
[환경변수]
  SLACK_WEBHOOK_URL  - Secret Manager에서 주입됨
"""

import os
import json
import urllib.request
import urllib.error


SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")


def _post_to_slack(payload):
    """Slack Incoming Webhook으로 POST"""
    if not SLACK_WEBHOOK_URL:
        print("  ⚠️ SLACK_WEBHOOK_URL이 설정되지 않음 (Slack 알림 스킵)")
        return False
    
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            SLACK_WEBHOOK_URL,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as res:
            response = res.read().decode("utf-8")
            if response.strip() == "ok":
                print("  ✅ Slack 알림 전송 성공")
                return True
            else:
                print(f"  ⚠️ Slack 응답: {response[:100]}")
                return False
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        print(f"  ❌ Slack HTTP 오류 {e.code}: {body[:200]}")
        return False
    except Exception as e:
        print(f"  ❌ Slack 알림 실패: {e}")
        return False


def send_pipeline_result(stats, success=True):
    """파이프라인 실행 결과 알림 (Block Kit 형식)"""
    if success:
        emoji = "🐷"
        status_text = "정상 완료"
        color_emoji = "✅"
    else:
        emoji = "⚠️"
        status_text = "오류 발생"
        color_emoji = "❌"
    
    # 헤더
    header_text = f"{emoji} 한돈투데이 파이프라인 — {status_text}"
    
    # 통계 섹션
    cost_krw = stats.get('total_cost_usd', 0) * 1400
    stats_text = (
        f"*{color_emoji} 실행 결과*\n"
        f"• 시작: {stats.get('started_at', '-')}\n"
        f"• 처리 시간: {stats.get('elapsed_seconds', 0)}초\n"
        f"\n"
        f"*📰 수집*\n"
        f"• 국내: {stats.get('korea_count', 0)}건\n"
        f"• 해외: {stats.get('overseas_count', 0)}건\n"
        f"\n"
        f"*✍️ 기사 생성*\n"
        f"• 생성: {stats.get('generated_count', 0)}건\n"
        f"• 검수 통과: {stats.get('passed_count', 0)}건\n"
        f"• 비용: ${stats.get('total_cost_usd', 0):.4f} (≈{cost_krw:.0f}원)"
    )
    
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": header_text, "emoji": True}
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": stats_text}
        },
    ]
    
    # GCS 링크 (있을 때만)
    gcs_urls = stats.get("gcs_urls", {})
    if gcs_urls:
        gcs_text = "*📁 결과 파일*\n"
        if "json" in gcs_urls:
            gcs_text += f"• JSON: `{gcs_urls['json']}`\n"
        if "markdown" in gcs_urls:
            gcs_text += f"• Markdown: `{gcs_urls['markdown']}`"
        
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": gcs_text}
        })
    
    # 오류 (있을 때만)
    errors = stats.get("errors", [])
    if errors:
        # 오류 메시지 너무 길면 자르기
        error_lines = []
        for e in errors[:3]:
            line = str(e)[:300]
            error_lines.append(f"• {line}")
        if len(errors) > 3:
            error_lines.append(f"... 외 {len(errors) - 3}건")
        
        error_text = "*⚠️ 오류 내역*\n" + "\n".join(error_lines)
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": error_text}
        })
    
    # Footer
    blocks.append({
        "type": "context",
        "elements": [
            {"type": "mrkdwn", "text": f"_{stats.get('finished_at', '-')} • Cloud Functions_"}
        ]
    })
    
    payload = {
        "text": header_text,  # 알림용 fallback 텍스트
        "blocks": blocks,
    }
    
    return _post_to_slack(payload)




def send_briefing_result(briefing_result):
    """일일 시황 브리핑 전용 Slack 알림 (06시에만 전송)"""
    success = briefing_result.get("success", False)
    cost_usd = briefing_result.get("cost_usd", 0)
    cost_krw = cost_usd * 1400
    title = briefing_result.get("title", "제목 없음")
    article_id = briefing_result.get("article_id", "-")
    error = briefing_result.get("error", "")

    if success:
        header = "🌅 한돈투데이 모닝 브리핑 — 생성 완료"
        body = (
            f"*✅ 브리핑 발행 완료*\n"
            f"• 제목: {title}\n"
            f"• 기사 ID: {article_id}\n"
            f"• 생성 비용: ${cost_usd:.4f} (≈{cost_krw:.0f}원)\n"
            f"• URL: https://handontoday.com/article/{article_id}-"
        )
        # slug가 있으면 URL 완성, 없으면 ID만
        slug = briefing_result.get("slug", "")
        if slug and article_id != "-":
            body += f"{slug}/"
        else:
            body = body.rstrip(f"{article_id}-")
            body += f"https://handontoday.com/article/{article_id}/ (slug 미생성)"
    else:
        header = "🌅 한돈투데이 모닝 브리핑 — ⚠️ 생성 실패"
        body = (
            f"*❌ 브리핑 생성 실패*\n"
            f"• 오류: {error[:300] if error else '알 수 없는 오류'}"
        )

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": header, "emoji": True}
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": body}
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": "_매일 오전 06:00 KST · Cloud Functions_"}
            ]
        }
    ]

    payload = {
        "text": header,
        "blocks": blocks,
    }

    return _post_to_slack(payload)

def send_simple_message(text):
    """간단한 텍스트 메시지 (디버그용)"""
    return _post_to_slack({"text": text})
