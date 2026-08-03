import traceback
from utils.logger import setup_logger
from utils.config import get_config, get_userData
from utils import norm
from core.msg_builder import build_message
from core.browser import get_browser
from playwright.sync_api import Response
import time
import json
import os


config = get_config()
userData = get_userData()
logger = setup_logger(level=config.get("logLevel", "Info"))
matchMode = config.get("matchMode", "nickname")
userIDDict = {}

# www.douyin.com/chat 会话列表选择器
CONVERSATION_ITEM_SELECTOR = ".conversationConversationItemwrapper"
CONVERSATION_TITLE_SELECTOR = ".conversationConversationItemtitle"
CONVERSATION_LIST_SELECTOR = ".conversationConversationListwrapper"
CHAT_EDITOR_SELECTOR = ".messageEditorimChatEditorContainer"
CHAT_MESSAGE_BOX_SELECTOR = ".messageMessageBoxmessageBox"
CHAT_FROM_ME_SELECTOR = ".MessageItemTextisFromMe"
CHAT_BUBBLE_TEXT_SELECTOR = ".MessageItemTextbubbleTextContent"
CHAT_MESSAGE_TIME_SELECTOR = ".MessageBoxTimetimeLayout"

# 未登录时页面会出现的关键字
LOGIN_MARKERS = ["扫码登录", "验证码登录", "密码登录"]


def handle_response(response: Response):
    """
    监听会话列表接口，收集好友完整信息（short_id/unique_id/sec_uid/昵称/备注）
    """
    global userIDDict
    if "aweme/v1/web/im/user/info" in response.url:
        try:
            json_data = response.json()
            for item in json_data.get("data", []):
                short_id = item.get("short_id")
                unique_id = item.get("unique_id")
                sec_uid = item.get("sec_uid", "")
                uid = str(item.get("uid", ""))
                nickname = norm(item.get("nickname"))
                remark_name = norm(item.get("remark_name", nickname))
                info = [short_id, unique_id, sec_uid, nickname, remark_name, uid]
                # 用所有标识符建立索引：备注/昵称/uid/short_id/抖音号/sec_uid
                for key in {
                    remark_name,
                    nickname,
                    uid,
                    str(short_id) if short_id else "",
                    str(unique_id) if unique_id else "",
                    sec_uid,
                }:
                    if key:
                        userIDDict[key] = info
        except Exception as e:
            tb = traceback.extract_tb(e.__traceback__)
            last = tb[-1]
            print(f"解析响应失败: {e}")
            print(f"文件: {last.filename}, 行号: {last.lineno}, 函数: {last.name}")


def retry_operation(name, operation, retries=3, delay=2, *args, **kwargs):
    """
    通用的重试逻辑
    :param name: 操作名称（用于日志记录）
    :param operation: 要执行的异步操作
    :param retries: 最大重试次数
    :param delay: 每次重试之间的延迟（秒）
    """
    for attempt in range(retries):
        try:
            return operation(*args, **kwargs)
        except Exception as e:
            if attempt < retries - 1:
                logger.warning(f"{name} 失败，正在重试第 {attempt + 1} 次，错误：{e}")
                time.sleep(delay)
            else:
                logger.error(f"{name} 失败，已达到最大重试次数，错误：{e}")
                raise


def _snapshot_on_failure(page, username, stage):
    """失败时记录现场：当前 URL、页面标题、正文文本、截图（随 logs/ 一起上传）"""
    try:
        os.makedirs("logs/screenshots", exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in username)
        fname = f"logs/screenshots/{safe_name}_{stage}_{timestamp}.png"
        page.screenshot(path=fname)
        page_text = ""
        try:
            page_text = page.locator("body").inner_text(timeout=5000)[:2000]
        except Exception:
            pass
        txt_fname = f"logs/screenshots/{safe_name}_{stage}_{timestamp}.txt"
        with open(txt_fname, "w", encoding="utf-8") as f:
            f.write(f"URL: {page.url}\n标题: {page.title()}\n\n正文:\n{page_text}\n")
        preview = page_text.replace("\n", " | ")[:600]
        logger.warning(
            f"账号 {username} [{stage}] 现场快照已保存: URL={page.url}, "
            f"标题={page.title()}, 截图={fname}, 正文片段: {preview}"
        )
    except Exception as e:
        logger.warning(f"账号 {username} [{stage}] 保存现场快照失败: {e}")


