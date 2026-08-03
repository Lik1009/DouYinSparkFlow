"""
发送结果 review 双重校验：
  - 校验主发送任务是否成功、是否真正发送了消息、有无超时/未登录异常
  - cookies 过期时发送专门邮件
  - 发现问题时 exit 1，供工作流自动重试一次后再报错
"""
import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.notify import COOKIE_EXPIRED_SUBJECT, build_cookie_expired_body, send_email


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--main-result", required=True, help="run-main 任务结果 success/failure")
    ap.add_argument("--logs-dir", required=True, help="run-logs artifact 解压目录")
    ap.add_argument("--expected", type=int, required=True, help="配置的目标好友总数")
    args = ap.parse_args()

    problems = []
    text = ""
    app_log = Path(args.logs_dir) / "app.log"
    skip_marker = Path(args.logs_dir) / "SKIP_ALREADY_SENT"
    if skip_marker.exists():
        print("[review] 检测到『今天已发送，跳过』标记，review 通过")
        sys.exit(0)
    if app_log.exists():
        text = app_log.read_text(encoding="utf-8", errors="ignore")

    if args.main_result != "success":
        problems.append(f"发送任务未成功（结果: {args.main_result}）")

    sent = len(re.findall(r"已向好友 .* 发送消息", text))
    if args.expected > 0:
        if "任务完成" not in text:
            problems.append("未检测到『任务完成』标记")
        if sent == 0:
            problems.append("实际发送数为 0，疑似异常")
    if "未登录" in text:
        problems.append("检测到未登录（cookies 已过期或无效）")
    if "Traceback" in text:
        problems.append("检测到异常堆栈")
    if "TimeoutError" in text:
        problems.append("检测到致命超时")
    if text.count("尝试失败") >= 3 or "重试 3 次后仍失败" in text:
        problems.append("页面元素多次重试仍失败")

    print(f"[review] 主任务结果: {args.main_result}，日志发送数: {sent}/{args.expected}")
    if args.expected > 0 and 0 < sent < args.expected:
        print(f"[review] 注意：发送数({sent})少于目标数({args.expected})，可能有部分好友未找到，请查看日志")
    if problems:
        print("[review] 发现问题：")
        for p in problems:
            print("  -", p)
    else:
        print("[review] 检查通过")

    if "未登录" in text:
        run_url = ""
        run_id = os.getenv("GITHUB_RUN_ID", "")
        if run_id:
            run_url = f"https://github.com/Luolingli/DouYinSparkFlow/actions/runs/{run_id}"
        send_email(COOKIE_EXPIRED_SUBJECT, build_cookie_expired_body(run_url))

    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
