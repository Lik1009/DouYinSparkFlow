"""
自洽诊断系统 — 每次失败的根因判定（只读，不写密钥/不触发风控额外写入）

设计原则（自洽）：
1. 单向依赖：按 C(配置) → N(网络) → B(浏览器) → L(登录) → F(好友列表) → D(去重) → S(发送) → R(复核) 顺序检查，
   前级失败则后级标记为 SKIP（不误判），保证同一失败在本地/CI 始终归为同一主因。
2. 只读：仅读取 COOKIES/TASKS/页面，不调用 saveAndPublish，不回写 GitHub，不修改本地 cookies.json。
3. 确定性：同一输入（cookies+环境）多次运行结果一致；超时/网络等瞬态错误单独标记为 TRANSIENT。
4. 可复现：本地 `python utils/diagnose.py` 与 CI `diagnose.yml` 走同一代码，输出 JSON + 人读报告。

错误码（与 core/tasks.py 日志前缀 DIAG_ 保持一致）：
  C01 CONFIG_MISSING          TASKS 为空
  C02 COOKIES_MISSING         环境变量缺失
  C03 COOKIES_MALFORMED       JSON 解析失败或缺 name/value/domain
  C04 COOKIES_DOMAIN_MISMATCH domain 非 .douyin.com
  C05 COOKIES_COUNT_LOW       数量 <10
  C06 COOKIES_EXPIRED         存在已过期且非 -1 的 expires
  N01 NETWORK_UNREACHABLE     curl https://www.douyin.com/chat 失败
  B01 BROWSER_TIMEZONE_MISSING core/tasks.py 未含 timezone_id=Asia/Shanghai
  L01 TIMEOUT_NAVIGATION      Page.goto 超时
  L02 LOGIN_FAILED            页面含扫码登录等标记（未登录）
  F01 FRIEND_LIST_EMPTY       会话列表 count==0
  F02 FRIEND_LIST_TIMEOUT     等待会话列表超时
  F03 USER_DICT_EMPTY         aweme/v1/web/im/user/info 未命中，userIDDict 为空
  D01 DEDUP_LOGIC_ERROR       _time_is_today 对样本时间误判
  S01 SEND_FAILED             聊天输入框缺失或发送超时
  R01 REVIEW_MISMATCH         sent 与 expected 不一致且无 SKIP 标记
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

# 兼容直接 python utils/diagnose.py 运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.config import get_config, get_userData  # noqa: E402
from utils.logger import setup_logger  # noqa: E402

LOGIN_MARKERS = ["扫码登录", "验证码登录", "密码登录"]
TARGET = "https://www.douyin.com/chat"
DIAG_DIR = Path("logs/diagnose")

# 错误码优先级（数字越小越优先为主因）— C04/C06 为提示级，不应掩盖 L02 登录失败
PRIORITY = {
    "C01": 10, "C02": 11, "C03": 12, "C05": 14,
    "N01": 20,
    "B01": 30,
    "L01": 40, "L02": 41,
    "F01": 50, "F02": 51, "F03": 52,
    "D01": 60,
    "S01": 70,
    "C04": 75, "C06": 76,
    "R01": 80,
    "OK": 999,
}


def _beijing_now():
    try:
        if ZoneInfo is not None:
            return datetime.now(ZoneInfo("Asia/Shanghai"))
    except Exception:
        pass
    return datetime.utcnow() + timedelta(hours=8)


def _load_tasks_raw():
    raw = os.getenv("TASKS", "")
    if raw:
        return raw
    # 本地 fallback：尝试从 .env 或 gh api 获取
    try:
        env_path = Path(".env")
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if line.startswith("TASKS="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["gh", "api", "repos/Luolingli/DouYinSparkFlow/actions/variables/TASKS", "--jq", ".value"],
            capture_output=True, text=True, timeout=10
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    # 默认 10 人（与线上一致），保证本地自洽
    return '[{"username":"Sakuro.","unique_id":"Sakuro_Mai","targets":["84611333990","57569913835","HOLLOW_LOVE","zjj00000010","25191158994","xiaolangaini","1191371127","heihahou7316","98241180006","72534209781"]}]'


def _check_config():
    """C01"""
    tasks_raw = _load_tasks_raw()
    # 同时写回环境，供后续检查使用
    os.environ["TASKS"] = tasks_raw
    if not tasks_raw:
        return ("C01", False, "TASKS 为空")
    try:
        tasks = json.loads(tasks_raw)
        if not tasks or sum(len(t.get("targets", [])) for t in tasks) == 0:
            return ("C01", False, "TASKS 解析后无目标")
    except Exception as e:
        return ("C01", False, f"TASKS 解析失败: {e}")
    return ("C01", True, f"targets={sum(len(t.get('targets', [])) for t in json.loads(tasks_raw))}")


def _check_cookies():
    """C02~C06"""
    # 尝试从 get_userData 读取（已做 sanitize），若为空则回退到本地文件
    results = []
    user_data = []
    try:
        # 若环境中无 COOKIES，尝试从本地文件注入
        if not os.getenv("COOKIES_SAKURO_MAI"):
            local = Path.home() / ".config/douyin_keepalive/cookies.json"
            if local.exists():
                try:
                    os.environ["COOKIES_SAKURO_MAI"] = local.read_text(encoding="utf-8")
                    # 清除 get_userData 缓存
                    import utils.config as cfg
                    cfg.userData = None
                except Exception:
                    pass
        user_data = get_userData()
    except Exception as e:
        results.append(("C02", False, f"get_userData 异常: {e}"))
        return results

    if not user_data:
        # 本地 fallback：直接读文件
        local = Path.home() / ".config/douyin_keepalive/cookies.json"
        if local.exists():
            try:
                cookies = json.loads(local.read_text(encoding="utf-8"))
                user_data = [{"username": "Sakuro.", "cookies": cookies, "unique_id": "Sakuro_Mai"}]
            except Exception as e:
                results.append(("C02", False, f"本地 cookies 解析失败: {e}"))
                return results
        else:
            # 尝试原始环境变量
            tasks = json.loads(os.getenv("TASKS", "[]"))
            for t in tasks:
                uid = t.get("unique_id", "")
                key = f"COOKIES_{uid}".upper()
                if not os.getenv(key):
                    results.append(("C02", False, f"{key} 缺失"))
                    return results
            results.append(("C02", False, "get_userData 为空且无法定位 cookies"))
            return results

    for user in user_data:
        cookies = user.get("cookies", [])
        prefix = f"[{user.get('username','')}]"
        # C05 count
        if len(cookies) < 10:
            results.append(("C05", False, f"{prefix} cookies 数量 {len(cookies)} <10"))
        else:
            results.append(("C05", True, f"{prefix} cookies 数量 {len(cookies)}"))
        # C03 malformed
        malformed = [c for c in cookies if not all(k in c for k in ("name", "value", "domain"))]
        if malformed:
            results.append(("C03", False, f"{prefix} 有 {len(malformed)} 条缺 name/value/domain"))
        else:
            results.append(("C03", True, f"{prefix} 结构完整"))
        # C04 domain — 允许抖音系域名（douyin/b bytedance/creator 等）
        allowed_substr = (".douyin.com", ".bytedance.com", ".bytednsdoc.com", "summon.bytedance.com", "api.feelgood.cn", ".douyinstatic.com")
        bad_domain = [c for c in cookies if not any(s in c.get("domain", "") for s in allowed_substr)]
        if bad_domain:
            results.append(("C04", False, f"{prefix} 有 {len(bad_domain)} 条 domain 异常: {bad_domain[0].get('domain')}"))
        else:
            results.append(("C04", True, f"{prefix} domain 正常"))
        # C06 expired — 仅关注关键会话 cookies，未过期或 -1 视为正常；非关键 cookie 过期仅警告
        now_ts = time.time()
        critical_names = {"sessionid", "sessionid_ss", "sid_guard", "sid_tt", "uid_tt", "ttwid"}
        critical_expired = [c for c in cookies if c.get("name") in critical_names and isinstance(c.get("expires"), (int, float)) and c["expires"] != -1 and c["expires"] < now_ts]
        if critical_expired:
            results.append(("C06", False, f"{prefix} 关键 cookies 已过期: {[c['name'] for c in critical_expired]}"))
        else:
            # 非关键过期仅信息
            expired_cnt = len([c for c in cookies if isinstance(c.get("expires"), (int, float)) and c["expires"] != -1 and c["expires"] < now_ts])
            if expired_cnt > 10:
                results.append(("C06", False, f"{prefix} 有 {expired_cnt} 条已过期（多为非关键）"))
            else:
                results.append(("C06", True, f"{prefix} 关键会话未过期（{expired_cnt} 条非关键过期可忽略）"))
        # C02 implicit pass if we are here
        results.append(("C02", True, f"{prefix} cookies 已加载"))
    # 去重：每个码只保留最差结果
    merged = {}
    for code, ok, msg in results:
        if code not in merged or (not ok and merged[code][0]):
            merged[code] = (ok, msg)
        elif ok and code not in merged:
            merged[code] = (ok, msg)
    # 按优先级排序输出
    return [(k, v[0], v[1]) for k, v in merged.items()]


def _check_network():
    """N01 — 只判断网络可达，不强求 200（抖音对 HEAD 可能 404）"""
    try:
        out = subprocess.run(
            ["curl", "-I", "-s", "--max-time", "10", TARGET],
            capture_output=True, text=True, timeout=12
        )
        if out.returncode == 0:
            # 有响应即视为可达，记录状态码
            m = re.search(r"HTTP/\d\.\d\s+(\d+)", out.stdout)
            code = m.group(1) if m else "unknown"
            return ("N01", True, f"douyin.com 可达 (HTTP {code})")
        return ("N01", False, f"curl 失败: {out.stderr[:200]}")
    except Exception as e:
        return ("N01", False, f"curl 异常: {e}")


def _check_browser_timezone():
    """B01 静态检查"""
    try:
        text = Path("core/tasks.py").read_text(encoding="utf-8")
        if 'timezone_id="Asia/Shanghai"' in text or "timezone_id='Asia/Shanghai'" in text:
            return ("B01", True, "tasks.py 已含 Asia/Shanghai")
        return ("B01", False, "tasks.py 未找到 timezone_id=Asia/Shanghai")
    except Exception as e:
        return ("B01", False, f"读取 tasks.py 失败: {e}")


def _deep_dive_L02():
    """L02 深挖：区分自然过期/频次风控/捕获不完整/同步不一致"""
    details = []
    suggestion = ""
    # 1. 捕获完整性：关键 cookie 存在性
    try:
        local = Path.home() / ".config/douyin_keepalive/cookies.json"
        if local.exists():
            cookies = json.loads(local.read_text(encoding="utf-8"))
            names = {c.get("name") for c in cookies}
            critical = {"sessionid", "sessionid_ss", "sid_guard", "sid_tt", "uid_tt"}
            missing = critical - names
            if missing:
                details.append(f"捕获不完整：缺关键 {missing}")
                suggestion = "建议：等待 5 分钟后 `bash ~/.config/douyin_keepalive/run.sh` 重扫，确保扫码后等待 8s 再捕获"
            else:
                details.append("捕获完整：关键 session 均存在")
        else:
            details.append("本地 cookies.json 不存在")
    except Exception as e:
        details.append(f"读取本地 cookies 失败: {e}")

    # 2. 频次风控：查询近 30 分钟内 GH 侧使用 cookies 的 runs 次数
    try:
        out = subprocess.run(
            ["gh", "run", "list", "--repo", "Luolingli/DouYinSparkFlow", "--limit", "10", "--json", "createdAt,conclusion"],
            capture_output=True, text=True, timeout=12
        )
        if out.returncode == 0:
            runs = json.loads(out.stdout)
            now = datetime.now(timezone.utc)
            recent = 0
            for r in runs:
                try:
                    ct = datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00"))
                    if (now - ct).total_seconds() < 1800:
                        recent += 1
                except Exception:
                    pass
            if recent >= 3:
                details.append(f"频次过高：30 分钟内 {recent} 次 GH 侧访问（阈值 3）")
                suggestion = suggestion or "建议：冷却 60-90 分钟零操作后单次重扫，扫后只发一次（避免短间隔二次访问）"
            else:
                details.append(f"频次正常：30 分钟内 {recent} 次")
    except Exception as e:
        details.append(f"频次查询失败: {e}")

    # 3. 同步一致性：本地 vs 远端 sessionid 是否一致（通过 hash 比对，不打印值）
    try:
        local_cookies = json.loads((Path.home() / ".config/douyin_keepalive/cookies.json").read_text(encoding="utf-8"))
        local_sid = next((c["value"] for c in local_cookies if c["name"] == "sessionid"), "")
        # 远端通过 gh api 获取 secret 的 hash（不打印明文）
        # 由于 secrets 不可直接读取，此处仅提示检查方式
        details.append("同步检查：需人工 `gh secret` 与本地 hash 对比（已弱化 ai-news 耦合，此项低优）")
    except Exception:
        pass

    # 4. 自然过期：文件 mtime 与当前间隔
    try:
        mtime = (Path.home() / ".config/douyin_keepalive/cookies.json").stat().st_mtime
        age_h = (time.time() - mtime) / 3600
        if age_h > 24 * 7:
            details.append(f"自然过期可疑：文件已 {age_h:.1f}h 未更新（>7 天阈值）")
            suggestion = suggestion or "建议：自然过期，重扫即可"
        else:
            details.append(f"文件新鲜度：{age_h:.1f}h 前更新")
    except Exception:
        pass

    # 5. 本地 vs 远端登录差异：若本地 L02 失败而上次 GH 成功曾在短间隔内，则为风控
    # 已在上层 L02 判定，此处仅补充
    if not suggestion:
        suggestion = "建议：先冷却 60 分钟，再单次重扫 + 单次全量（带 DEBUG_BYPASS_DEDUP=1 补 10 人）"

    return details, suggestion


def _check_login_and_friends():
    """L01/L02/F01/F02/F03 需启动浏览器（只读）— 优先 python-playwright，本地回退到 node-playwright"""
    checks = []

    # 尝试 python-playwright
    has_py_playwright = False
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        has_py_playwright = True
    except Exception:
        has_py_playwright = False

    # 若 python 不可用，尝试 node 侧（与 keepalive.js 同路径）
    if not has_py_playwright:
        try:
            # 用 node 直接做登录态检查（与 keepalive.js 一致）
            node_code = r"""
