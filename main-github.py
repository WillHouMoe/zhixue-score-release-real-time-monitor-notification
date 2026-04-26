import json
import time
import os
import requests
from collections import OrderedDict

# ==================== SETUP ====================

URL = "<URL>"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "Origin": "https://www.zhixue.com",
    "Pragma": "no-cache",
    "Referer": "https://www.zhixue.com/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "User-Agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Mobile Safari/537.36",
    "X-Trans-Ready": "true",
    "XToken": os.environ.get("ZHIXUE_XTOKEN", "<ZHIXUE_XTOKEN>"),
    "authbizcode": "0001",
    "authguid": os.environ.get("ZHIXUE_AUTHGUID", "<ZHIXUE_AUTHGUID>"),
    "authtimestamp": os.environ.get("ZHIXUE_AUTHTIMESTAMP", "<ZHIXUE_AUTHTIMESTAMP>"),
    "authtoken": os.environ.get("ZHIXUE_AUTHTOKEN", "<ZHIXUE_AUTHTOKEN>"),
    "role": "student",
    "sec-ch-ua": "\"Google Chrome\";v=\"147\", \"Not.A/Brand\";v=\"8\", \"Chromium\";v=\"147\"",
    "sec-ch-ua-mobile": "?1",
    "sec-ch-ua-platform": "\"Android\"",
    "token": os.environ.get("ZHIXUE_TOKEN", "<ZHIXUE_TOKEN>"),
}

SENDKEY = os.environ.get("SERVERCHAN_SENDKEY", "<SENDKEY>")
INTERVAL = 60
ERROR_NOTIFY_COOLDOWN = 600
SEND_FIRST_RUN_NOTIFY = True
EXAM_TITLE = os.environ.get("EXAM_TITLE", "<EXAM_TITLE>")

# ==================== TOOLKIT ====================

def fetch_scores():
    try:
        resp = requests.get(URL, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            print(f"error: HTTP Error Code: {resp.status_code}")
            return None
        text = resp.text.strip()
        if not text:
            print("error: Response Blank")
            return None
        data = resp.json()
    except Exception as e:
        print(f"error: JSON request or parsing failed: {e}")
        return None

    if data.get("errorCode") != 0:
        print(f"error: Interface Error: {data.get('errorInfo', '未知')}")
        return None

    result = data.get("result")
    if not result:
        print("error: No Result")
        return None

    paper_list = result.get("paperList", [])
    scores = OrderedDict()
    for paper in paper_list:
        subject = paper.get("subjectName") or paper.get("title", "未知学科")
        user_score = paper.get("userScore")
        standard_score = paper.get("standardScore")
        if subject and user_score is not None and standard_score is not None:
            scores[subject] = (float(user_score), float(standard_score))
    return scores


def format_scores(scores_dict):
    lines = []
    for subject, (user, std) in scores_dict.items():
        lines.append(f"{subject} {user} / {std}")
    return lines


def send_notification(title, content):
    if not SENDKEY or SENDKEY == "你的SendKey":
        print("success: SENDKEY is unconfigured; sending operation skipped.")
        return False
    api = f"https://sctapi.ftqq.com/{SENDKEY}.send"
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
    print(f"zhixue.com grade monitoring started, interval: {INTERVAL} seconds...")
    previous_scores = None
    last_error_notify = 0
    first_run = True

    while True:
        try:
            current_scores = fetch_scores()
        except Exception as e:
            print(f"fatal error: {e}")
            current_scores = None

        if current_scores is None:
            now = time.time()
            if now - last_error_notify > ERROR_NOTIFY_COOLDOWN:
                title = f"智学网成绩监控异常 - {EXAM_TITLE}"
                content = "获取成绩失败，XToken 可能已过期，请重新获取 curl 命令并更新。"
                send_notification(title, content)
                last_error_notify = now
            else:
                print("[skip] Exception notification cooling down, no duplicate sending.")
            time.sleep(INTERVAL)
            continue

        last_error_notify = 0
        current_lines = format_scores(current_scores)
        print("\n" + time.strftime("%Y-%m-%d %H:%M:%S") + f" {EXAM_TITLE} 当前成绩：")
        if not current_lines:
            print("  undefined")
        else:
            for line in current_lines:
                print("  " + line)
        print("-" * 40)

        # ---------- 首次启动通知 ----------
        if first_run and SEND_FIRST_RUN_NOTIFY:
            title = f"智学网成绩监控已启动：{EXAM_TITLE}"
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
                # 当前出分列表字符串，用于所有变化通知
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

        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()