#!/usr/bin/env python3
"""
将智学网 curl 命令转换为 Python 监控脚本的 URL 和 HEADERS 代码片段。
用法：
    python curl2config.py curl_command.txt    # 从文件读
    或直接管道输入：pbpaste | python curl2config.py   (macOS)
"""

import sys
import re

# 需要转换为环境变量的敏感请求头（key -> 环境变量名）
SENSITIVE_MAP = {
    "XToken": "ZHIXUE_XTOKEN",
    "authtoken": "ZHIXUE_AUTHTOKEN",
    "authguid": "ZHIXUE_AUTHGUID",
    "authtimestamp": "ZHIXUE_AUTHTIMESTAMP",   # 时间戳也可能过期，建议也设成变量
    "token": "ZHIXUE_TOKEN",
}

def parse_curl(text: str):
    """从 curl 命令文本中提取 URL 和请求头"""
    # 提取 URL（位于 curl 后的单引号中）
    url_match = re.search(r"curl\s+'([^']+)'", text)
    if not url_match:
        raise ValueError("❌ 未找到 URL，请确认 curl 命令格式正确")
    url = url_match.group(1)

    headers = {}
    # 提取所有 -H 'Key: Value'
    for m in re.finditer(r"-H\s+'([^']+)'", text):
        line = m.group(1)
        if ":" in line:
            key, value = line.split(":", 1)
            key, value = key.strip(), value.strip()
            # 如果值本身就是 *** 占位符，替换成真实示例（但通常我们直接从浏览器拿，不会是***）
            headers[key] = value
    return url, headers

def generate_config(url, headers):
    """生成 URL 和 HEADERS 的 Python 代码字符串 """
    lines = []
    lines.append(f'URL = "{url}"\n')
    lines.append("HEADERS = {")

    for key, val in headers.items():
        if key in SENSITIVE_MAP:
            env_var = SENSITIVE_MAP[key]
            # 值中的双引号需要转义，以便安全放入 os.environ.get 的默认值字符串
            escaped_val = val.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'    "{key}": os.environ.get("{env_var}", "{escaped_val}"),')
        else:
            # 普通头部直接硬编码
            escaped_val = val.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'    "{key}": "{escaped_val}",')
    lines.append("}")
    return "\n".join(lines)

def main():
    if len(sys.argv) > 1:
        with open(sys.argv[1], "r", encoding="utf-8") as f:
            raw = f.read()
    else:
        raw = sys.stdin.read()

    if not raw.strip():
        print("❌ 输入为空，请传入 curl 命令文本")
        sys.exit(1)

    try:
        url, headers = parse_curl(raw)
    except Exception as e:
        print(str(e))
        sys.exit(1)

    # 输出配置片段
    print("# 以下内容请复制到监控脚本的配置区域\n")
    print("import os\n")
    print(generate_config(url, headers))

if __name__ == "__main__":
    main()