def wait_and_click_with_retry(page, username, stage, selector, retries=3, timeout=30000, click=False):
    """等待元素（可选点击），失败时记录现场并刷新页面重试"""
    for attempt in range(1, retries + 1):
        try:
            page.wait_for_selector(selector, timeout=timeout)
            if click:
                page.locator(selector).click()
            return
        except Exception as e:
            _snapshot_on_failure(page, username, f"{stage}-第{attempt}次")
            logger.warning(
                f"账号 {username} [{stage}] 第 {attempt} 次尝试失败: {e}，刷新页面重试"
            )
            try:
                page.reload(wait_until="domcontentloaded")
            except Exception:
                pass
            time.sleep(2)
    _snapshot_on_failure(page, username, f"{stage}-重试耗尽")
    raise TimeoutError(f"账号 {username} [{stage}] 重试 {retries} 次后仍失败")


def check_target_name(targetName, targets):
    """检查会话标题是否命中目标：支持备注/昵称/抖音号/short_id 多种匹配"""
    targetName = norm(targetName)
    if targetName in userIDDict:
        matched = next((v for v in userIDDict[targetName] if v and v in targets), None)
        if matched is not None:
            return matched
    if targetName in targets:
        return targetName
    return None


def already_sent_today_in_chat(page):
    """打开会话后检查聊天面板：是否存在今天发送的火花消息（我发的 + 今日火花 + 时间非昨天/前天）"""
    try:
        boxes = page.locator(CHAT_MESSAGE_BOX_SELECTOR)
        n = boxes.count()
        for i in range(min(n, 30)):
            box = boxes.nth(i)
            if box.locator(CHAT_FROM_ME_SELECTOR).count() == 0:
                continue
            try:
                txt = box.locator(CHAT_BUBBLE_TEXT_SELECTOR).inner_text(timeout=1500)
            except Exception:
                continue
            if "今日火花" not in txt:
                continue
            t = ""
            try:
                t = box.locator(CHAT_MESSAGE_TIME_SELECTOR).inner_text(timeout=1500)
            except Exception:
                pass
            if "昨天" not in t and "前天" not in t and "/" not in t:
                return True
    except Exception:
        pass
    return False


