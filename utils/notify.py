"""
SMTP 邮件通知工具
需要配置环境密钥（GitHub Actions Secrets）：
  SMTP_HOST     如 smtp.qq.com
  SMTP_PORT     465（SSL）或 587（STARTTLS）
  SMTP_USER     发件邮箱
  SMTP_PASSWORD 邮箱授权码（不是登录密码）
  NOTIFY_EMAIL  收件邮箱（可选，默认同 SMTP_USER）
未配置时仅打印提示，不阻塞工作流。
"""
import os
import smtplib
from email.header import Header
from email.mime.text import MIMEText


def send_email(subject: str, body: str) -> bool:
    host = os.getenv("SMTP_HOST", "").strip()
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    if not (host and user and password):
        print("[notify] 未配置 SMTP_HOST/SMTP_USER/SMTP_PASSWORD，跳过邮件发送", flush=True)
        return False

    port = int(os.getenv("SMTP_PORT", "465").strip() or "465")
    to_addr = os.getenv("NOTIFY_EMAIL", "").strip() or user
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = user
    msg["To"] = to_addr

    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            server.starttls()
        server.login(user, password)
        server.sendmail(user, [to_addr], msg.as_string())
        server.quit()
        print(f"[notify] 邮件已发送至 {to_addr}: {subject}", flush=True)
        return True
    except Exception as e:
        print(f"[notify] 邮件发送失败: {e}", flush=True)
        return False


COOKIE_EXPIRED_SUBJECT = "【抖音续火花·紧急】Cookies 已过期，需要更新"


def build_cookie_expired_body(run_url: str = "") -> str:
    return (
        "抖音 Spark Flow 检测到登录已失效（cookies 过期或被风控拒绝），消息未发送。\n\n"
        "更新方法（任选其一）：\n"
        "1. 浏览器打开 https://creator.douyin.com/ 并登录 Sakuro_Mai，"
        "用 Cookie-Editor 扩展导出 cookies，更新仓库环境密钥 COOKIES_SAKURO_MAI"
        "（Settings → Secrets and variables → Actions → Environments → user-data）。\n"
        "2. 或在本机运行：node ~/.config/douyin_keepalive/keepalive.js，扫码后自动更新。\n\n"
        f"相关运行: {run_url or '见 GitHub Actions 页面'}\n"
    )
