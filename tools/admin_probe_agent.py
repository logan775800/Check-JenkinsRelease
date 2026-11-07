"""后台地址探测程序：跑在 Logan 的固定 IP 电脑上，替发版机器人访问各站后台登录页。

为什么不在机器人服务器上直接访问：各站后台前面的 Cloudflare 只放行白名单 IP，
机器人那台服务器不在白名单里（77/79 个站返回 Cloudflare 403），而这台电脑在。
机器人经 ssh 反向隧道（由这台电脑主动连过去）调用本程序：
    GET http://127.0.0.1:18765/check?site=AR104
返回 JSON：{"site": "AR104", "results": [{"url", "verdict", "why", "code", "title"}...]}
verdict：ok=后台程序正常出页面 / bad=后台崩了或打不开 / unknown=被拦、看不出好坏

站点→后台地址清单是 CSV（站点,完整URL），不进仓库（含各站域名），每次请求现读，改完不用重启。
只访问清单里的地址：不接受任意 URL，免得这个口子被当成代理用。
"""
import csv
import html
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("PROBE_HOST", "127.0.0.1")
PORT = int(os.environ.get("PROBE_PORT", "18765"))
LIST = os.environ.get("PROBE_LIST", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  "..", "local", "admin_urls.csv"))
TIMEOUT = 30          # 刚部署完的 IIS 第一次请求要编译视图，给足时间
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# 后台程序自己崩了的特征（AR088 11-06：缺 CodeDom dll，页面「应用程序中的服务器错误」，状态码可能是 500）
CRASH = re.compile(r"应用程序中的服务器错误|Server Error in '/|Runtime Error|CodeDom|Compilation Error|"
                   r"编译错误|运行时错误|HTTP Error 50\d|Service Unavailable", re.I)
CF_BLOCK = re.compile(r"Attention Required|cf-error-details|Just a moment", re.I)


def load_list():
    m = {}
    with open(LIST, encoding="utf-8-sig", newline="") as f:
        for row in csv.reader(f):
            if len(row) < 2:
                continue
            site, url = row[0].strip().upper(), row[1].strip()
            if re.fullmatch(r"AR\d{3}", site) and url.startswith("http"):
                m.setdefault(site, []).append(url)
    return m


def classify(code, final_url, body):
    """(verdict, why, title) —— 判定规则单独成函数，自测里直接喂样例。"""
    t = re.search(r"<title>\s*([^<]*)", body or "", re.I)
    title = html.unescape(t.group(1)).strip()[:60] if t else ""
    if CRASH.search(body or ""):
        return "bad", "后台报错页（%s）" % (CRASH.search(body).group(0)), title
    if code and code >= 500:
        return "bad", "HTTP %s" % code, title
    if code == 403 and CF_BLOCK.search(body or ""):
        return "unknown", "被 Cloudflare 拦了（这台电脑的 IP 不在该站白名单？）", title
    if code == 200 and ("登录" in title or "/Error/IpLock" in (final_url or "")):
        return "ok", "IP 限制页（程序正常）" if "IpLock" in (final_url or "") else "登录页正常", title
    if code == 200:
        return "unknown", "页面能打开但不是登录页：%s" % (title or "无标题"), title
    return "bad", "HTTP %s" % code, title


def probe(url):
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
            code, final, body = r.status, r.geturl(), r.read(200000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        code, final = e.code, e.geturl() if hasattr(e, "geturl") else url
        try:
            body = e.read(200000).decode("utf-8", "replace")
        except Exception:
            body = ""
    except Exception as e:
        return {"url": url, "verdict": "bad", "why": "连不上：%s" % str(getattr(e, "reason", e))[:120],
                "code": 0, "title": "", "secs": round(time.time() - t0, 1)}
    v, why, title = classify(code, final, body)
    return {"url": url, "verdict": v, "why": why, "code": code, "title": title,
            "secs": round(time.time() - t0, 1)}


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        if u.path == "/healthz":
            try:
                n = sum(len(v) for v in load_list().values())
                return self._send(200, {"ok": True, "urls": n})
            except Exception as e:
                return self._send(500, {"ok": False, "error": str(e)})
        if u.path != "/check":
            return self._send(404, {"error": "only /check?site=ARnnn"})
        site = (qs.get("site") or [""])[0].strip().upper()
        if not re.fullmatch(r"AR\d{3}", site):
            return self._send(400, {"error": "bad site"})
        try:
            urls = load_list().get(site, [])
        except Exception as e:
            return self._send(500, {"error": "读清单失败：%s" % e})
        with ThreadPoolExecutor(4) as ex:
            res = list(ex.map(probe, urls))
        self._send(200, {"site": site, "results": res})

    def log_message(self, fmt, *a):
        sys.stderr.write("%s %s\n" % (time.strftime("%m-%d %H:%M:%S"), fmt % a))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--once":       # 手测：python admin_probe_agent.py --once AR104
        m = load_list()
        for s in sys.argv[2:] or sorted(m):
            for r in map(probe, m.get(s.upper(), [])):
                print(s.upper(), r["verdict"], r["code"], r["why"], r["url"])
        sys.exit(0)
    print("admin probe agent on %s:%s, list=%s" % (HOST, PORT, os.path.abspath(LIST)), flush=True)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