def scroll_and_select_user(page, username, targets, stats):
    """在 www.douyin.com/chat 会话列表中滚动查找目标好友"""
    target_selector = CONVERSATION_ITEM_SELECTOR
    scrollable_friends_selector = CONVERSATION_LIST_SELECTOR

    logger.debug(f"账号 {username} 开始查找目标好友列表")
    logger.debug(f"账号 {username} 目标好友列表: {targets}")

    # 等待会话列表加载完成（带刷新重试）
    wait_and_click_with_retry(page, username, "会话列表", CONVERSATION_LIST_SELECTOR)
    if page.locator(CONVERSATION_ITEM_SELECTOR).count() == 0:
        logger.warning(f"账号 {username} 会话列表为空，无可发送对象")
        return

    # 等待会话标题从数字 ID 解析为昵称（最多约 60 秒），避免扫描抢跑
    for _ in range(30):
        try:
            visible = page.locator(CONVERSATION_ITEM_SELECTOR).all()[:15]
            titles = []
            for el in visible:
                try:
                    titles.append(norm(el.locator(CONVERSATION_TITLE_SELECTOR).inner_text()))
                except Exception:
                    pass
            if not titles:
                break
            numeric = sum(1 for t in titles if t.isdigit())
            if numeric == 0 or numeric <= max(1, len(titles) * 0.3):
                break
        except Exception:
            break
        time.sleep(2)

    found_targets = set()
    tried_ids = set()
    sent_targets = set()
    remaining_targets = set(targets)
    empty_scroll_count = 0
    MAX_EMPTY_SCROLLS = 10
    scan_deadline = time.monotonic() + 240  # 单账号扫描时间预算：最多 4 分钟

    while True:
        if time.monotonic() > scan_deadline:
            logger.warning(f"账号 {username} 扫描超过 4 分钟时间预算，停止搜索")
            if len(remaining_targets) > 0:
                logger.warning(f"账号 {username} 时间到，仍有以下好友未找到: {remaining_targets}")
            break

        target_elements = page.locator(target_selector).all()
        prev_found_count = len(found_targets)

        for element in target_elements:
            try:
                span = element.locator(CONVERSATION_TITLE_SELECTOR)
                targetName = span.inner_text()

                targetSymbol = check_target_name(targetName, targets)

                if targetSymbol:
                    if targetSymbol in sent_targets:
                        # 已发送过，防止重复发送
                        found_targets.add(targetName)
                        continue
                    # 打开会话，检查聊天面板里今天是否已发过火花消息（对方回复后也能识别）
                    element.click()
                    time.sleep(1.5)
                    if already_sent_today_in_chat(page):
                        stats["skipped"] += 1
                        logger.info(f"账号 {username} 好友 {targetName} 今天已发送过，跳过")
                        found_targets.add(targetName)
                        continue
                    sent_targets.add(targetSymbol)
                    logger.info(f"账号 {username} 命中目标好友 {targetName}")
                    yield targetSymbol

                    if targetSymbol in remaining_targets:
                        remaining_targets.remove(targetSymbol)
                    if len(remaining_targets) == 0:
                        logger.debug(f"账号 {username} 所有目标好友均已找到，停止搜索")
                        return
                    found_targets.add(targetName)
                    break
                if targetName.isdigit():
                    # 数字标题：用户信息尚未解析，本轮先跳过，等待后续轮次标题更新
                    continue
                if targetName in found_targets:
                    continue
                found_targets.add(targetName)
                logger.debug(f"账号 {username} 找到会话 {targetName}")
            except Exception as e:
                traceback.print_exc()
        else:
            new_found = len(found_targets) > prev_found_count
            if new_found:
                empty_scroll_count = 0
            else:
                empty_scroll_count += 1

            if empty_scroll_count >= MAX_EMPTY_SCROLLS:
                logger.warning(f"账号 {username} 连续 {MAX_EMPTY_SCROLLS} 次滚动未发现新会话，判定已到达底部")
                if len(remaining_targets) > 0:
                    logger.warning(f"账号 {username} 搜索结束，仍有以下好友未找到: {remaining_targets}")
                break

            scrollable_element = page.locator(scrollable_friends_selector).element_handle()
            if scrollable_element:
                scroll_top_before = page.evaluate("(element) => element.scrollTop", scrollable_element)
                page.evaluate("(element) => element.scrollTop += 800", scrollable_element)
                time.sleep(0.3)
                scroll_top_after = page.evaluate("(element) => element.scrollTop", scrollable_element)

                if scroll_top_before == scroll_top_after:
                    empty_scroll_count += 2
                    logger.debug(
                        f"账号 {username} scrollTop 未变化 ({scroll_top_before})，可能已到底 (空滚动计数: {empty_scroll_count}/{MAX_EMPTY_SCROLLS})"
                    )
                else:
                    logger.debug(
                        f"账号 {username} 滚动会话列表加载更多 (scrollTop: {scroll_top_before} -> {scroll_top_after})"
                    )
                time.sleep(1.5)
            else:
                logger.error(f"账号 {username} 未找到滚动容器，退出")
                break


