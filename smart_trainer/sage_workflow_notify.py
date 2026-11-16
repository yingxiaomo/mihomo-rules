#!/usr/bin/env python3
"""Send a final Sage workflow status without changing job outcome."""
from __future__ import annotations

import argparse
import html
import json
import os
import time
import urllib.error
import urllib.request


def build_message(args):
    if args.status == "success":
        title = "✅ <b>Sage 四维先验流程成功</b>"
        details = [
            f"Release: <code>{html.escape(args.release_tag)}</code>",
            f"训练仓: <code>{html.escape(args.training_commit)}</code>",
            f"Consumer: <code>{html.escape(args.consumer_commit)}</code>",
        ]
        if args.bundle_sha:
            details.append(f"Bundle SHA256: <code>{html.escape(args.bundle_sha)}</code>")
    else:
        title = "❌ <b>Sage 四维先验流程失败</b>"
        details = [
            f"失败步骤: <code>{html.escape(args.failed_step or 'Unknown step')}</code>",
            f"训练仓: <code>{html.escape(args.training_commit)}</code>",
            f"Consumer: <code>{html.escape(args.consumer_commit)}</code>",
        ]
    if args.run_url:
        details.append(f'<a href="{html.escape(args.run_url, quote=True)}">查看 GitHub Actions</a>')
    return title + "\n" + "\n".join(details)


def send_message(text, retries=3):
    token = os.environ.get("TG_BOT_TOKEN", "")
    chat_id = os.environ.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("Telegram 未配置，跳过最终状态通知")
        return False
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = json.loads(response.read().decode("utf-8"))
                if response.status == 200 and body.get("ok") is True:
                    print("Telegram 最终状态通知已发送")
                    return True
                print(f"Telegram 最终状态通知被拒绝 (attempt {attempt}/{retries})")
        except Exception as exc:
            status = exc.code if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
            print(f"Telegram 最终状态通知失败: {status} (attempt {attempt}/{retries})")
        if attempt < retries:
            time.sleep(attempt)
    print("::warning::Telegram final notification was not delivered")
    return False


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", choices=("success", "failure"), required=True)
    parser.add_argument("--failed-step", default="")
    parser.add_argument("--run-url", default="")
    parser.add_argument("--release-tag", default="")
    parser.add_argument("--training-commit", required=True)
    parser.add_argument("--consumer-commit", required=True)
    parser.add_argument("--bundle-sha", default="")
    args = parser.parse_args(argv)
    send_message(build_message(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