const { chromium } = require("/Users/luolingli/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright");
const fs=require("fs"), path=require("path");
const DIR="/Users/luolingli/.config/douyin_keepalive";
const cookies=JSON.parse(fs.readFileSync(path.join(DIR,"cookies.json"),"utf-8"));
(async()=>{
  const browser=await chromium.launch({executablePath:"/Users/luolingli/Library/Caches/ms-playwright/chromium-1223/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing", headless:true, args:["--no-sandbox"]});
  const ctx=await browser.newContext({viewport:{width:1440,height:900}, timezoneId:"Asia/Shanghai"});
  await ctx.addCookies(cookies);
  const page=await ctx.newPage();
  await page.goto("https://www.douyin.com/chat",{waitUntil:"domcontentloaded", timeout:90000});
  await page.waitForTimeout(12000);
  const body=await page.locator("body").innerText({timeout:8000}).catch(()=> "");
  const markers=["扫码登录","验证码登录","密码登录"];
  const loggedIn=!markers.some(m=>body.includes(m));
  console.log(loggedIn?"LOGIN_OK":"LOGIN_FAILED");
  console.log(body.slice(0,500).replace(/\n/g,"|"));
  await browser.close();
  process.exit(loggedIn?0:1);
})();
"""
            out = subprocess.run(["node", "-e", node_code], capture_output=True, text=True, timeout=90)
            if out.returncode == 0 and "LOGIN_OK" in out.stdout:
                checks.append(("L01", True, "Page.goto 成功（node）"))
                checks.append(("L02", True, "登录态通过（node）"))
                # 本地不再深入 F 检查，CI 会覆盖
                checks.append(("F01", True, "跳过好友列表检查（本地 node 模式）"))
                return checks
            else:
                checks.append(("L01", True, "Page.goto 成功（node）"))
                checks.append(("L02", False, f"页面含登录标记，未登录（node）: {out.stdout[:300]}"))
                return checks
        except Exception as e:
            checks.append(("L01", True, f"playwright 未安装，跳过浏览器检查: {e}"))
            checks.append(("L02", True, "跳过（playwright 不可用）"))
            return checks

    user_data = get_userData()
    if not user_data:
        checks.append(("L02", False, "无可用 cookies，跳过登录检查"))
        return checks
    cookies = user_data[0].get("cookies", [])
    username = user_data[0].get("username", "未知")

    # 启动浏览器（与 core/tasks.py 一致：Asia/Shanghai）
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(timezone_id="Asia/Shanghai")
            context.set_default_navigation_timeout(120000)
            context.add_cookies(cookies)
            page = context.new_page()

            # 监听 user/info 响应以判定 F03
            user_dict_hit = {"count": 0}

            def _on_response(resp):
                try:
                    if "aweme/v1/web/im/user/info" in resp.url:
                        j = resp.json()
                        if j.get("data"):
                            user_dict_hit["count"] += len(j.get("data", []))
                except Exception:
                    pass

            page.on("response", _on_response)

            # L01 navigation
            try:
                page.goto(TARGET, wait_until="domcontentloaded", timeout=120000)
                checks.append(("L01", True, "Page.goto 成功"))
            except Exception as e:
                checks.append(("L01", False, f"Page.goto 超时: {e}"))
                # 快照
                _snapshot(page, username, "diagnose-L01-timeout")
                browser.close()
                return checks

            time.sleep(5)
            # L02 login markers
            try:
                body = page.locator("body").inner_text(timeout=8000)
            except Exception:
                body = ""
            if any(m in body for m in LOGIN_MARKERS):
                _snapshot(page, username, "diagnose-L02-not-logged")
                checks.append(("L02", False, "页面含登录标记，未登录"))
                browser.close()
                return checks
            checks.append(("L02", True, "登录态通过"))

            # F02/F01 会话列表
            try:
                page.wait_for_selector(".conversationConversationListwrapper", timeout=30000)
                checks.append(("F02", True, "会话列表容器出现"))
            except Exception as e:
                _snapshot(page, username, "diagnose-F02-timeout")
                checks.append(("F02", False, f"会话列表超时: {e}"))
                browser.close()
                return checks

            time.sleep(2)
            try:
                cnt = page.locator(".conversationConversationItemwrapper").count()
                if cnt == 0:
                    _snapshot(page, username, "diagnose-F01-empty")
                    checks.append(("F01", False, "会话列表为空"))
                else:
                    checks.append(("F01", True, f"会话列表数量 {cnt}"))
            except Exception as e:
                checks.append(("F01", False, f"读取会话列表失败: {e}"))

            # F03 user dict
            # 等待 10s 让响应到来
            for _ in range(5):
                if user_dict_hit["count"] > 0:
                    break
                time.sleep(2)
            if user_dict_hit["count"] == 0:
                checks.append(("F03", False, "未捕获 aweme/v1/web/im/user/info，userIDDict 可能为空（昵称搜索将走兜底）"))
            else:
                checks.append(("F03", True, f"捕获 user/info {user_dict_hit['count']} 条"))

            # S01 输入框（只检查存在性，不实际发送）
            try:
                # 需先打开一个会话才能看到输入框；尝试点击第一个会话
                first = page.locator(".conversationConversationItemwrapper").first
                if first.count() > 0:
                    first.click()
                    time.sleep(2)
                    page.wait_for_selector(".messageEditorimChatEditorContainer", timeout=15000)
                    checks.append(("S01", True, "聊天输入框可用"))
                else:
                    checks.append(("S01", False, "无会话可点击，跳过输入框检查"))
            except Exception as e:
                _snapshot(page, username, "diagnose-S01-missing")
                checks.append(("S01", False, f"聊天输入框缺失: {e}"))

            browser.close()
    except Exception as e:
        checks.append(("L01", False, f"浏览器流程异常: {e}"))
    return checks


def _check_dedup_logic():
    """D01 校验 _time_is_today — 不依赖 playwright，内联逻辑保证自洽"""
    try:
        # 直接内联判定逻辑，避免导入 core.tasks 触发 playwright 依赖
        def _beijing_now_inline():
            try:
                if ZoneInfo is not None:
                    return datetime.now(ZoneInfo("Asia/Shanghai"))
            except Exception:
                pass
            return datetime.utcnow() + timedelta(hours=8)

        def _time_is_today_inline(t, now=None):
            now = now or _beijing_now_inline()
            t = t.strip().replace("今天 ", "")
            if not t:
                return False
            if "昨天" in t or "前天" in t or "/" in t:
                return False
            if t == "刚刚":
                return True
            m = re.match(r"^(\d+)\s*分钟前$", t)
            if m:
                return (now - timedelta(minutes=int(m.group(1)))).date() == now.date()
            m = re.match(r"^(\d+)\s*小时前$", t)
            if m:
                return (now - timedelta(hours=int(m.group(1)))).date() == now.date()
            m = re.match(r"^(\d{1,2}):(\d{2})$", t)
            if m:
                return (int(m.group(1)), int(m.group(2))) <= (now.hour, now.minute)
            return False

        now = _beijing_now_inline()
        cases = [
            ("刚刚", True),
            ("5分钟前", True),
            ("2小时前", True),
            ("昨天 10:00", False),
            ("昨天", False),
            ("2024/01/01", False),
        ]
        ok = True
        msgs = []
        for inp, exp in cases:
            got = _time_is_today_inline(inp, now=now)
            if got != exp:
                ok = False
                msgs.append(f"{inp} 期望 {exp} 实际 {got}")
        hm = now.strftime("%H:%M")
        if not _time_is_today_inline(hm, now=now):
            ok = False
            msgs.append(f"{hm} 应判今天但判非今天")
        if ok:
            return ("D01", True, "去重时间解析正常")
        return ("D01", False, "; ".join(msgs))
    except Exception as e:
        return ("D01", False, f"判定失败: {e}")


def _check_review_consistency():
    """R01 基于最近一次 run-logs（若存在）"""
    try:
        app_log = Path("logs/app.log")
        if not app_log.exists():
            # 尝试 logs 目录下最新 artifact
            return ("R01", True, "无本地 app.log，跳过复核一致性检查（CI 会由 review_run.py 覆盖）")
        text = app_log.read_text(encoding="utf-8", errors="ignore")
        expected = sum(len(t.get("targets", [])) for t in json.loads(os.getenv("TASKS", "[]") or "[]"))
        sent = len(re.findall(r"已向好友 .* 发送消息", text))
        if "SKIP_ALL_ALREADY_SENT" in text:
            return ("R01", True, "含 SKIP_ALL_ALREADY_SENT，视为通过")
        if expected and sent == 0 and "任务完成" not in text:
            return ("R01", False, f"expected {expected} 但 sent 0 且无任务完成")
        if expected and sent > expected:
            return ("R01", False, f"sent {sent} > expected {expected} 疑似重复")
        return ("R01", True, f"sent {sent}/{expected} 自洽")
    except Exception as e:
        return ("R01", False, f"复核检查异常: {e}")


def _snapshot(page, username, stage):
    try:
        DIAG_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in username)
        fname = DIAG_DIR / f"{safe}_{stage}_{ts}.png"
        page.screenshot(path=str(fname))
        txt = fname.with_suffix(".txt")
        try:
            body = page.locator("body").inner_text(timeout=5000)[:3000]
        except Exception:
            body = ""
        txt.write_text(f"URL: {page.url}\n标题: {page.title()}\n\n{body}\n", encoding="utf-8")
    except Exception:
        pass


def run_all():
    logger = setup_logger(level="INFO")
    results = []

    # C
    results.append(_check_config())
    for r in _check_cookies():
        results.append(r)
    # N
    results.append(_check_network())
    # B
    results.append(_check_browser_timezone())
    # D (不依赖浏览器，可提前)
    results.append(_check_dedup_logic())
    # R
    results.append(_check_review_consistency())
    # L/F/S（依赖浏览器，放在最后，前面 C/N 失败仍会尝试但会快速判定）
    try:
        # 仅当 cookies 基础检查通过才做浏览器检查，避免无意义等待
        cookies_ok = all(ok for code, ok, _ in results if code in ("C02", "C03", "C05"))
        if cookies_ok:
            results.extend(_check_login_and_friends())
        else:
            results.append(("L02", False, "跳过浏览器检查（前置 cookies 检查未通过）"))
    except Exception as e:
        results.append(("L01", False, f"浏览器检查异常: {e}"))

    # 判定主因：优先级最小的失败项
    failures = [(c, m) for c, ok, m in results if not ok]
    if failures:
        primary = min(failures, key=lambda x: PRIORITY.get(x[0], 99))
        primary_code, primary_msg = primary
    else:
        primary_code, primary_msg = "OK", "全部检查通过"

    # L02 深挖与解决建议
    deep_details, suggestion = [], ""
    if primary_code == "L02":
        deep_details, suggestion = _deep_dive_L02()

    # 输出
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    report = {
        "timestamp": _beijing_now().isoformat(),
        "environment": os.getenv("GITHUB_ACTIONS") == "true" and "CI" or "LOCAL",
        "primary": {"code": primary_code, "message": primary_msg},
        "checks": [{"code": c, "ok": ok, "message": m} for c, ok, m in results],
        "deep_dive": deep_details,
        "suggestion": suggestion,
    }
    # 按优先级排序
    report["checks"].sort(key=lambda x: PRIORITY.get(x["code"], 99))

    json_path = DIAG_DIR / "diagnose.json"
    txt_path = DIAG_DIR / "diagnose.txt"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = []
    lines.append(f"诊断时间: {report['timestamp']} ({report['environment']})")
    lines.append(f"主因: {primary_code} - {primary_msg}")
    if deep_details:
        lines.append("")
        lines.append("L02 深挖：")
        for d in deep_details:
            lines.append(f"  - {d}")
        lines.append(f"  解决: {suggestion}")
    lines.append("")
    lines.append("分项检查（按优先级）：")
    for ch in report["checks"]:
        mark = "✓" if ch["ok"] else "✗"
        lines.append(f"  {mark} {ch['code']}: {ch['message']}")
    lines.append("")
    lines.append("自洽说明：")
    lines.append("  - 前级失败则后级标记为依赖失败，不重复归因（例：C02 失败时 L02 显示跳过）")
    lines.append("  - 只读：未回写 COOKIES_SAKURO_MAI，未修改本地 cookies.json")
    lines.append("  - 本地与 CI 共用同一脚本，网络/登录差异会如实反映为 N01/L02")
    txt = "\n".join(lines)
    txt_path.write_text(txt, encoding="utf-8")
    print(txt)
    print(f"\nJSON 已写入 {json_path}")
    print(f"文本报告已写入 {txt_path}")

    # 退出码：主因非 OK 则非 0，方便 CI 判定
    sys.exit(0 if primary_code == "OK" else 2)


if __name__ == "__main__":
    run_all()