def do_user_task(browser, username, cookies, targets):
    context = browser.new_context()
    context.set_default_navigation_timeout(config["browserTimeout"])
    context.set_default_timeout(config["browserTimeout"])

    page = context.new_page()
    page.on("response", handle_response)

    # 注入 Cookie
    context.add_cookies(cookies)

    # 打开抖音网页聊天页面
    retry_operation(
        "打开抖音网页聊天页面",
        page.goto,
        retries=config["taskRetryTimes"],
        delay=5,
        url="https://www.douyin.com/chat",
    )

    time.sleep(5)  # 等待页面加载，避免误判登录态

    # 快速检查登录态：未登录时页面会出现扫码/验证码登录入口
    try:
        body_text = page.locator("body").inner_text(timeout=5000)
        if any(m in body_text for m in LOGIN_MARKERS):
            _snapshot_on_failure(page, username, "未登录检测")
            raise RuntimeError(
                f"账号 {username} 未检测到登录态（页面显示登录入口），"
                "Cookies 可能已过期或被风控拒绝，请重新导出并更新 COOKIES_SAKURO_MAI 密钥"
            )
        logger.debug(f"账号 {username} 登录态检查通过")
    except RuntimeError:
        raise
    except Exception as e:
        logger.debug(f"账号 {username} 登录态检查跳过（{e}），交给后续选择器判断")

    logger.info(f"账号 {username} 开始执行消息任务")
    stats = {"sent": 0, "skipped": 0}
    for friend_name in scroll_and_select_user(page, username, targets, stats):
        stats["sent"] += 1
        if stats["sent"] > len(targets):
            logger.error(f"账号 {username} 发送次数({stats['sent']})超过目标数({len(targets)})，强制终止")
            _snapshot_on_failure(page, username, "发送次数异常")
            raise RuntimeError(f"账号 {username} 发送次数异常，强制终止")
        logger.debug(f"账号 {username} 已选中好友 {friend_name}，准备输入消息")
        chat_input_selector = CHAT_EDITOR_SELECTOR
        try:
            page.wait_for_selector(chat_input_selector, timeout=config["browserTimeout"])
        except Exception:
            _snapshot_on_failure(page, username, f"聊天输入框-{friend_name}")
            raise
        chat_input = page.locator(chat_input_selector)

        message = build_message()
        for line in message.split("\\n"):
            chat_input.type(line)
            if line != message.split("\\n")[-1]:
                chat_input.press("Shift+Enter")

        logger.info(f"账号 {username} 向好友 {friend_name} 输入消息: {message}")
        chat_input.press("Enter")
        time.sleep(2)
        _snapshot_on_failure(page, username, f"发送后-{friend_name}")
        logger.info(f"账号 {username} 已向好友 {friend_name} 发送消息")

    context.close()
    return stats


def runTasks():
    playwright, browser = get_browser()
    try:
        logger.info("开始执行任务")
        logger.debug("当前配置如下：")
        logger.debug(f"消息模板: {config.get('messageTemplate', '未找到消息模板')}")
        logger.debug(f"一言类型: {config['hitokotoTypes']}")
        for user in userData:
            logger.debug(f"用户: {user.get('username', '未知用户')}, 目标好友: {user['targets']}")

        for user in userData:
            cookies = user["cookies"]
            targets = user["targets"]
            username = user.get("username", "未知用户")
            logger.info(f"开始处理账号 {username}")
            stats = do_user_task(browser, username, cookies, targets)
            sent, skipped = stats["sent"], stats["skipped"]
            if sent == 0 and skipped == len(targets) and len(targets) > 0:
                logger.info(f"账号 {username} 今天已全部发送过（跳过 {skipped} 个），无需重复发送")
                logger.info(f"账号 {username} SKIP_ALL_ALREADY_SENT")
                continue
            logger.info(f"账号 {username} 任务完成，共发送 {sent} 条消息，跳过 {skipped} 条")
            # review 自检：配置了目标但一条都没发出去 → 判定失败，避免“静默没发”
            if targets and sent == 0:
                logger.error(
                    f"账号 {username} 配置了 {len(targets)} 个目标好友但发送数为 0，"
                    "判定为异常（请查看上方日志中未找到的好友列表）"
                )
                raise RuntimeError(f"账号 {username} 发送数为 0，未发出任何消息")
    finally:
        browser.close()
        playwright.stop()
