# Zhixue Score Release Real time Monitor Notification

Zhixue Score Release Real-time Monitor &amp; Notification

智学网成绩发布实时监控 & 通知

Step 1. 用 Chrome / DevTools 在成绩界面找到名为 getReportMain 的请求，复制其 curl 命令。复制的结果的格式应如下：

```
curl 'https://ali-bg.zhixue.com/zhixuebao/report/exam/getReportMain?examId=***' \
  -H 'Accept: application/json, text/plain, */*' \
  -H 'Accept-Language: zh-CN,zh;q=0.9' \
  -H 'Cache-Control: no-cache' \
  -H 'Connection: keep-alive' \
  -H 'Origin: https://www.zhixue.com' \
  -H 'Pragma: no-cache' \
  -H 'Referer: https://www.zhixue.com/' \
  -H 'Sec-Fetch-Dest: empty' \
  -H 'Sec-Fetch-Mode: cors' \
  -H 'Sec-Fetch-Site: same-site' \
  -H 'User-Agent: Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Mobile Safari/537.36' \
  -H 'X-Trans-Ready: true' \
  -H 'XToken: ***' \
  -H 'authbizcode: 0001' \
  -H 'authguid: ***' \
  -H 'authtimestamp: ***' \
  -H 'authtoken: ***' \
  -H 'role: student' \
  -H 'sec-ch-ua: "Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"' \
  -H 'sec-ch-ua-mobile: ?1' \
  -H 'sec-ch-ua-platform: "Android"' \
  -H 'token: ***'
```

Step 2. 将复制的命令粘贴到 curl.txt 中，运行 curl2config.py

```bash
python3 curl2config.py curl.txt
```

Step 3. 将输出的结果 URL 和 Request Header 插入到 main.py 相应位置上，并把 main.py 中其它用 `<xxx>` 表示的参数填充完整。其中，`SENDKEY` 请在 Server 酱中获取，获取方法见 [官方文档](https://doc.sc3.ft07.com/zh/serverchan3)。在手机上装好 Server 酱 APP 端，即可食用。

```bash
python3 main.py
```