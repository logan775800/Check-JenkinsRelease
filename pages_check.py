# -*- coding: utf-8 -*-
"""Web-Pages 发完的前端检查(只读,零第三方依赖)。

为什么要有:11-14 AR064 两次 uat 合并的部署,CF 标 success、所有文件 200,但首页黑屏——
首页 chunk 里留着没打包进去的 import "@/assets/.../headerMessage.png",浏览器加载即失败、路由起不来。
只看状态码查不出,这里把产物的 JS 依赖整张图走一遍。

做法:
1. 从本次 Jenkins 构建日志里取 wrangler 打印的部署地址 https://<hash>.<项目>.pages.dev(不需要 CF API)
2. 首页 → 入口 JS → 递归全部 chunk:每个都要 200 且是 JS(Pages 缺文件会回首页 HTML,也是 200)
   + 产物里不许有未解析的模块路径(真 import 只能是 ./ ../ / http 开头)
3. 有问题时往前找「最后一个检查通过的构建」当基准:只报这次新增的(连发两次坏版本也照报),
   往前都不干净的算既有问题(如 plat-h5 的 import("libpag"))
4. 项目主地址 <项目>.pages.dev 的入口 JS == 本次部署 → 线上已切到这一版
站点测试域名前有 IP 白名单,机器人这台 403,不用它。
"""
import concurrent.futures as cf
import re
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) JenkinsReleaseBot/1.0 pages-check"
DEPLOY_RE = re.compile(r"Take a peek over at (https://[0-9a-z\-]+\.[0-9a-z\-]+\.pages\.dev)")
_P = r'''([A-Za-z0-9@_~\-./]+)'''
# 压缩产物里真 import 紧贴引号;报错文案 `from "${e.fullPath}"`、`import 'pixi.js/..'` 带空格/模板变量,不算
IMPORT_RES = [
    re.compile(r'''(?<![\w$])from"''' + _P + '"'),
    re.compile(r'''(?:^|[;}\n])import"''' + _P + '"'),
    re.compile(r'''(?<![\w$.])import\("''' + _P + r'''"\)'''),
]
ASSET_RE = re.compile(r'''["'](?:/|\./)?(assets/[A-Za-z0-9_\-./]+\.js)["']''')
OK_PREFIX = ("./", "../", "/", "http://", "https://", "data:", "blob:")


def deploy_url(console_text):
    m = DEPLOY_RE.findall(console_text or "")
    return (m[-1].rstrip("/") + "/") if m else None


