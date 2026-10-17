"""智学网会话管理：cookie 优先，失效时让你在浏览器里自己登录。

设计
----
1. **cookie 有效** → 直接复用。不启动浏览器，不打扰任何人。
2. **cookie 失效** → 打开一个浏览器窗口，**你自己在里面输入账号密码登录**
   （有滑块就拖一下）。程序只在旁边等，登录成功后抓取 cookie 存下来。
3. 全流程中，**密码不经过本程序、不进终端、不落任何文件**。

为什么不用"终端输密码 + 脚本回填表单"
--------------------------------------
那是多此一举：密码要经过我们的进程，还得模拟输入（而模拟输入既不自然，
又要额外处理点击、节奏、滑块）。让用户在真实浏览器里自己登，反而：
  · 密码只进浏览器，不进我们的代码
  · 可以用密码管理器/自动填充
  · 滑块、验证码由用户自然完成，程序不需要任何模拟

为什么登录前必须加反检测参数
----------------------------
智学网登录接了极验(Geetest v4)。Playwright 默认带 `--enable-automation`，
页面里 `navigator.webdriver === true`，极验在**初始化阶段**就判定为自动化，
`/verify` 无条件返回 `forbidden`（页面显示「极验封禁 / Error code: 60500」）。
对照实验结论：

    反检测  拟人化   结果
    ─────────────────────────
      ✗      ✗     forbidden
      ✗      ✓     forbidden
      ✓      ✗     success   ← 拟人化并非必要
      ✓      ✓     success

所以唯一必要的开关就是 `--disable-blink-features=AutomationControlled`
（把 `navigator.webdriver` 变成 undefined）。这与鼠标轨迹、账号密码、IP 都无关。

判断登录是否真的完成
--------------------
不能用 `loginUserName` cookie 是否存在 —— 它在 SSO 完成**之前**就会被写入，
据此取到的 cookie 全是无效的。正确信号是 `/container/getCurrentUser`
返回非空 `result.role`（这正是 `zhixuewang.login_cookie()` 内部调用的接口）。

用法
----
    from zhixue_login import get_account

    account = get_account()          # 有缓存直接用；失效才弹浏览器让你登
    mark = account.get_self_mark(account.get_latest_exam())

命令行:
    ../venv/bin/python zhixue_login.py             # 确保有可用会话并验证查分
    ../venv/bin/python zhixue_login.py --status    # 只看状态，绝不开浏览器
    ../venv/bin/python zhixue_login.py --auto-only # 无人值守：失效直接报错
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from playwright.sync_api import BrowserContext, Playwright, sync_playwright

COOKIE_CACHE = HERE / "cookies.json"

LOGIN_URL = "https://www.zhixue.com/login.html"

REAL_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")

# 唯一必要的反检测开关：去掉 navigator.webdriver
STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--no-default-browser-check",
]

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
"""

CURRENT_USER_URL = "https://www.zhixue.com/container/getCurrentUser"

DEFAULT_LOGIN_TIMEOUT = 300.0   # 留足时间让用户从容输入账号密码 + 拖滑块


class LoginError(RuntimeError):
    """登录相关错误基类。"""


class CookieExpired(LoginError):
    """缓存 cookie 已失效，且当前不允许打开浏览器让用户登录。"""


class LoginTimeout(LoginError):
    """等待用户在浏览器里完成登录超时。"""


# ====================================================================
# 浏览器
# ====================================================================

def launch_context(playwright: Playwright, *, headless: bool = False,
                   channel: str | None = None) -> BrowserContext:
    """启动去掉自动化特征的浏览器上下文。"""
    kwargs: dict = {
        "headless": headless,
        "args": STEALTH_ARGS,
        "ignore_default_args": ["--enable-automation"],
    }
    if channel:
        kwargs["channel"] = channel

    browser = playwright.chromium.launch(**kwargs)
    context = browser.new_context(
        viewport={"width": 1280, "height": 860},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        user_agent=REAL_UA,
    )
    context.add_init_script(STEALTH_JS)
    return context


# ====================================================================
# 登录状态判定
# ====================================================================

