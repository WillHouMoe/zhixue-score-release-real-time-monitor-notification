# 智学网成绩发布监控

一个本地运行的 Python 小工具：复用智学网登录会话，定时检查指定考试的成绩，
在出现新出分、分数更新或单科成绩撤回时，通过 Server 酱把变化推送到手机。

用户入口是根目录的 `main.py`。

## 安装

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python -m playwright install chromium
```

## 配置推送

在 [Server酱](https://sct.ftqq.com/) 获取 SendKey，然后任选一种方式：

```bash
export SERVERCHAN_SENDKEY="SCT..."
```

或者写进本地 `monitor_config.json`：

```json
{
  "serverchan_sendkey": "SCT...",
  "exam_name": "要监控的考试名称",
  "exam_id": "考试 id（仅作记录）"
}
```

环境变量优先。`monitor_config.json` 和 `cookies.json` 都只保存在本地，
不会提交到 Git。

## 运行

```bash
# 首次运行：确认账号并选择考试
venv/bin/python main.py

# 沿用上次的账号和考试，只验证一轮
venv/bin/python main.py --yes --once

# 长期监控
venv/bin/python main.py --yes
```

首次运行或 cookie 失效时会打开浏览器窗口。请直接在窗口里登录，
密码不会经过本程序，也不会写入文件；登录成功后会保存 cookie 供后续复用。

## 常用参数

| 参数 | 作用 |
| --- | --- |
| `--yes` | 跳过账号确认和考试选择，直接沿用上次配置 |
| `--once` | 只跑一轮就退出，适合验证配置 |

轮询间隔、错误通知冷却时间等常量位于 `main.py` 顶部的配置区。
