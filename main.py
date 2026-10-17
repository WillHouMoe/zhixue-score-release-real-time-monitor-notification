#!/usr/bin/env python3
"""智学网成绩发布实时监控 & 通知

相比旧版的变化（只改了"获取信息"的部分，监控/通知逻辑保持不变）：

  旧版：把 curl 抓到的 examId 和一堆鉴权 header 硬编码在文件里，
        XToken 一过期就得重新抓包改代码。

  新版：
    第一步 setup    复用登录会话，把账号身份用**非敏感信息**（用户名/姓名/
                    学校班级）打印出来让你确认；cookie 过期会打开浏览器，
                    请你在里面自己登录（密码不经过本程序）。
    第二步 选考试   抓取最近考试列表让你选，并把选择按**考试名称**保存下来。
                    考试链接里的 examId 会变，名字相对稳定，所以下次运行优先
                    按名字匹配；匹配不唯一或找不到时才让你重选。
    第三步 主循环   检测变化、发送通知的逻辑与旧版完全一致，
                    只是取分改成调用 zhixuewang 库（它会自动续 XToken）。

主循环里两个针对真实情况的行为约定：

  · **考试被暂时撤回**：整场考试一条成绩都查不到时，不当作错误、不发通知，
    也不把这个空结果当成新基线。保持监控，等它重新出现后与**上一次成功获取的
    成绩**比对，有差异才通知。某一科单独消失仍按原逻辑报【成绩撤回】。

  · **错误通知限流**：获取失败的通知最多每 ERROR_NOTIFY_COOLDOWN 秒一条，
    连续失败超过 MAX_CONSECUTIVE_FAILURES 次后彻底安静，直到重新成功获取。

运行加固（`harden_library_session`）：库内部 20 个请求全部没设 timeout，
一旦网络卡住会永久挂死。这里给库的 session 注入默认超时，并停用只用于排名的
额外请求（它拖慢速度，且失败会连已拿到的成绩一起丢掉）。

用法:
    python3 main.py                 # 交互式：确认登录 + 选择考试
    python3 main.py --yes           # 跳过两个确认，直接沿用上次的登录与考试
    python3 main.py --yes --once    # 只跑一轮就退出，用于验证配置
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from zhixue_login import clear_cached_cookies, get_account  # noqa: E402

# ==================== SETUP ====================

INTERVAL = 60
ERROR_NOTIFY_COOLDOWN = 600
SEND_FIRST_RUN_NOTIFY = True

CONFIG_PATH = ROOT / "monitor_config.json"
RECENT_EXAM_PAGES = 2      # 最近学年最多抓几页考试（每页 10 场）
CALL_TIMEOUT = 45          # 单次库调用看门狗秒数（库内部没有设置任何 timeout）
REQUEST_TIMEOUT = 20       # 注入给库内每个 HTTP 请求的默认超时
DISABLE_RANK_LOOKUP = True # 停用只用于排名的额外请求（拖慢速度，且失败会丢掉已拿到的成绩）


# ==================== 库调用看门狗 ====================

class LibraryHang(Exception):
    """库调用在给定时间内没返回。

    zhixuewang 内部所有请求都没有传 timeout，一旦网络卡住会永久挂死，
    所以这里的每次调用都要套一层看门狗。
    """


def call_with_timeout(fn, timeout: float, label: str):
    """在守护线程里执行 fn，超时抛 LibraryHang。"""
    box: dict = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - 要把异常带回主线程
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True, name=f"zx-{label}")
    thread.start()
    thread.join(timeout)

    if thread.is_alive():
        raise LibraryHang(f"{label} 超过 {timeout:.0f}s 未返回（库内部无 timeout）")
    if "error" in box:
        raise box["error"]
    return box.get("value")


# ==================== 给库的 session 兜底 ====================

def harden_library_session(account) -> list[str]:
    """让 zhixuewang 不那么容易卡死，也少发无用请求。做两件事：

    1) **注入默认超时。** 库内部所有请求都没传 timeout，网络一卡就永久挂死。
       注意 `socket.setdefaulttimeout()` 对它无效 —— urllib3 会显式构造
       `Timeout(connect=None, read=None)`，等于"永不超时"，不会回退到全局默认值
       （已实测：本地"只接受不回包"的服务器上照样无限阻塞）。
       所以这里直接给 session 实例包一层 request，补上默认 timeout。

    2) **停用排名查询。** `get_self_mark()` 末尾会调 `_set_exam_rank()`，额外发
       getLevelTrend / getSubjectDiagnosis 两个请求，结果只用于 class_rank，
       而本监控不展示排名。更糟的是它一旦失败会抛异常，把**已经拿到的成绩也一起丢掉**。
       替换成空操作后，每次轮询少两次请求，也少一类失败。

    Returns:
        实际生效的措施说明，用于打印。
    """
    notes: list[str] = []

    # ---- 1) 默认超时 ----
    try:
        session = account.get_session()
        if not getattr(session, "_zx_timeout_patched", False):
            original_request = session.request

            def request_with_timeout(method, url, **kwargs):
                kwargs.setdefault("timeout", REQUEST_TIMEOUT)
                return original_request(method, url, **kwargs)

            session.request = request_with_timeout
            session._zx_timeout_patched = True
            notes.append(f"已给库内请求注入 {REQUEST_TIMEOUT}s 默认超时")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"注入默认超时失败（{type(exc).__name__}: {exc}），仍依赖看门狗")

    # ---- 2) 停用排名查询 ----
    if DISABLE_RANK_LOOKUP and hasattr(account, "_set_exam_rank"):
        try:
            account._set_exam_rank = lambda mark: None
            notes.append("已停用未使用的排名查询（每轮少 2 个请求）")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"停用排名查询失败（{type(exc).__name__}: {exc}）")

    return notes


# ==================== 第一步：确认登录 ====================

def describe_account(account) -> list[str]:
    """列出用于人工核对的身份信息 —— 全部是非敏感字段，不含密码/Cookie/手机号。"""
    role_map = {"student": "学生", "parent": "家长", "teacher": "教师"}
    lines = [f"角色      : {role_map.get(account.role.name, account.role.name)}"]

    for label, attr in (("姓名      : ", "name"),
                        ("登录用户名: ", "username")):
        value = getattr(account, attr, "") or ""
        if value:
            lines.append(label + str(value))

    clazz = getattr(account, "clazz", None)
    if clazz is not None:
        school = getattr(getattr(clazz, "school", None), "name", "")
        grade = getattr(getattr(clazz, "grade", None), "name", "")
        if school:
            lines.append(f"学校      : {school}")
        if grade:
            lines.append(f"年级      : {grade}")
        if getattr(clazz, "name", ""):
            lines.append(f"班级      : {clazz.name}")

    return lines


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    while True:
        try:
            answer = input(prompt + suffix).strip().lower()
        except EOFError:
            return default
        if not answer:
            return default
        if answer in ("y", "yes", "是", "1"):
            return True
        if answer in ("n", "no", "否", "0"):
            return False
        print("请输入 y 或 n")


def setup_account(auto_yes: bool = False):
    """拿到一个可用账号，并让用户用非敏感信息确认是对的账号。"""
    print("=" * 62)
    print("第一步：确认登录状态")
    print("=" * 62)

    while True:
        account = get_account()

        print("\n当前登录的账号信息：")
        for line in describe_account(account):
            print("  " + line)

        if auto_yes or ask_yes_no("\n确认这是你要监控的账号吗？", default=True):
            return account

        # 用户否认 -> 清掉本地缓存，强制重新登录
        clear_cached_cookies()
        print("\n已清除本地登录缓存，请重新登录。\n")


# ==================== 第二步：选择考试 ====================

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception as exc:  # noqa: BLE001
            print(f"警告：读取 {CONFIG_PATH.name} 失败（{exc}），忽略")
    return {}


def save_config(exam) -> None:
    """按名称保存选择。id 只作记录 —— examId 会随链接变化，名字相对稳定。"""
    data = load_config()
    data.update({
        "exam_name": exam.name,
        "exam_id": exam.id,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    CONFIG_PATH.chmod(0o600)


def fetch_recent_exams(account, pages: int = RECENT_EXAM_PAGES) -> list:
    """抓取最近若干场考试。

    不用库的 get_exams()，因为它会遍历**全部学年**、翻完每一页，慢且请求量大。
    这里从最近的学年开始找，第一个"有考试"的学年取够就停。
    """
    years = call_with_timeout(account.get_academic_year, CALL_TIMEOUT, "get_academic_year")

    collected: list = []
    for year in years:
        page = 1
        while page <= pages:
            batch, has_next = call_with_timeout(
                lambda p=page, y=year: account.get_page_exam(p, y),
                CALL_TIMEOUT, f"get_page_exam(page={page})")
            collected.extend(batch)
            if not has_next:
                break
            page += 1
        if collected:
            break  # 最近一个有考试的学年就够了

    collected.sort(key=lambda e: (e.create_time or 0), reverse=True)
    return collected


def format_exam_line(index: int, exam) -> str:
    when = "日期未知"
    if exam.create_time:
        try:
            when = datetime.fromtimestamp(exam.create_time / 1000).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            pass
    return f"  [{index:2d}] {when}  {exam.name}"


def prompt_exam_choice(exams: list):
    print("\n最近的考试：")
    for i, exam in enumerate(exams, 1):
        print(format_exam_line(i, exam))

    print("\n输入序号选择；也可直接输入考试名称的一部分来匹配；直接回车 = 最新一场。")
    while True:
        try:
            raw = input("你的选择: ").strip()
        except EOFError:
            raw = ""

        if not raw:
            exam = exams[0]
            print(f"已选择最新一场：{exam.name}")
            save_config(exam)
            return exam

        if raw.isdigit():
            index = int(raw)
            if 1 <= index <= len(exams):
                exam = exams[index - 1]
                print(f"已选择：{exam.name}")
                save_config(exam)
                return exam
            print(f"序号超出范围（1-{len(exams)}），请重新输入")
            continue

        hits = [e for e in exams if raw in e.name]
        if len(hits) == 1:
            exam = hits[0]
            print(f"已选择：{exam.name}")
            save_config(exam)
            return exam
        if not hits:
            print(f"没有匹配「{raw}」的考试，请重新输入")
            continue

        print(f"「{raw}」匹配到 {len(hits)} 场，请输入序号或更精确的关键词：")
        for exam in hits:
            print(f"  - {exam.name}")


def choose_exam(account, auto_yes: bool = False):
    """交互式选出要监控的考试。优先按上次保存的**名称**匹配。"""
    print("\n" + "=" * 62)
    print("第二步：选择要监控的考试")
    print("=" * 62)

    saved = load_config()
    saved_name = saved.get("exam_name", "")

    try:
        exams = fetch_recent_exams(account)
    except Exception as exc:  # noqa: BLE001
        print(f"警告：获取考试列表失败（{type(exc).__name__}: {exc}）")
        print("改为直接取最新一次考试…")
        exams = []
        try:
            latest = call_with_timeout(account.get_latest_exam, CALL_TIMEOUT, "get_latest_exam")
            if latest:
                exams = [latest]
        except Exception as exc2:  # noqa: BLE001
            print(f"错误：连最新考试也拿不到（{type(exc2).__name__}: {exc2}）")
            return None

    if not exams:
        print("错误：没有查到任何考试记录")
        return None

    # ---- 优先沿用上次保存的考试（按名称匹配，抗 examId 变化）----
    if saved_name:
        exact = [e for e in exams if e.name == saved_name]
        loose = exact or [e for e in exams if saved_name in e.name or e.name in saved_name]

        if len(loose) == 1:
            exam = loose[0]
            print(f"\n沿用上次保存的考试：「{exam.name}」")
            if auto_yes or ask_yes_no("仍监控这场考试吗？", default=True):
                save_config(exam)
                return exam
            print("好，请重新选择。")
        elif len(loose) > 1:
            print(f"\n上次保存的「{saved_name}」匹配到多场考试，请重新指定。")
        else:
            print(f"\n上次保存的考试「{saved_name}」已不在最近列表中，请重新选择。")

    if auto_yes:
        exam = exams[0]
        print(f"[--yes] 直接选择最新一场：{exam.name}")
        save_config(exam)
        return exam

    return prompt_exam_choice(exams)


# ==================== 第三步：取分（改用 zhixuewang 库） ====================

def fetch_scores(account, exam):
    """获取指定考试的成绩。

    Returns:
        OrderedDict{学科: (得分, 满分)}；失败返回 None。
        注意：考试已发布但尚未出分时返回的是空 OrderedDict，不是 None。
    """
    try:
        mark = call_with_timeout(
            lambda: account.get_self_mark(exam, has_total_score=False),
            CALL_TIMEOUT, "get_self_mark")
    except Exception as exc:  # noqa: BLE001
        print(f"error: 获取成绩失败：{type(exc).__name__}: {exc}")
        return None

    scores: OrderedDict = OrderedDict()
    for item in mark:
        subject = item.subject.name
        user_score = item.score
        standard_score = item.subject.standard_score
        if subject and user_score is not None and standard_score is not None:
            scores[subject] = (float(user_score), float(standard_score))
    return scores


def format_scores(scores_dict):
    lines = []
    for subject, (user, std) in scores_dict.items():
        lines.append(f"{subject} {user} / {std}")
    return lines


# ==================== 通知 ====================

def send_notification(title, content):
    sendkey = (os.environ.get("SERVERCHAN_SENDKEY", "").strip()
               or str(load_config().get("serverchan_sendkey", "")).strip())
    if not sendkey or sendkey == "你的SendKey":
        print("success: SERVERCHAN_SENDKEY is unconfigured; sending operation skipped.")
        return False
    api = f"https://sctapi.ftqq.com/{sendkey}.send"
    payload = {"title": title, "desp": content}
    try:
        r = requests.post(api, data=payload, timeout=10)
        if r.status_code == 200:
            print(f"success: Sent: {title}")
            return True
        else:
            print(f"error: Sending failed, status code: {r.status_code}")
    except Exception as e:
        print(f"error: Sending Exception: {e}")
    return False


def compare_and_get_changes(previous, current):
    """
    对比两次成绩，分别返回新增、修改、移除的学科。

    - added: list of (subject, (new_user, new_std))
    - modified: list of (subject, (old_user, old_std, new_user, new_std))
    - removed: list of (subject, (old_user, old_std))
    """
    added = []
    modified = []
    removed = []

    # 新增 & 修改
    for subject, new_scores in current.items():
        if subject not in previous:
            added.append((subject, new_scores))
        else:
            old_scores = previous[subject]
            if old_scores != new_scores:
                modified.append((subject, old_scores[0], old_scores[1], new_scores[0], new_scores[1]))

    # 移除（现在没有，之前有的）
    for subject, old_scores in previous.items():
        if subject not in current:
            removed.append((subject, old_scores))

    return added, modified, removed


# ==================== MAIN ====================

def main():
    parser = argparse.ArgumentParser(description="智学网成绩发布实时监控 & 通知")
    parser.add_argument("--yes", action="store_true",
                        help="跳过登录确认和考试选择确认，直接沿用上次配置")
    parser.add_argument("--once", action="store_true",
                        help="只跑一轮就退出（用于验证配置是否正确）")
    args = parser.parse_args()

    # ---------- setup ----------
    account = setup_account(auto_yes=args.yes)

    # 先给库的 session 兜底，这样连"拉考试列表"也受超时保护
    print()
    for note in harden_library_session(account):
        print(f"运行加固：{note}")

    exam = choose_exam(account, auto_yes=args.yes)
    if exam is None:
        print("无法确定要监控的考试，退出。")
        return 1

    exam_title = exam.name
    print(f"\n开始监控：{exam_title}")
    print(f"考试 id（仅作记录，可能随链接变化）：{exam.id}")
    print(f"刷新间隔：{INTERVAL} 秒\n")

    previous_scores = None
    last_error_notify = 0
    first_run = True
    withdrawn = False          # 考试是否处于"被学校暂时撤回"状态

    # 连续失败计数及上限
    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = 5   # 连续失败5次后暂停错误通知

    def nap() -> None:
        """一轮结束后的等待；--once 模式下不等待。"""
        if not args.once:
            time.sleep(INTERVAL)

    # 用 for 而不是 while，方便 --once 只跑一轮（continue 仍会推进循环）
    for _ in range(1 if args.once else 1 << 60):
        try:
            current_scores = fetch_scores(account, exam)
        except Exception as e:
            print(f"fatal error: {e}")
            current_scores = None

        # ---------- 处理获取失败（网络/接口异常）----------
        if current_scores is None:
            consecutive_failures += 1
            now = time.time()

            # 错误通知是**限流**的：无论中间是否成功过，最多每 ERROR_NOTIFY_COOLDOWN 秒一次，
            # 且连续失败超过 MAX_CONSECUTIVE_FAILURES 次后彻底安静，直到重新成功获取。
            if (now - last_error_notify > ERROR_NOTIFY_COOLDOWN) and (consecutive_failures <= MAX_CONSECUTIVE_FAILURES):
                title = f"智学网成绩监控异常 - {exam_title}"
                content = "获取成绩失败，可能是登录已过期或网络异常，请重新运行本脚本。"
                send_notification(title, content)
                last_error_notify = now
                print(f"[error] 已发送错误通知（连续失败第 {consecutive_failures} 次）")
            else:
                if consecutive_failures > MAX_CONSECUTIVE_FAILURES:
                    print("[skip] 连续失败次数已达上限，暂停错误通知，等待下一次成功获取后恢复")
                else:
                    print("[skip] 错误通知冷却中，暂不发送")

            nap()
            continue

        # ---------- 获取成功 ----------
        # 重置连续失败计数（只要成功拿到响应，无论成绩有无变化都重置）
        if consecutive_failures > 0:
            print(f"[reset] 成功获取成绩，连续失败计数 {consecutive_failures} 已清零")
            consecutive_failures = 0

        # ---- 整场考试一条成绩都查不到：视为被学校暂时撤回 ----
        # 关键是**不要**把这个空结果当成新基线，否则等考试恢复时会误判成"新出分"，
        # 也避免把"撤回"当成错误去反复通知。这里保持上一次成功获取的成绩作为基线，
        # 静默等待，等它重新出现时再与基线比对，有差异才通知。
        if not current_scores and previous_scores:
            if not withdrawn:
                withdrawn = True
                print("\n" + time.strftime("%Y-%m-%d %H:%M:%S") +
                      f" [{exam_title}] 当前查不到任何成绩，可能被学校暂时撤回了。")
                print("           保持监控，等它恢复且与上次成绩有差异时再通知；期间不发消息。")
            else:
                print(f"[waiting] {time.strftime('%H:%M:%S')} 考试仍未恢复，继续等待…")
            nap()
            continue

        if withdrawn:
            withdrawn = False
            print(f"[restored] {exam_title} 的成绩已重新出现，与上次成功获取的数据比对…")

        # 如果希望保留冷却的严肃性，可以注释掉上一行；这里保留，让冷却继续生效。

        current_lines = format_scores(current_scores)
        print("\n" + time.strftime("%Y-%m-%d %H:%M:%S") + f" {exam_title} 当前成绩：")
        if not current_lines:
            print("  undefined")
        else:
            for line in current_lines:
                print("  " + line)
        print("-" * 40)

        # ---------- 首次启动通知 ----------
        if first_run and SEND_FIRST_RUN_NOTIFY:
            title = f"智学网成绩监控已启动：{exam_title}"
            if current_lines:
                body = "\n\n".join(format_scores(current_scores))
            else:
                body = "暂无出分数据"
            content = f"监控已开始运行，当前出分如下：\n\n{body}"
            send_notification(title, content)
            first_run = False

        # ---------- 成绩变化检测 ----------
        if previous_scores is None:
            previous_scores = current_scores
            print("Initialization completed, score baseline established.")
        else:
            added, modified, removed = compare_and_get_changes(previous_scores, current_scores)

            if added or modified or removed:
                current_scores_str = "\n\n".join(format_scores(current_scores)) if current_scores else "暂无出分数据"

                # 1. 新出分学科
                for subject, (user, std) in added:
                    title = f"【新出分】{subject}"
                    content = f"【新出分：{subject}】{user} / {std}\n\n当前已出分：\n\n{current_scores_str}"
                    send_notification(title, content)

                # 2. 成绩更新
                for subject, old_user, old_std, new_user, new_std in modified:
                    title = f"【成绩更新】{subject}"
                    if old_std != new_std:
                        score_change = f"{old_user} / {old_std} -> {new_user} / {new_std}"
                    else:
                        score_change = f"{old_user} -> {new_user} / {new_std}"
                    content = f"【成绩更新：{subject}】{score_change}\n\n当前已出分：\n\n{current_scores_str}"
                    send_notification(title, content)

                # 3. 成绩撤回
                for subject, (old_user, old_std) in removed:
                    title = f"【成绩撤回】{subject}"
                    content = f"【成绩撤回：{subject}】原成绩 {old_user} / {old_std} 已消失\n\n当前已出分：\n\n{current_scores_str}"
                    send_notification(title, content)

                previous_scores = current_scores   # 更新基线
            else:
                print("No Changes.")

        nap()

    return 0


if __name__ == "__main__":
    sys.exit(main())