def current_user(context: BrowserContext) -> dict | None:
    """用上下文 cookie 问 /container/getCurrentUser。

    这正是 zhixuewang.login_cookie() 内部调用的接口，因此能代表"登录是否真的完成"。
    """
    try:
        r = context.request.get(CURRENT_USER_URL, timeout=8000)
        return r.json()
    except Exception:  # noqa: BLE001
        return None


def is_logged_in(context: BrowserContext) -> bool:
    data = current_user(context)
    if not isinstance(data, dict):
        return False
    result = data.get("result")
    return bool(isinstance(result, dict) and result.get("role"))


def _context_cookies(context: BrowserContext) -> dict[str, str]:
    return {c["name"]: c["value"] for c in context.cookies()}


def cookies_alive(cookies: dict[str, str]) -> bool:
    """用给定 cookie 问一次 getCurrentUser，判断会话是否仍有效。"""
    if not cookies or "loginUserName" not in cookies:
        return False
    from zhixuewang.session import get_basic_session
    sess = get_basic_session()
    sess.cookies.update(cookies)
    try:
        r = sess.get(CURRENT_USER_URL, timeout=15)
        result = r.json().get("result")
        return bool(isinstance(result, dict) and result.get("role"))
    except Exception:  # noqa: BLE001
        return False


# ====================================================================
# 让用户在浏览器里自己登录
# ====================================================================

def manual_login(*, timeout: float = DEFAULT_LOGIN_TIMEOUT,
                 channel: str | None = None,
                 verbose: bool = True) -> dict[str, str]:
    """打开浏览器，请用户自己完成登录，然后抓取 cookie。

    程序在这里只做两件事：等、抓。不输入任何东西。

    Raises:
        LoginTimeout: 超时用户仍未登录成功
        LoginError: 浏览器被关闭
    """
    def say(msg: str) -> None:
        if verbose:
            print(msg, flush=True)

    print("\n" + "=" * 66)
    print("  需要登录智学网。")
    print()
    print("  已为你打开浏览器窗口，请直接在里面输入账号密码完成登录。")
    print("  如果出现极验滑块，拖一下即可。")
    print()
    print("  密码全程不经过本程序，也不会写入任何文件。")
    print(f"  登录成功后会立刻保存 cookie，之后长期复用。最多等 {timeout:.0f} 秒。")
    print("=" * 66 + "\n", flush=True)

    with sync_playwright() as p:
        context = launch_context(p, headless=False, channel=channel)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)

            deadline = time.time() + timeout
            started = time.time()
            last_heartbeat = 0.0
            cookies: dict[str, str] | None = None

            while time.time() < deadline:
                time.sleep(1.0)

                if is_logged_in(context):
                    say("\n[login] 检测到登录成功，正在保存会话…")
                    time.sleep(1.2)          # 让 cookie 落全
                    cookies = _context_cookies(context)
                    break

                # 浏览器被关掉就中止，不要空等
                try:
                    pages = context.pages
                except Exception:  # noqa: BLE001
                    pages = []
                if not pages:
                    raise LoginError("浏览器窗口已被关闭，登录中止")

                # 每 20 秒给个心跳，避免看起来像卡死
                elapsed = time.time() - started
                if elapsed - last_heartbeat >= 20:
                    last_heartbeat = elapsed
                    say(f"[login] 仍在等待登录…（已等待 {elapsed:.0f}s / {timeout:.0f}s）")

            if cookies is None:
                raise LoginTimeout(
                    f"等待 {timeout:.0f} 秒仍未检测到登录成功。"
                    "可加大 --timeout，或确认已在浏览器窗口里完成登录。"
                )
        finally:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass

    if "loginUserName" not in cookies:
        raise LoginError(
            "登录后未拿到 loginUserName cookie，zhixuewang 无法使用。"
            f"现有字段: {sorted(cookies)}"
        )
    return cookies


# ====================================================================
# cookie 缓存
# ====================================================================

