"""
GitHub Actions 定时续期脚本：
用当前 cookies 打开 www.douyin.com/chat，让页面心跳续期会话，
抓取最新 cookies 并通过 SPARKFLOW_PAT 回写到环境密钥 COOKIES_SAKURO_MAI。

若未配置 SPARKFLOW_PAT，则只检查会话有效性并跳过回写（日常发送不受影响）。
"""
import json
import os
import subprocess
import sys

from playwright.sync_api import sync_playwright

LOGIN_MARKERS = ["扫码登录", "验证码登录", "密码登录"]
TARGET = "https://www.douyin.com/chat"
REPO = "Luolingli/DouYinSparkFlow"
ENV_NAME = "user-data"
SECRET_NAME = "COOKIES_SAKURO_MAI"


def log(msg):
    print(f"[keepalive] {msg}", flush=True)


def normalize(cookies):
    out = []
    for c in cookies:
        expires = c.get("expires")
        out.append(
            {
                "name": c["name"],
                "value": c["value"],
                "domain": c["domain"],
                "path": c.get("path", "/"),
                "expires": int(expires) if isinstance(expires, (int, float)) and expires > 0 else -1,
                "httpOnly": bool(c.get("httpOnly")),
                "secure": bool(c.get("secure")),
            }
        )
    return out


def main():
    pat = os.getenv("SPARKFLOW_PAT", "").strip()
    raw = os.getenv(SECRET_NAME, "")
    if not raw:
        log(f"缺少 {SECRET_NAME} 密钥，无法续期")
        sys.exit(1)
    try:
        cookies = json.loads(raw)
    except Exception as e:
        log(f"解析 cookies 失败: {e}")
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        context.add_cookies(cookies)
        page = context.new_page()
        page.goto(TARGET, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(12000)
        body = ""
        try:
            body = page.locator("body").inner_text(timeout=8000)
        except Exception:
            pass
        if any(m in body for m in LOGIN_MARKERS):
            log("登录已失效，无法续期（需要重新导出 cookies 并更新密钥）")
            browser.close()
            sys.exit(1)
        page.wait_for_timeout(30000)  # 停留触发心跳/会话刷新
        fresh = normalize(context.cookies())
        browser.close()

    if len(fresh) < 10:
        log(f"cookies 数量异常({len(fresh)})，跳过更新")
        sys.exit(1)

    log(f"会话有效，捕获 {len(fresh)} 个 cookies")
    tmp = "/tmp/fresh_cookies.json"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(fresh, f, ensure_ascii=False)

    if not pat:
        log("未配置 SPARKFLOW_PAT，跳过密钥回写（会话已续期，但下次仍需原密钥）")
        return

    env = dict(os.environ)
    env["GH_TOKEN"] = pat
    subprocess.run(
        [
            "gh",
            "secret",
            "set",
            SECRET_NAME,
            "--env",
            ENV_NAME,
            "--repo",
            REPO,
            "--body-file",
            tmp,
        ],
        env=env,
        check=True,
    )
    log(f"已回写 {len(fresh)} 个 cookies 到 {ENV_NAME}/{SECRET_NAME}")


if __name__ == "__main__":
    main()
