"""Three bounded, synthetic email samples; no news fetch or AI calls."""
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
import json
import os
from pathlib import Path
import smtplib
import ssl
from urllib.request import Request, urlopen

UTC = timezone.utc
SLOTS = {
    '55 6 2 11 *': datetime(2026, 11, 2, 6, 55, tzinfo=UTC),
    '0 7 2 11 *': datetime(2026, 11, 2, 7, 0, tzinfo=UTC),
    '5 7 2 11 *': datetime(2026, 11, 2, 7, 5, tzinfo=UTC),
}

def planned_time(cron, now):
    planned = SLOTS[cron]
    if not planned <= now < planned + timedelta(hours=24):
        raise ValueError('Outside the one-day test window; no email sent')
    return planned

def main():
    started = datetime.now(UTC)
    planned = planned_time(os.environ['PROBE_CRON'], started)
    repository = os.environ['GITHUB_REPOSITORY']
    run_id = os.environ['GITHUB_RUN_ID']
    req = Request(f'https://api.github.com/repos/{repository}/actions/runs/{run_id}', headers={
        'Authorization': 'Bearer ' + os.environ['GITHUB_TOKEN'],
        'Accept': 'application/vnd.github+json',
    })
    with urlopen(req, timeout=30) as r:
        run = json.load(r)
    if run['event'] != 'schedule' or run['run_attempt'] != 1:
        raise ValueError('Only first scheduled attempts may send')
    created = datetime.fromisoformat(run['created_at'].replace('Z', '+00:00'))
    report = {
        'planned_utc': planned.isoformat(),
        'run_created_utc': created.isoformat(),
        'probe_started_utc': started.isoformat(),
        'schedule_delay_seconds': (created - planned).total_seconds(),
        'startup_seconds': (started - created).total_seconds(),
        'run_url': run['html_url'],
    }
    bj = timezone(timedelta(hours=8))
    address = os.environ['GMAIL_ADDRESS'].strip()
    if address != 'zeshentian1@gmail.com':
        raise ValueError('Unexpected test recipient')
    msg = EmailMessage()
    msg['From'] = address
    msg['To'] = address
    msg['Subject'] = '【定时测试】Horizon ' + planned.astimezone(bj).strftime('%m月%d日 %H:%M')
    msg['Date'] = format_datetime(datetime.now(UTC))
    msg['Message-ID'] = make_msgid(domain='horizon.invalid')
    report['message_id'] = msg['Message-ID']
    msg.set_content('\n'.join([
        '这是一封定时测试邮件，使用固定测试数据，不抓新闻、不调用 MiniMax。',
        '计划触发（北京时间）：' + planned.astimezone(bj).isoformat(),
        'GitHub 创建任务（北京时间）：' + created.astimezone(bj).isoformat(),
        '测试脚本开始（北京时间）：' + started.astimezone(bj).isoformat(),
        '调度延迟（秒）：' + str(report['schedule_delay_seconds']),
        '创建至脚本启动（秒）：' + str(report['startup_seconds']),
        '实际到达时间请以 Gmail 为准。',
        run['html_url'],
    ]))
    report['smtp_begin_utc'] = datetime.now(UTC).isoformat()
    # No automatic SMTP retry: an uncertain send must not duplicate a sample.
    with smtplib.SMTP_SSL('smtp.gmail.com', 465, context=ssl.create_default_context(), timeout=30) as smtp:
        smtp.login(address, os.environ['GMAIL_APP_PASSWORD'])
        refused = smtp.send_message(msg)
        if refused:
            raise RuntimeError('Recipient refused')
    report['smtp_accepted_utc'] = datetime.now(UTC).isoformat()
    output = Path('timing-report.json')
    output.write_text(json.dumps(report, indent=2) + '\n')
    with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a') as f:
        f.write('## Synthetic timing sample\n\n```json\n' + json.dumps(report, indent=2) + '\n```\n')
    print(json.dumps(report))

if __name__ == '__main__':
    main()