def load_cached_cookies() -> dict[str, str]:
    if COOKIE_CACHE.exists():
        try:
            data = json.loads(COOKIE_CACHE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "loginUserName" in data:
                return data
        except Exception:  # noqa: BLE001
            pass
    return {}


def save_cookies(cookies: dict[str, str]) -> None:
    COOKIE_CACHE.write_text(json.dumps(cookies, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    COOKIE_CACHE.chmod(0o600)


def clear_cached_cookies() -> bool:
    """删除缓存的 cookie，强制下次重新登录。返回是否真的删掉了。"""
    if COOKIE_CACHE.exists():
        COOKIE_CACHE.unlink()
        return True
    return False


# ====================================================================
# 对外主接口
# ====================================================================

def ensure_cookies(*, allow_interactive: bool = True,
                   timeout: float = DEFAULT_LOGIN_TIMEOUT,
                   channel: str | None = None,
                   verbose: bool = True) -> dict[str, str]:
    """确保拿到一份可用的 cookie。

    顺序：
      1. 复用 `cookies.json`（若有效）—— 不启动浏览器、零人工
      2. 失效时打开浏览器，请用户自己登录一次

    Args:
        allow_interactive: 是否允许打开浏览器让用户登录。
                           False 时 cookie 失效会直接抛 CookieExpired（适合无人值守）。
        timeout: 等待用户完成登录的秒数。

    Raises:
        CookieExpired: cookie 失效且不允许人工介入
        LoginTimeout / LoginError: 登录未完成
    """
    def say(msg: str) -> None:
        if verbose:
            print(msg, flush=True)

    cached = load_cached_cookies()
    if cached:
        if cookies_alive(cached):
            say("[session] 缓存 cookie 仍然有效，直接复用（未启动浏览器、无需人工）")
            return cached
        say("[session] 缓存 cookie 已失效")
    else:
        say("[session] 没有缓存 cookie")

    if not allow_interactive:
        raise CookieExpired(
            "cookie 已失效，且当前为无人值守模式（不打开浏览器让用户登录）")

    cookies = manual_login(timeout=timeout, channel=channel, verbose=verbose)
    save_cookies(cookies)
    print("[session] 已保存 cookie，后续运行将直接复用，不再需要登录。", flush=True)
    return cookies


def get_account(**kwargs):
    """返回 zhixuewang 的 Account 对象（自动管理会话）。"""
    import zhixuewang

    cookies = ensure_cookies(**kwargs)
    return zhixuewang.login_cookie(cookies)


# ====================================================================
# CLI
# ====================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="智学网会话管理（cookie 优先；失效时请在浏览器里自己登录）")
    parser.add_argument("--status", action="store_true",
                        help="只检查 cookie 状态，绝不打开浏览器")
    parser.add_argument("--auto-only", action="store_true",
                        help="无人值守：cookie 失效直接报错，不打开浏览器")
    parser.add_argument("--timeout", type=float, default=DEFAULT_LOGIN_TIMEOUT,
                        help=f"等待完成登录的秒数，默认 {DEFAULT_LOGIN_TIMEOUT:.0f}")
    parser.add_argument("--channel", default=None, help="传 chrome 用真实 Chrome")
    args = parser.parse_args()

    if args.status:
        cached = load_cached_cookies()
        if not cached:
            print("cookie 状态: 无缓存（首次运行需要登录一次）")
            return 1
        if cookies_alive(cached):
            print(f"cookie 状态: 有效（{len(cached)} 个字段），无需任何操作")
            return 0
        print("cookie 状态: 已失效（下次运行需要在浏览器里登录一次）")
        return 1

    try:
        cookies = ensure_cookies(
            allow_interactive=not args.auto_only,
            timeout=args.timeout,
            channel=args.channel,
        )
    except (CookieExpired, LoginTimeout, LoginError) as exc:
        print(f"!! 失败: {type(exc).__name__}: {exc}")
        return 1

    import zhixuewang
    account = zhixuewang.login_cookie(cookies)
    print(f"[main] zhixuewang 接管成功: 角色={account.role.name} 用户名={account.username}")
    exam = account.get_latest_exam()
    print(f"[main] 最新考试: {exam.name}")
    for item in account.get_self_mark(exam):
        print(f"[main]   成绩: {item}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