def _get1(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if "cloudflareaccess.com" in r.geturl():
                return 401, "cf-access", ""
            return r.status, r.headers.get("Content-Type", ""), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", "") if e.headers else "", ""
    except Exception as e:  # noqa
        return 0, "", "%s: %s" % (type(e).__name__, e)


def get(url, timeout=30):
    r = (0, "", "")
    for i in range(3):  # 网络抖动重试
        r = _get1(url, timeout)
        if r[0] != 0:
            return r
        time.sleep(1 + i)
    return r


def entries_of(html):
    return re.findall(r'<script[^>]+type="module"[^>]+src="([^"]+)"', html) + \
        re.findall(r'<link[^>]+rel="modulepreload"[^>]+href="([^"]+)"', html)


def _short(u):
    return u.split("/assets/", 1)[-1] if "/assets/" in u else urllib.parse.urlparse(u).path.lstrip("/")


def check(base):
    """返回 dict(kind=vite/static/access/down, problems=[...], js=N, entries=[path...])"""
    out = {"kind": "vite", "problems": [], "js": 0, "entries": []}
    code, ctype, html = get(base)
    if ctype == "cf-access":
        out["kind"] = "access"
        return out
    if code != 200 or "html" not in ctype:
        out.update(kind="down", problems=["首页打不开:HTTP %s %s" % (code, ctype.split(";")[0])])
        return out
    host = urllib.parse.urlparse(base).netloc
    entries = [urllib.parse.urljoin(base, e) for e in entries_of(html)]
    out["entries"] = [urllib.parse.urlparse(e).path for e in entries]
    if not entries:
        out["kind"] = "static"  # 非 Vite 落地页:只查 <script src>
        srcs = [urllib.parse.urljoin(base, s) for s in re.findall(r'<script[^>]+src="([^"]+)"', html)]
        srcs = [s for s in srcs if urllib.parse.urlparse(s).netloc == host]
        with cf.ThreadPoolExecutor(8) as ex:
            for u, (c, t, _) in zip(srcs, ex.map(get, srcs)):
                if c != 200 or "javascript" not in t:
                    out["problems"].append("脚本加载失败 %s:%s" % (c, _short(u)))
                else:
                    out["js"] += 1
        return out

    seen, queue, ref = set(), list(entries), {e: "index.html" for e in entries}
    probs = set()
    with cf.ThreadPoolExecutor(16) as ex:
        while queue:
            batch = [u for u in dict.fromkeys(queue) if u not in seen]
            queue = []
            seen.update(batch)
            for url, (code, ctype, body) in zip(batch, ex.map(get, batch)):
                src = _short(ref.get(url, "?"))
                if code != 200:
                    probs.add("缺文件(HTTP %s):%s ← %s" % (code, _short(url), src))
                    continue
                if "javascript" not in ctype:
                    probs.add("文件不存在(返回的是 %s):%s ← %s" % (ctype.split(";")[0] or "?", _short(url), src))
                    continue
                out["js"] += 1
                for rx in IMPORT_RES:
                    for sp in rx.findall(body):
                        if not sp.startswith(OK_PREFIX):
                            probs.add("引用没打包进去:\"%s\" 在 %s" % (sp, _short(url)))
                        elif sp.endswith((".js", ".mjs")):
                            nu = urllib.parse.urljoin(url, sp)
                            if urllib.parse.urlparse(nu).netloc == host and nu not in seen:
                                ref.setdefault(nu, url)
                                queue.append(nu)
                for a in ASSET_RE.findall(body):
                    nu = urllib.parse.urljoin(base, a)
                    if nu not in seen:
                        ref.setdefault(nu, url)
                        queue.append(nu)
    out["problems"] = sorted(probs)
    return out


def norm(x):
    """去掉 chunk 指纹,方便跨版本比同一个问题"""
    return re.sub(r"-[A-Za-z0-9_\-]{8}\.js", ".js", x)


def project_alias(url):
    """https://<hash>.<项目>.pages.dev/ → https://<项目>.pages.dev/"""
    h = urllib.parse.urlparse(url).netloc.split(".", 1)[1]
    return "https://%s/" % h


def run(url, prev_urls):
    """url = 本次部署地址;prev_urls = 之前的部署地址(新→旧)。
    返回 dict:
      verdict  ok / bad(有加载失败的问题,不管是不是这次引入,一律 bad——发版机器人不放行坏页面) / unknown(查不了)
      new      比上一个能查的版本多出来的问题(这次引入的)
      known    上一个版本就有的问题(还没修)
      baseline 最后一个检查通过的版本地址(回滚目标),找不到为 None
      js, kind, url, checked(实际检查的地址), live(项目主地址是否已是这一版 True/False/None)
    11-14 教训:AR064 Jenkins 只留 3 次构建,#34 往前只找到坏的 #33,按「既有问题放行」就漏了。
    """
    r = {"url": url, "checked": url, "new": [], "known": [], "baseline": None, "live": None}
    c = check(url)
    if c["kind"] == "access":
        # 部署地址开了 CF Access(ar043 那类);刚发完的就是最新 Production → 查项目主地址
        alias = project_alias(url)
        c = check(alias)
        r["checked"] = alias
        r["live"] = True if c["kind"] != "access" else None
    r.update(js=c["js"], kind=c["kind"])
    if c["kind"] == "access":
        r.update(verdict="unknown", why="部署地址受 CF Access 保护,查不了")
        return r
    probs = c["problems"]
    if r["live"] is None and c["entries"]:
        a = check_entries(project_alias(url))
        r["live"] = (set(a) == set(c["entries"])) if a is not None else None
    if not probs:
        r["verdict"] = "ok"
        return r
    prev_set = None          # 紧挨着的上一个能查的版本:区分「这次引入」还是「上一版就有」
    for pu in list(dict.fromkeys(prev_urls))[:6]:
        if pu.rstrip("/") == url.rstrip("/"):
            continue
        pc = check(pu)
        if pc["kind"] in ("access", "down"):
            continue
        if prev_set is None:
            prev_set = {norm(x) for x in pc["problems"]}
        if not pc["problems"]:
            r["baseline"] = pu
            break
    prev_set = prev_set or set()
    r["new"] = [x for x in probs if norm(x) not in prev_set]
    r["known"] = [x for x in probs if norm(x) in prev_set]
    r["verdict"] = "bad"
    return r


def check_entries(base):
    code, ctype, html = get(base)
    if code != 200 or "html" not in ctype:
        return None
    return [urllib.parse.urlparse(urllib.parse.urljoin(base, e)).path for e in entries_of(html)]


if __name__ == "__main__":  # 手动:python pages_check.py <部署地址> [之前的部署地址...]
    import json
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(run(sys.argv[1], sys.argv[2:]), ensure_ascii=False, indent=1))
