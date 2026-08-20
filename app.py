# -*- coding: utf-8 -*-
"""
Jenkins 发版核对 · 本地网页版

用法：
    $env:JENKINS_USER='logan'; $env:JENKINS_API_TOKEN='xxxx'
    python app.py                     # 然后浏览器开 http://127.0.0.1:8770

把发布单整段复制粘进网页文本框，点「开始检查」，按视图分组列出每个站点的发版结果。

为什么必须有这个后端、不能做成纯静态网页：
  1. Jenkins 没开 CORS，浏览器直接调 /api/json 会被跨域拦掉；
  2. API Token 若放前端 JS，等于谁打开网页谁就有你的 Jenkins 权限。
后端在本机持有 Token 并转发，前端只跟 127.0.0.1 说话，Token 不出这台机器。

只读，不会触发任何构建。零第三方依赖（Python 标准库）。
"""
import base64
import difflib
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import socketserver
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

# ThreadingHTTPServer 是 Python 3.7 才有的。CentOS 7.9 自带 python3 是 3.6.8，
# 直接 import 会 ImportError，所以退回自己拼 ThreadingMixIn（等价实现）。
try:
    from http.server import ThreadingHTTPServer
except ImportError:
    class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
        daemon_threads = True

JENKINS_URL = os.environ.get("JENKINS_URL", "").rstrip("/")
JENKINS_USER = os.environ.get("JENKINS_USER", "")
JENKINS_TOKEN = os.environ.get("JENKINS_API_TOKEN", "")
PORT = int(os.environ.get("JENKINS_WEB_PORT", "8770"))
# 监听地址。默认只听回环；要给别人访问必须显式设成 0.0.0.0，顺带强制你想一下认证问题。
HOST = os.environ.get("JENKINS_WEB_HOST", "127.0.0.1")
# 是否允许用服务端环境变量里的凭据。本机自用=1；部署到服务器务必设 0，
# 否则所有访问者都在用部署者的 Token 查数据，等于绕过 Jenkins 权限。
ALLOW_SERVER_CREDS = os.environ.get("JENKINS_ALLOW_SERVER_CREDS", "1") == "1"
MAX_BUILDS_PER_JOB = int(os.environ.get("JENKINS_MAX_BUILDS", "30"))
CACHE_TTL = int(os.environ.get("JENKINS_CACHE_TTL", "60"))  # 秒。一次核对往往连点几次，缓存避免反复拉
# 按需拉构建历史时的并发数。Jenkins 前面有 Cloudflare WAF，并发开太大会被限速，
# 6 是实测下来既明显加速又不触发限速的值。见 README「Jenkins 前面有 WAF」。
FETCH_CONCURRENCY = max(1, int(os.environ.get("JENKINS_FETCH_CONCURRENCY", "6")))
# 需要拉的 job 超过这个数就改用「一次性全量」那条老路：几百个短请求穿 WAF
# 比一个大请求更容易被限速，而且到了这个量级两者耗时也差不多了。
BULK_THRESHOLD = int(os.environ.get("JENKINS_BULK_THRESHOLD", "200"))
MAX_BODY = 2 * 1024 * 1024  # /api/check 请求体上限。发布单再长也就几十 KB

# ── 第二套 Jenkins：后台(Admin)───────────────────────────────────────
# 后台不在 AR 那套 Jenkins 上，是独立的一台（Windows，job 叫 Sit-Admin 之类），
# 命名规约、参数名、凭据全都不一样。以前发布单里的「admin → xxx」直接被跳过不核对，
# 于是每次发版后台发没发、发的哪版，得另外找人问。
#
# 配置齐了才启用；没配就退回原来那句「不在这套 Jenkins 上」提示，不影响主流程。
# 用**独立凭据**：那台 Jenkins 上的账号和 AR 这套没关系，也不该共用 Token。
ADMIN_URL = os.environ.get("ADMIN_JENKINS_URL", "").rstrip("/")
ADMIN_USER = os.environ.get("ADMIN_JENKINS_USER", "")
ADMIN_TOKEN = os.environ.get("ADMIN_JENKINS_TOKEN", "")
# 后台的发版是**两步**，跟 AR 那套完全不同（他描述的实际操作）：
#   1. 先跑 build_admin，**版本号输在这里**，产出制品；
#   2. 再去 admin_ALL 视图里逐个点 Build Now，把制品部署到各站点。
# 所以「后台核对」要拆成两个问题，混成一个必然出错：
#   • 构建：build_admin 这一版打的是不是发布单要求的版本？（比版本）
#   • 部署：admin_ALL 里哪些 job 在时间窗内真的跑了？（比跑没跑，**没有版本可比**）
# 拿部署 job 去比版本，会因为它们根本没有版本参数而全判「版本未知」，纯噪音。
ADMIN_BUILD_JOB = os.environ.get("ADMIN_JENKINS_BUILD_JOB", "build_admin").strip()
ADMIN_DEPLOY_VIEW = os.environ.get("ADMIN_JENKINS_DEPLOY_VIEW", "admin_ALL").strip()
# 手动指定部署 job（逗号分隔）。留空就从上面那个视图里列。
ADMIN_JOBS = [j.strip() for j in
              os.environ.get("ADMIN_JENKINS_JOBS", "").split(",") if j.strip()]


ADMIN_COMP = "Admin"       # 内部组件名，页面上单独成一块
ADMIN_VIEW = "后台（另一套 Jenkins）"


def admin_ready():
    """三项就够。job 名单不算必填 —— 没写就自动发现。"""
    return bool(ADMIN_URL and ADMIN_USER and ADMIN_TOKEN)

# job 命名规约：AR{编号}-{组件}-{国家}-{站点名}
# 发布单里写的组件名和 Jenkins 里的组件名对不上，这里做别名映射。
# 键一律用 canon() 归一后的形式：转小写、去掉空格/连字符/下划线。
# 这样 "Web-Pages" / "web pages" / "webpages" / "WEB_PAGES" 都归到同一个键，不用逐个列。
COMPONENT_ALIAS = {
    "web": "Pages", "pages": "Pages", "webpages": "Pages",
    "前端": "Pages", "web页面": "Pages", "页面": "Pages", "前端页面": "Pages",
    "lotteryapi": "LotteryApi", "lottery": "LotteryApi",
    "webintranetapi": "WebIntranetApi", "intranetapi": "WebIntranetApi",
    # 发布单里长期就写成 Intrenet（a/e 对调）。这不是要提醒的笔误，是这边的既定写法，
    # 所以放进别名表走精确匹配 —— 靠下面的近似匹配也能认出来，但每次都会弹一条
    # 「已按最接近的…核对」黄条，天天核对天天弹，那是噪音。
    # 以后再冒出别的固定写法，也照这样加一行，不要靠近似匹配兜。
    "webintrenetapi": "WebIntranetApi", "intrenetapi": "WebIntranetApi",
    "webextendapi": "WebExtendApi", "extendapi": "WebExtendApi",
    "agentapi": "AgentApi",
    "webagent": "Web", "agentweb": "Web",
    "thirdapi": "ThirdApi",
    "thirdjob": "ThirdJob",
}


def canon(s):
    """组件名归一：小写 + 去空格/连字符/下划线。发布单里同一个组件有七八种写法。"""
    return re.sub(r"[\s\-_]+", "", (s or "").strip().lower())
# 组件 -> Jenkins 视图名，页面上按视图分组展示
COMPONENT_VIEW = {
    "Pages": "Web-Pages", "LotteryApi": "LotteryApi", "AgentApi": "AgentApi",
    "Web": "Agent_Web", "WebExtendApi": "WebExtendApi",
    "WebIntranetApi": "WebIntranetApi", "ThirdApi": "ThirdApi", "ThirdJob": "ThirdJob",
}
# 发布单里会出现、但不在这套 AR 命名的 Jenkins 上的组件
NOT_IN_JENKINS = {"admin", "sitadmin", "后台", "后台管理"}

# 缓存按「用户」分桶：不同人的 Jenkins 权限不同，看到的 job 集合也不同，不能共用一份。
# 每桶两部分：names = job 清单（便宜，一次拉全）；builds = 按 job 名分开存的构建历史（贵，按需拉）。
_cache = {}
_cache_lock = threading.Lock()
# 同一份数据的并发请求只让一个真去打 Jenkins，其余等它拉完直接用结果。
# 不这么做的话，缓存冷的时候连点两下「开始检查」会同时拉两遍全量。
_locks = {}


def _bucket(user):
    with _cache_lock:
        return _cache.setdefault(user, {"names": None, "builds": {}})


def _lock_for(key):
    with _cache_lock:
        lk = _locks.get(key)
        if lk is None:
            lk = _locks[key] = threading.Lock()
        return lk


# ---------------------------------------------------------------- Jenkins 访问
def jenkins_get(path, user, token_, timeout=180, base=None):
    """base 不传就是 AR 那套 Jenkins；后台那套把它的地址传进来。"""
    url = (base or JENKINS_URL) + path
    req = urllib.request.Request(url)
    token = base64.b64encode(f"{user}:{token_}".encode("ascii")).decode("ascii")
    req.add_header("Authorization", "Basic " + token)
    # 站点前面有 WAF，会按 User-Agent 拦截：python-urllib 的默认 UA 直接 403，必须伪装成浏览器
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) JenkinsReleaseCheck/1.0")
    ctx = None
    if url.lower().startswith("https"):
        ctx = ssl.create_default_context()
        # 显式抬到 TLS 1.2 起步。老系统（CentOS 7 的 OpenSSL 1.0.2）默认可能先试更低版本，
        # 被强制 1.2+ 的对端直接回 alert，报错信息还很难懂。
        try:
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2   # Python 3.7+
        except AttributeError:
            ctx.options |= getattr(ssl, "OP_NO_SSLv2", 0) | getattr(ssl, "OP_NO_SSLv3", 0)
            ctx.options |= getattr(ssl, "OP_NO_TLSv1", 0) | getattr(ssl, "OP_NO_TLSv1_1", 0)
        if os.environ.get("JENKINS_SKIP_CERT") == "1":
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.loads(r.read().decode("utf-8"))


def _q(tree):
    return "?tree=" + urllib.parse.quote(tree, safe="[]{},")


_BUILD_FIELDS = ("builds[number,result,building,timestamp,duration,url,actions["
                 "parameters[name,value],lastBuiltRevision[SHA1,branch[name,SHA1]],"
                 "causes[userName,shortDescription]]]{0,%d}")


def _entry(raw):
    """把一个 job 的原始构建列表转成缓存条目。解析放在这里做一次，
    别放到 run_check 里 —— 那样 60 秒内每点一次都要把上万条构建重解析一遍。"""
    raw = raw or []
    return {"ts": time.time(),
            "builds": [read_build(b) for b in raw],
            # Jenkins 只给最近 N 次。取满了就说明可能还有更老的没拿到，
            # 这个标记用来判断「时间窗内没构建」到底是真没发，还是被截断了没看见。
            "trunc": len(raw) >= MAX_BUILDS_PER_JOB}


def fetch_job_names(user, token_, force=False):
    """只拉 job 名和 url。几百 KB 级别，秒回 —— 站点清单、组件归属、展示名都够用了。"""
    b = _bucket(user)
    t0 = time.time()
    hit = b["names"]
    if not force and hit and t0 - hit["ts"] < CACHE_TTL:
        return hit["jobs"]
    with _lock_for((user, "names")):
        hit = b["names"]
        # 等锁期间别人已经拉过一轮：那一轮就是这次刷新，force 也直接用它的结果
        if hit and (hit["ts"] >= t0 or (not force and time.time() - hit["ts"] < CACHE_TTL)):
            return hit["jobs"]
        data = jenkins_get("/api/json" + _q("jobs[name,url]"), user, token_, timeout=120)
        jobs = data.get("jobs", []) or []
        with _cache_lock:
            b["names"] = {"ts": time.time(), "jobs": jobs}
        return jobs


def _fetch_builds_bulk(user, token_):
    """一次请求拿全部 job 的构建历史。响应十几 MB、要几十秒，
    只在「按需拉」不划算（需要的 job 太多）或被限速退回时才走这条。"""
    tree = "jobs[name," + (_BUILD_FIELDS % MAX_BUILDS_PER_JOB) + "]"
    data = jenkins_get("/api/json" + _q(tree), user, token_, timeout=180)
    return {j.get("name"): _entry(j.get("builds")) for j in (data.get("jobs") or []) if j.get("name")}


def _fetch_builds_each(names, user, token_):
    """按 job 逐个拉，线程池并发。返回 (结果, 失败列表, 是否中途放弃)。"""
    got, errs = {}, []
    stop = threading.Event()

    def one(name):
        if stop.is_set():          # 已经决定放弃了，剩下的别再往 Jenkins 上打
            return None
        path = "/job/" + urllib.parse.quote(name, safe="") + "/api/json"
        return name, jenkins_get(path + _q(_BUILD_FIELDS % MAX_BUILDS_PER_JOB),
                                 user, token_, timeout=60)

    with ThreadPoolExecutor(max_workers=min(FETCH_CONCURRENCY, len(names))) as pool:
        for name, res in zip(names, pool.map(lambda n: _safe(one, n), names)):
            if res is None:
                continue
            if isinstance(res, Exception):
                errs.append((name, str(getattr(res, "reason", res))))
                # 连错 5 个而且一个都没成 —— 要么 Token 废了，要么被 WAF 限速了。
                # 这时候把剩下几百个必然失败的请求打完毫无意义，还正好是最招限速的行为。
                if not got and len(errs) >= 5:
                    stop.set()
            else:
                got[res[0]] = _entry(res[1].get("builds"))
    return got, errs, stop.is_set()


def _safe(fn, arg):
    try:
        return fn(arg)
    except Exception as e:      # 单个 job 拉失败不能把整批带崩，收集起来后面统一处理
        return e


def fetch_builds(names, user, token_, force=False):
    """
    拿这些 job 的构建历史（已解析）。返回 {job名: 条目}。

    只拉真正要核对的那些 job —— 一次核对通常几十上百个，而 Jenkins 上有六百多个，
    全量拉等于九成的数据白传。需要的量太大时自动退回一次性全量。
    """
    b = _bucket(user)
    now = time.time()
    out, todo = {}, []
    for n in names:
        e = b["builds"].get(n)
        if not force and e and now - e["ts"] < CACHE_TTL:
            out[n] = e
        else:
            todo.append(n)
    if not todo:
        return out, []

    warns = []
    with _lock_for((user, "builds")):
        # 等锁期间别人可能已经把其中一部分拉好了，重新过一遍缓存，能少拉就少拉
        still = []
        for n in todo:
            e = b["builds"].get(n)
            if e and e["ts"] >= now:
                out[n] = e
            else:
                still.append(n)
        if still:
            if len(still) >= BULK_THRESHOLD:
                got = _fetch_builds_bulk(user, token_)
            else:
                got, errs, gave_up = _fetch_builds_each(still, user, token_)
                # 中途放弃、或失败过半，基本是被 WAF 限速了（逐个打太密），退回一次性全量重来
                if gave_up or len(errs) * 2 >= len(still):
                    got = _fetch_builds_bulk(user, token_)
                elif errs:
                    warns.append("有 %d 个任务的构建历史没拉到，这些任务的结果不准：%s%s（原因：%s）"
                                 % (len(errs), ", ".join(n for n, _ in errs[:5]),
                                    "…" if len(errs) > 5 else "", errs[0][1]))
            with _cache_lock:
                b["builds"].update(got)
            for n in still:
                out[n] = got.get(n) or {"ts": time.time(), "builds": [], "trunc": False}
    return out, warns


def admin_deploy_jobs(force=False):
    """部署 job 名单：配了就按配的，没配就从 ADMIN_DEPLOY_VIEW 那个视图里列。

    返回 (job名列表, 告警)。列不出来就返回空 —— 绝不瞎猜一个名字，
    猜错了会显示成「没有这个任务」，看着像后台没发。
    """
    if ADMIN_JOBS:
        return list(ADMIN_JOBS), []
    b = _bucket("__admin__")
    hit = b["names"]
    if not force and hit and time.time() - hit["ts"] < CACHE_TTL:
        return list(hit["jobs"]), []
    path = "/view/" + urllib.parse.quote(ADMIN_DEPLOY_VIEW, safe="") + "/api/json"
    try:
        data = jenkins_get(path + _q("jobs[name]"), ADMIN_USER, ADMIN_TOKEN,
                           timeout=60, base=ADMIN_URL)
    except urllib.error.HTTPError as ex:
        return [], ["后台 Jenkins（%s）：%s" % (ADMIN_URL,
                    friendly_http(ex, "%s 视图" % ADMIN_DEPLOY_VIEW))]
    except Exception as ex:
        return [], ["连不上后台 Jenkins 的「%s」视图（%s）：%s。"
                    "检查 ADMIN_JENKINS_URL 和这台服务器到那个地址的网络。"
                    % (ADMIN_DEPLOY_VIEW, ADMIN_URL,
                       str(getattr(ex, "reason", ex))[:120])]
    names = [j.get("name") for j in (data.get("jobs") or []) if j.get("name")]
    with _cache_lock:
        b["names"] = {"ts": time.time(), "jobs": names}
    if not names:
        return [], ["后台 Jenkins 的「%s」视图里一个 job 都没有（视图名写错？或该账号无权限）"
                    % ADMIN_DEPLOY_VIEW]
    return names, []


def fetch_admin_builds(force=False):
    """拉后台那套 Jenkins 上配置的几个 job 的构建历史。

    单独一套缓存和凭据：那台机器和 AR 这套没有任何关系，权限也不共用。
    某个 job 拉失败不影响其余的 —— 后台常有 saas/非saas 多个变体，
    挂一个不该让整块后台核对都没了。
    """
    b = _bucket("__admin__")
    now = time.time()
    names, warns = admin_deploy_jobs(force=force)
    if ADMIN_BUILD_JOB:
        names = [ADMIN_BUILD_JOB] + [n for n in names if n != ADMIN_BUILD_JOB]
    out = {}
    for name in names:
        e = b["builds"].get(name)
        if not force and e and now - e["ts"] < CACHE_TTL:
            out[name] = e
            continue
        path = "/job/" + urllib.parse.quote(name, safe="") + "/api/json"
        try:
            data = jenkins_get(path + _q(_BUILD_FIELDS % MAX_BUILDS_PER_JOB),
                               ADMIN_USER, ADMIN_TOKEN, timeout=60, base=ADMIN_URL)
        except urllib.error.HTTPError as ex:
            warns.append("后台 Jenkins：%s" % friendly_http(ex, name))
            continue
        except Exception as ex:
            warns.append("后台 Jenkins 的「%s」没拉到：%s"
                         % (name, str(getattr(ex, "reason", ex))[:120]))
            continue
        ent = _entry(data.get("builds"))
        ent["url"] = ADMIN_URL + "/job/" + urllib.parse.quote(name, safe="") + "/"
        with _cache_lock:
            b["builds"][name] = ent
        out[name] = ent
    return out, warns, names


def tz_label():
    """当前进程时区，形如 'IST +0530'。页面上要显示出来 —— 否则用户拿它和
    Jenkins 页面的时间对不上，会以为工具查错了（实际只是时区不同）。"""
    return "%s %s" % (time.strftime("%Z"), time.strftime("%z"))


def friendly_http(e, where=""):
    """把 HTTP 错误翻译成「该去改什么」。

    401 和 403 是两回事，403 还分两层，三种情况的修法南辕北辙：
      • WAF 403（响应头有 CF-RAY、**没有** X-Jenkins）—— 请求根本没到 Jenkins。
        要么 UA 被拦（本工具已伪装浏览器 UA），要么**这台服务器的出口 IP 不在白名单**。
        实测过：办公网能通、机房服务器 403，就是这一条。
      • Jenkins 403（响应头有 X-Jenkins）—— 认证过了但**这个账号没有读取权限**，
        或者压根没带上凭据（按匿名处理）。去 Jenkins 里给权限。
      • 401 —— 用户名或 Token 不对。注意用户名要填**登录名不是邮箱**，
        而且 Token 是**按实例**发的，A 站的 Token 在 B 站一律 401。
    不分层就只能看到一句 Forbidden，然后在错误的方向上排查半天。
    """
    hdrs = {}
    try:
        hdrs = {k.lower(): v for k, v in dict(e.headers).items()}
    except Exception:
        pass
    at = ("「%s」" % where) if where else ""
    if e.code == 401:
        return ("%s认证失败（401）：用户名或 API Token 不对。用户名填**登录名不是邮箱**；"
                "Token 必须是在**这台 Jenkins** 上生成的（每个实例各发各的，拿别台的一律 401）。" % at)
    if e.code == 403:
        if "x-jenkins" in hdrs:
            return ("%sJenkins 拒绝了（403，响应头有 X-Jenkins=%s，说明请求已经到达 Jenkins）："
                    "凭据没被认出来、或这个账号没有读取权限。先确认 .env 里的用户名/Token 确实生效了"
                    "（改完要 `docker compose up -d --build`），再让管理员给这个账号 Overall/Read。"
                    % (at, hdrs.get("x-jenkins")))
        if "cf-ray" in hdrs or "cloudflare" in hdrs.get("server", ""):
            return ("%s被 Cloudflare WAF 拦了（403，只有 CF-RAY 没有 X-Jenkins，"
                    "请求**没到 Jenkins**）：多半是这台服务器的出口 IP 不在白名单里。"
                    "同样的请求在办公网能通、在机房服务器 403，就是这个原因 —— "
                    "把服务器出口 IP 加进 WAF 白名单。" % at)
        return "%s返回 403，但看不出是 WAF 还是 Jenkins 拒的（响应头里两个标记都没有）。" % at
    return "%sJenkins 返回 HTTP %d" % (at, e.code)


def friendly_neterr(e):
    """把 Python 的网络异常翻译成能照着做的提示，别直接把 traceback 甩给用户。"""
    s = str(getattr(e, "reason", e))
    ver = ssl.OPENSSL_VERSION
    if "TLSV1_ALERT_PROTOCOL_VERSION" in s or "UNSUPPORTED_PROTOCOL" in s:
        return (
            "TLS 握手失败：对端拒绝了本机提供的 TLS 版本。"
            "本机 OpenSSL 是「%s」—— 1.0.x 只支持到 TLS 1.2，若 Jenkins 前面的 WAF 强制 TLS 1.3 就会这样。"
            "三种解法：① 若 Jenkins 就在本机，把 JENKINS_URL 改成 http://127.0.0.1:<Jenkins端口> 直连，绕开 TLS；"
            "② 用 Docker 跑本服务（镜像自带新版 OpenSSL）；③ 升级本机 OpenSSL/Python。"
            "详见 README「CentOS 7 的 TLS 限制」。原始错误：%s" % (ver, s)
        )
    if "CERTIFICATE_VERIFY_FAILED" in s:
        return ("证书校验失败（自签证书或缺中间证书）。确认无误后可在配置里加 "
                "JENKINS_SKIP_CERT=1 跳过校验。原始错误：%s" % s)
    if "Name or service not known" in s or "nodename nor servname" in s:
        return "DNS 解析不了 JENKINS_URL 里的主机名，检查地址是否写对、服务器能否解析该域名。原始错误：%s" % s
    if "Connection refused" in s:
        return "连接被拒绝：JENKINS_URL 的地址或端口不对，或该端口上没有服务。原始错误：%s" % s
    if "timed out" in s.lower():
        return "连接 Jenkins 超时：检查服务器到 Jenkins 的网络是否通（防火墙/安全组）。原始错误：%s" % s
    return "连接 Jenkins 失败：%s" % s


def cached_at(entries):
    """数据快照时间。取本次实际用到的那几份缓存里最早的一个 ——
    报最新的会让人以为数据比实际更鲜，而漏判恰恰出在最旧的那份上。"""
    ts = [e["ts"] for e in entries if e]
    return datetime.fromtimestamp(min(ts)).strftime("%H:%M:%S") if ts else ""


# ---------------------------------------------------------------- 解析与判定
def normalize_ref(s):
    """Git 插件把分支记成 refs/remotes/origin/xxx，参数里写的是 xxx，不剥前缀会满屏假告警。"""
    if not s:
        return ""
    for p in ("refs/remotes/", "refs/heads/", "origin/"):
        if s.startswith(p):
            s = s[len(p):]
    return s


def site_of(job_name):
    m = re.match(r"^(AR\d+)-", job_name)
    return m.group(1) if m else ""


def component_of(job_name):
    # 尾部的 - 要可选：少数 job 就叫 AR001-Pages，没有国家/站点段。
    # 写死成必须有 - 的话，这类 job 建不进索引，会被判成「该站点没有这个任务」。
    m = re.match(r"^AR\d+-([^-]+)(?:-|$)", job_name)
    return m.group(1) if m else ""


def presplit(text):
    """复制发布单时常丢换行，导致「AR002-SiteB【发布步骤】」粘成一行。在【】标题前补回换行。"""
    return re.sub(r"(?<!\n)(【)", r"\n\1", text or "")


def parse_sites(text):
    """从发布单原文抠 AR 编号。兼容 AR51 / ar-051 / AR0051，统一补成 3 位。"""
    out = []
    for m in re.finditer(r"(?i)\bAR[-_ ]?0*(\d{1,4})\b", text or ""):
        code = "AR" + m.group(1).zfill(3)
        if code not in out:
            out.append(code)
    return sorted(out)


def canon_group(s):
    """
    分组名归一。同一个分组在发布单里有多种写法，必须归到一起才能对上：
        【发布站点】段写「中台站点」，【发布代码】段写「中台版本分支」，都要归成「中台」。
    只剥尾缀，不动词头 —— 「非中台」不能被剥成「中台」。
    """
    s = re.sub(r"[\s:：]+", "", (s or "").strip())
    for _ in range(4):   # 「中台版本分支」要连剥两次
        new = re.sub(r"(站点|站台|版本|分支|标签|tag|的)$", "", s, flags=re.IGNORECASE)
        if new == s:
            break
        s = new
    return s


def parse_groups(text):
    """
    发布单会把站点分组，同一组件按组发不同分支，例如：
        中台站点
            （国家）AR001-SiteA
        非中台站点
            （国家）AR002-SiteB
    返回 {站点编号: 分组名}。没有分组时全部为 ''。
    """
    groups, cur = {}, ""
    for line in presplit(text).splitlines():
        s = line.strip()
        if not s:
            continue
        codes = parse_sites(s)
        if codes:
            for c in codes:
                groups.setdefault(c, cur)
            continue
        # 分组标题：短、无 AR 编号、不是步骤行、不含箭头或 tag/分支关键字
        if (len(s) <= 16 and not s.startswith("【") and not re.match(r"^\d+\s*[.、]", s)
                and not re.search(r"(?i)(→|->|=>|➔|⇒|tag|分支|branch|标签)", s)):
            cur = canon_group(s)
    return groups


# 值至少要含一个字母或数字。否则「web  tag：」这种「组件名占一行、值在下面」的写法，
# 会让正则把行尾那个孤零零的冒号当成值，期望值变成「：」，于是所有站点全判「版本不符」。
_VALUE_OK = re.compile(r"[0-9A-Za-z]")

# 组件行：组件名 +（限定词）+ 箭头或 tag/分支 关键字 + 值。箭头和关键字至少要有一个，
# 否则随便一行 "a: b" 都会被当成组件。组件名允许含空格（"Web Pages"）。
_COMP_LINE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9_\- 一-龥]+?)\s*"
    r"[:：]?\s*"                       # 「ThirdApi:  →  分支: uat」组件名后面直接跟冒号
    r"(?:[（(](?P<q1>[^）)]*)[）)])?\s*"
    r"(?:(?P<arrow>→|->|=>|➔|⇒|➜)\s*)?"
    r"(?:(?P<kw>tag|分支|branch|标签|tag名|分支名)\s*)?"
    r"(?:[（(](?P<q2>[^）)]*)[）)])?\s*"
    r"[:：]?\s*(?P<val>\S+)?\s*$",
    re.IGNORECASE,
)
# 限定词行：出现在某个组件名下面，形如「非中台版本分支：main-3.04」「ar046（xx）：uat」
_QUAL_LINE = re.compile(r"^\s*(?P<label>[^:：]+?)\s*[:：]\s*(?P<val>\S+)\s*$")


# 拼错一个字母，这个组件就整个不核对 —— 而「没核对」在报告里长得跟「没问题」一模一样。
# 实际发生过：发布单写 webIntrenetApi（Intranet 的 a/e 对调），WebIntranetApi 那一栏
# 就此空白，没人会注意到少了一个组件。所以对认不出来的名字做一次近似匹配。
#
# 阈值 0.80 是量出来的，不是拍的：
#   • 那个真实错拼对 WebIntranetApi 是 0.929；
#   • 而**任意两个真实组件之间**最高只有 0.625（ThirdApi vs ThirdJob）；
#   • 乱写的字符串（"api"/"aaa"）最高 0.545。
# 0.80 卡在中间一大段空档里。改阈值前先把这三个数重新量一遍（tools\selftest.py 有守）。
FUZZY_MIN = 0.80
FUZZY_GAP = 0.06        # 最像的和第二像的差不到这么多 = 分不清，宁可不猜
FUZZY_MIN_LEN = 5       # 太短的名字容易撞上，"api" 这种一律不猜


def _fuzzy_pool():
    """近似匹配的候选池：Jenkins 组件名 + 纯 ASCII 的别名 + 不在本 Jenkins 的那几个。

    只收 ASCII：中文别名（前端/页面/后台）本来就短，逐字比相似度容易乱配，
    而它们精确匹配已经够用了。
    """
    pool = {}
    for v in set(COMPONENT_ALIAS.values()):
        pool[canon(v)] = (v, False)
    for k, v in COMPONENT_ALIAS.items():
        pool.setdefault(k, (v, False))
    for k in NOT_IN_JENKINS:
        pool.setdefault(k, (None, True))
    return {k: v for k, v in pool.items() if k.isascii() and k.isalnum()}


def fuzzy_component(key):
    """认不出来的名字 -> (组件名, 不在本Jenkins, 相似度)。拿不准就返回 (None, False, 0)。"""
    if len(key) < FUZZY_MIN_LEN or not key.isascii() or not key.isalnum():
        return None, False, 0.0
    scored = sorted(((difflib.SequenceMatcher(None, key, k).ratio(), k)
                     for k in _fuzzy_pool()), reverse=True)
    if not scored or scored[0][0] < FUZZY_MIN:
        return None, False, 0.0
    if len(scored) > 1 and scored[0][0] - scored[1][0] < FUZZY_GAP:
        # 两个候选一样像：猜错比不猜更糟，因为报告会理直气壮地按错的那个核对
        return None, False, 0.0
    name, not_here = _fuzzy_pool()[scored[0][1]]
    return name, not_here, scored[0][0]


def resolve_component(raw, fuzzy=False):
    """发布单里的组件写法 -> Jenkins 组件名。

    返回 (组件名, 是否属于「不在这套 Jenkins」, 近似匹配到的相似度)。
    相似度 >0 表示这次是**猜**的，调用方必须把这件事告诉用户。
    """
    key = canon(raw)
    if not key:
        return None, False, 0.0
    if key in NOT_IN_JENKINS:
        # 后台那套配好了就当成正常组件核对，没配才退回「不在这套 Jenkins」提示
        return (ADMIN_COMP, False, 0.0) if admin_ready() else (None, True, 0.0)
    name = COMPONENT_ALIAS.get(key)
    if not name:                       # 也允许直接写 Jenkins 里的组件名本身
        for v in set(COMPONENT_ALIAS.values()):
            if canon(v) == key:
                name = v
                break
    if name:
        return name, False, 0.0
    if fuzzy:
        return fuzzy_component(key)
    return None, False, 0.0


def parse_components(text):
    """
    从发布单【发布代码】段抠出 组件 -> 期望 tag/分支。三种写法都要吃下：

      A) 一行写完：      LotteryApi  →  tag: master_V3.08_001
      B) 一行带限定词：  web  tag（中台）: feature/v3
      C) 组件名单独一行，下面按分组/站点列各自的分支：
             web  tag：
                 非中台版本分支：  masterBranch/main-3.04
                 中台版本分支：    feature/v3
                 ar046（代收手续费版本）： uat     <- 站点级特例

    返回 (components, warnings)。component = {key,name,expect,raw,group,site}，
    group 为空表示对所有站点生效；site 非空表示只对该站点生效（优先级高于 group）。
    """
    comps, warns, seen = [], [], set()
    cur_comp = None      # C) 里「当前组件」
    cur_raw = ""

    def add(name, raw, expect, group="", site=""):
        k = (name, group, site)
        if k in seen:
            return
        seen.add(k)
        comps.append({"key": "%s@%s@%s" % (name, group, site), "name": name,
                      "expect": expect, "raw": raw, "group": group, "site": site})

    for line in presplit(text).splitlines():
        if not line.strip():
            continue
        m = _COMP_LINE.match(line)
        if m:
            raw = m.group("name").strip()
            val = (m.group("val") or "").strip()
            has_marker = bool(m.group("arrow") or m.group("kw"))
            # 站点行「（国家）AR001→SiteA」也带箭头，会撞进来，按 AR 编号剔掉
            is_site_line = bool(re.search(r"(?i)\bAR\d{2,4}\b", raw))
            # 只在组件行开近似匹配。限定词行（下面那段）不开：那些是「非中台版本分支」
            # 之类的说明文字，让它们也去猜组件名，会把说明当成组件误吃掉。
            name, not_here, sim = resolve_component(raw, fuzzy=not is_site_line)

            if not_here and has_marker:
                extra = f"（按「{raw}」最接近的名字判断，相似度 {sim:.0%}）" if sim else ""
                warns.append(f"发布单里的「{raw} → {val}」不在这套 Jenkins 上"
                             f"（属于另一套后台部署），本次未核对{extra}")
                cur_comp = None
                continue

            if name and sim:
                # 猜出来的必须说出来：报告里「没核对」和「没问题」长得一样，
                # 猜错了他会拿着一份自信的错报告去交差。
                warns.append(f"发布单里写的「{raw}」在 Jenkins 上没有，"
                             f"已按最接近的「{name}」核对（相似度 {sim:.0%}）。"
                             f"如果不对，改掉发布单里的拼写再跑一次。")

            if name and not is_site_line:
                if val and _VALUE_OK.search(val):
                    add(name, raw, val, canon_group(m.group("q2") or m.group("q1") or ""))
                    cur_comp, cur_raw = name, raw   # 后面可能还跟着限定词行
                else:
                    # C) 组件名占一行、值在下面几行。这里不能当成组件行处理，
                    # 否则会把行尾的冒号当成期望值。
                    cur_comp, cur_raw = name, raw
                continue

            if has_marker and not name and not is_site_line and val and _VALUE_OK.search(val):
                # 形似组件行但名字不认识。带箭头的一律当组件行告警；
                # 不带箭头、且正处在某个组件下面的，才可能是限定词行（见下）。
                # 不这么分的话「随便写个啥 → tag: v2」会被吞成上一个组件的分组，静默出错。
                if cur_comp is None or m.group("arrow"):
                    warns.append(f"不认识的组件名「{raw} → {val}」，已跳过（未核对）。"
                                 f"可用：{', '.join(sorted(set(COMPONENT_ALIAS.values())))}")
                    continue

        # C) 限定词行：只有在「当前组件」已确定、且本行没有箭头时才解释，避免误吃正文
        if cur_comp and not re.search(r"→|->|=>|➔|⇒|➜", line):
            q = _QUAL_LINE.match(line)
            if q:
                label, val = q.group("label").strip(), q.group("val").strip()
                if not _VALUE_OK.search(val):
                    continue
                if resolve_component(label)[0]:
                    continue                      # 是组件名，交给上面处理
                codes = parse_sites(label)
                if codes:                          # ar046（代收手续费版本）： uat
                    add(cur_comp, cur_raw, val, site=codes[0])
                else:                              # 非中台版本分支： main-3.04
                    g = canon_group(re.sub(r"[（(][^）)]*[）)]", "", label))
                    if g:
                        add(cur_comp, cur_raw, val, group=g)
    return comps, warns


def read_build(b):
    ts = int(b.get("timestamp") or 0) / 1000.0
    acts = [a for a in (b.get("actions") or []) if isinstance(a, dict)]
    params = []
    for a in acts:
        if a.get("parameters"):
            params.extend(a["parameters"])
    # 参数名不统一：Api/Job/Web 类叫 TAG，Pages 类叫 BRANCH_NAME，
    # 后台那套（Sit-Admin）叫 BRANCH。三个都试。
    want = ""
    for p in params:
        if p.get("name") in ("TAG", "BRANCH_NAME", "BRANCH"):
            want = p.get("value") or ""
            break
    got, sha = "", ""
    for a in acts:
        rev = a.get("lastBuiltRevision")
        if rev:
            br = (rev.get("branch") or [{}])[0]
            got = br.get("name") or ""
            sha = (rev.get("SHA1") or "")[:7]
            break
    who = ""
    for a in acts:
        if a.get("causes"):
            c = a["causes"][0]
            who = c.get("userName") or c.get("shortDescription") or ""
            break
    return {
        "num": b.get("number"),
        "result": "BUILDING" if b.get("building") else (b.get("result") or ""),
        "ts": ts,
        "time": datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S") if ts else "",
        "dur": round((b.get("duration") or 0) / 1000),
        "want": normalize_ref(want),
        "got": normalize_ref(got),
        "sha": sha,
        "who": who,
        "params": "; ".join(f"{p.get('name')}={p.get('value')}" for p in params),
        "url": b.get("url") or "",
    }


def run_check(text, date_str, days, exclude, all_sites, force, user, token_):
    comps, warns = parse_components(text)
    sites = parse_sites(text)
    site_group = parse_groups(text)      # {站点: 分组}，无分组时为 ''

    day0 = datetime.strptime(date_str, "%Y-%m-%d")
    frm = day0.timestamp()
    to = (day0 + timedelta(days=max(1, days))).timestamp()

    # 解析不出组件就没必要打 Jenkins 了 —— 粘错内容是最常见的情况，别让它等几十秒
    if not comps:
        warns.append("发布单里没解析出组件行（形如「LotteryApi → tag: xxx」或「web → 分支: xxx」），请检查粘贴内容")
        return {"ok": False, "warnings": warns, "sites": sites, "components": [], "rows": []}

    jobs = fetch_job_names(user, token_, force=force)
    known = sorted({site_of(j["name"]) for j in jobs if site_of(j["name"])})
    comp_names = {c["name"] for c in comps}

    # (站点, 组件) -> job 列表。建一次索引，替代「每个站点每个组件都线性扫一遍全部 job」，
    # 六百个 job × 几百个组合原本是三十万次正则匹配。
    index = {}
    for j in jobs:
        s, cp = site_of(j["name"]), component_of(j["name"])
        if s and cp:
            index.setdefault((s, cp), []).append(j)

    if all_sites:
        # 全量：凡是拥有本次任一组件的站点全部纳入，清单一行不用写，也就抄不漏
        sites = sorted({s for (s, cp) in index if cp in comp_names})
        src = "全量（所有拥有这些组件的站点）"
    else:
        src = "发布单正文解析"
    if not sites:
        warns.append("没解析出任何 AR 站点编号")

    exclude = {e.strip().upper() for e in exclude if e.strip()}
    if exclude:
        sites = [s for s in sites if s not in exclude]
    unknown = [s for s in sites if s not in known]

    # 站点展示名：优先取 Pages 任务的最后一段（英文站点名，如 tiranga），
    # 没有就退而取任意 4 段以上任务名的最后一段。直接遍历取第一个会拿到「印度」这种国家名。
    disp = {}
    for prefer in ("Pages", "LotteryApi", None):
        for j in jobs:
            s = site_of(j["name"])
            if not s or s in disp:
                continue
            if prefer and component_of(j["name"]) != prefer:
                continue
            parts = j["name"].split("-")
            if len(parts) >= 4 and parts[-1]:
                disp[s] = parts[-1]

    # 组件带了分组限定词（如「web tag（中台）」）时，只核对属于该分组的站点。
    # 没有限定词的组件对所有站点生效。
    group_names = {c["group"] for c in comps if c["group"]}
    if group_names:
        unmatched = sorted({site_group.get(s, "") for s in sites} - group_names - {""})
        if unmatched:
            warns.append("这些站点分组在发布代码里没有对应的分支说明，未核对：" + ", ".join(unmatched))

    # 站点级特例（如「ar046（代收手续费版本）：uat」）优先级最高：
    # 该站点只按特例那条核对，不再套用它所属分组的分支，否则会重复且互相矛盾。
    override = {}
    for c in comps:
        if c.get("site"):
            override.setdefault(c["name"], set()).add(c["site"])

    # 先排计划（哪些组件×站点×job 要看），再一次性把这些 job 的构建历史拉回来。
    # 分两趟是为了「按需拉」：只有排进计划的 job 才值得花一次请求。
    plan, wanted, nogroup = [], set(), set()
    for c in comps:
        if c["name"] == ADMIN_COMP:
            continue          # 后台不按站点发，单独处理（见下面 admin_rows）
        if c.get("site"):
            comp_sites = [s for s in sites if s == c["site"]]
            if not comp_sites:
                warns.append(f"发布代码里给 {c['site']} 单独指定了分支，但站点清单里没有这个站点")
        else:
            comp_sites = []
            for s in sites:
                if s in override.get(c["name"], set()):
                    continue
                if c["group"]:
                    g = site_group.get(s, "")
                    if g != c["group"]:
                        # 分组对不上就跳过。但「压根没有分组归属」得记下来单独告警 ——
                        # 尤其是勾了「核对全部站点」时，站点来自 Jenkins 而分组只写在发布单正文里，
                        # 大部分站点的分组都是空的，会被整片静默跳过。
                        if not g:
                            nogroup.add(s)
                        continue
                comp_sites.append(s)
            if c["group"] and not comp_sites:
                warns.append(f"发布代码里写了「{c['raw']}（{c['group']}）」，但站点清单里没有「{c['group']}」分组的站点")
        for s in comp_sites:
            matched = index.get((s, c["name"]), [])
            plan.append((c, s, matched))
            wanted.update(j["name"] for j in matched)

    if nogroup:
        lst = sorted(nogroup)
        warns.append("这 %d 个站点在发布单正文里找不到分组归属，所有带分组的组件都没核对它们"
                     "%s：%s%s" % (len(lst), "（勾了「核对全部站点」时常见）" if all_sites else "",
                                   ", ".join(lst[:12]), "…" if len(lst) > 12 else ""))

    builds_map, fetch_warns = fetch_builds(sorted(wanted), user, token_, force=force)
    warns.extend(fetch_warns)

    rows, truncated = [], set()
    for c, s, matched in plan:
        expect = normalize_ref(c["expect"])
        if not matched:
            rows.append({"site": s, "siteName": disp.get(s, ""), "comp": c["name"], "compKey": c["key"],
                         "group": c["group"], "onlySite": c.get("site",""), "view": COMPONENT_VIEW.get(c["name"], ""), "job": "",
                         "state": "NOJOB", "expect": c["expect"], "detail": "该站点没有这个任务"})
            continue
        for j in matched:
            e = builds_map.get(j["name"]) or {"builds": [], "trunc": False}
            inwin = sorted([x for x in e["builds"] if frm <= x["ts"] < to],
                           key=lambda x: x["ts"], reverse=True)
            # 同站点同组件可能有多个 job（如 某站点的 Pages 有 5 个），
            # 用去掉「AR###-组件-」前缀后的剩余部分区分，否则色块长得一模一样
            suffix = re.sub(r"^%s-%s-?" % (re.escape(s), re.escape(c["name"])), "", j["name"])
            base = {"site": s, "siteName": disp.get(s, ""), "comp": c["name"], "compKey": c["key"],
                    "group": c["group"], "onlySite": c.get("site",""), "view": COMPONENT_VIEW.get(c["name"], ""), "job": j["name"],
                    "jobSuffix": suffix if len(matched) > 1 else "", "expect": c["expect"]}
            if not inwin:
                # Jenkins 只给最近 N 次构建。若这 N 次全都晚于时间窗，那窗口内那次很可能
                # 是被截掉了没拿到 —— 这时报「未发布」是错的，会让人去重发一个已经发过的站点。
                cut = e["trunc"] and e["builds"] and min(x["ts"] for x in e["builds"]) > frm
                if cut:
                    truncated.add(j["name"])
                rows.append(dict(base, state="MISS", url=j.get("url", ""),
                                 detail=("最近 %d 次构建都晚于时间窗，可能没查全（不一定真的没发）" % MAX_BUILDS_PER_JOB)
                                        if cut else "时间窗内没有构建"))
                continue
            b = inwin[0]  # 窗口内最后一次构建为准
            actual = b["want"] or b["got"]
            if b["result"] == "BUILDING":
                st, detail = "RUN", f"#{b['num']} 正在构建中"
            elif b["result"] != "SUCCESS":
                st, detail = "FAIL", f"#{b['num']} {b['result']}  {b['time']}  by {b['who']}"
            elif expect and not actual:
                # 构建成功了，但既没有 TAG/BRANCH_NAME 参数也没有 lastBuiltRevision，
                # 无从判断发的是哪个版本。以前这种落 OK（绿色），等于把「没核对」显示成「核对通过」。
                st, detail = "NOVER", f"#{b['num']} 构建成功，但没记录分支/tag，版本无法核对"
            elif expect and actual and normalize_ref(actual) != expect:
                st, detail = "VER", f"#{b['num']} 期望 {c['expect']}，实际 {actual}"
            else:
                st, detail = "OK", f"#{b['num']} {b['time']}  {b['sha']}  by {b['who']}"
            rows.append(dict(base, state=st, detail=detail, num=b["num"], result=b["result"],
                             time=b["time"], ts=b["ts"], sha=b["sha"], who=b["who"], actual=actual,
                             dur=b["dur"], url=b["url"]))

    # ── 后台：另一套 Jenkins，不按站点发 ─────────────────────────────
    # 这里刻意**不**造出「每个站点一行 Admin」——后台是整个平台一套，
    # 硬塞进站点网格会让人以为它是按站点发的，那是编出来的信息。
    # 一个 job 一行，和它在那边的实际粒度一致。
    for c in comps:
        if c["name"] != ADMIN_COMP:
            continue
        expect = normalize_ref(c["expect"])
        amap, awarns, ajobs = fetch_admin_builds(force=force)
        warns.extend(awarns)
        if not amap:
            # 一条都没拉到 = 根本没连上那台。这时**不能出任何后台行**：
            # 出一行「没取到这个任务」看着像后台缺了这个 job，而事实是我们没连上，
            # 那是把「不知道」显示成了「查过了」。
            warns.append("发布单里要发后台，但这次一条后台数据都没拉到（原因见上），"
                         "**后台等于没核对**。后台发没发请人工确认。")
            continue
        for jobname in ajobs:
            if jobname not in amap:
                continue          # 这个 job 没拉到，上面已单独告警，别造行

            # 构建那一步才带版本号；部署 job 只是点 Build Now，没有版本可比。
            # 拿部署 job 去比版本会全判「版本未知」，那是纯噪音。
            is_build = (jobname == ADMIN_BUILD_JOB)
            e = amap.get(jobname)
            base = {"site": "后台构建" if is_build else "后台部署",
                    "siteName": jobname, "comp": ADMIN_COMP,
                    "compKey": c["key"], "group": c["group"], "onlySite": "",
                    "view": ADMIN_VIEW, "job": jobname, "jobSuffix": "",
                    "expect": c["expect"] if is_build else "", "url": (e or {}).get("url", "")}
            if e is None:
                rows.append(dict(base, state="NOJOB", detail="后台 Jenkins 没取到这个任务"))
                continue
            inwin = sorted([x for x in e["builds"] if frm <= x["ts"] < to],
                           key=lambda x: x["ts"], reverse=True)
            if not inwin:
                cut = e["trunc"] and e["builds"] and min(x["ts"] for x in e["builds"]) > frm
                if cut:
                    truncated.add(jobname)
                rows.append(dict(base, state="MISS",
                                 detail=("最近 %d 次构建都晚于时间窗，可能没查全（不一定真的没发）"
                                         % MAX_BUILDS_PER_JOB) if cut
                                 else ("时间窗内没构建过" if is_build else "时间窗内没执行过部署")))
                continue
            b = inwin[0]
            actual = b["want"] or b["got"]
            if b["result"] == "BUILDING":
                st, detail = "RUN", f"#{b['num']} 正在{'构建' if is_build else '部署'}中"
            elif b["result"] != "SUCCESS":
                st, detail = "FAIL", f"#{b['num']} {b['result']}  {b['time']}  by {b['who']}"
            elif not is_build:
                # 部署 job：跑过且成功就是达成。这里不谈版本 ——
                # 它部署的是 build_admin 产出的那份制品，版本对不对由构建那行负责。
                st = "OK"
                detail = f"#{b['num']} 已执行 {b['time']}  by {b['who']}"
            elif expect and not actual:
                st, detail = "NOVER", f"#{b['num']} 构建成功，但没记录版本参数，无法核对"
            elif expect and actual and normalize_ref(actual) != expect:
                st, detail = "VER", f"#{b['num']} 期望 {c['expect']}，实际 {actual}"
            else:
                st, detail = "OK", f"#{b['num']} {b['time']}  {b['sha']}  by {b['who']}"
            rows.append(dict(base, state=st, detail=detail, num=b["num"], result=b["result"],
                             time=b["time"], ts=b["ts"], sha=b["sha"], who=b["who"],
                             actual=actual if is_build else "", dur=b["dur"],
                             url=b["url"] or base["url"]))
        # 构建成功但一个部署都没跑 —— 最容易漏的一步：制品打好了，忘了去点部署。
        arows = [x for x in rows if x["comp"] == ADMIN_COMP]
        built_ok = any(x["site"] == "后台构建" and x["state"] == "OK" for x in arows)
        deployed = [x for x in arows if x["site"] == "后台部署" and x["state"] == "OK"]
        if built_ok and ajobs and not deployed:
            warns.append("后台 build_admin 这一版构建成功了，但「%s」视图里**一个部署都没执行**。"
                         "制品打好了没发出去 —— 去点 Build Now。" % ADMIN_DEPLOY_VIEW)

    summary = {}
    for r in rows:
        summary[r["state"]] = summary.get(r["state"], 0) + 1
    if unknown:
        warns.append("这些编号 Jenkins 上不存在，请核对发布单：" + ", ".join(unknown))
    if truncated:
        warns.append("有 %d 个任务判成「未发布」，但它们最近 %d 次构建全都晚于时间窗 —— "
                     "很可能是发过之后又构建了很多次、被截断没查到。把 JENKINS_MAX_BUILDS 调大，"
                     "或把发版日期改准，再核对一次。" % (len(truncated), MAX_BUILDS_PER_JOB))

    snap = [_bucket(user)["names"]] + [builds_map[n] for n in sorted(wanted) if n in builds_map]
    return {"ok": True, "source": src, "date": date_str, "days": days,
            "sites": sites, "unknown": unknown, "components": comps,
            "rows": rows, "summary": summary, "warnings": warns,
            "tz": tz_label(), "cachedAt": cached_at(snap)}


# ---------------------------------------------------------------- HTTP
PAGE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jenkins 发版核对</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1a1d21;--mut:#6b7280;--bd:#e3e6ea;
--ok:#0f9960;--okbg:#e6f5ee;--bad:#d3392f;--badbg:#fdeceb;--warn:#b7791f;--warnbg:#fdf6e3;
--run:#1c7ed6;--runbg:#e7f2fd;--none:#9aa3ad;--nonebg:#f0f2f4;}
@media(prefers-color-scheme:dark){:root{--bg:#14171a;--card:#1c2024;--fg:#e6e8ea;--mut:#9aa3ad;--bd:#2c3238;
--okbg:#122b20;--badbg:#2e1614;--warnbg:#2c2413;--runbg:#12212f;--nonebg:#22272b;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:24px 18px 60px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--mut);font-size:13px;margin-bottom:18px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:16px;margin-bottom:16px}
textarea{width:100%;min-height:190px;padding:11px;border:1px solid var(--bd);border-radius:8px;
background:var(--bg);color:var(--fg);font:12.5px/1.6 Consolas,"Cascadia Mono",monospace;resize:vertical}
.ctl{display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end;margin-top:12px}
.f{display:flex;flex-direction:column;gap:5px}
.f label{font-size:12px;color:var(--mut)}
input[type=date],input[type=number],input[type=text]{padding:7px 9px;border:1px solid var(--bd);
border-radius:7px;background:var(--bg);color:var(--fg);font:13px inherit}
input[type=text]{min-width:210px}
.chk{flex-direction:row;align-items:center;gap:7px;padding-bottom:7px}
button{padding:9px 20px;border:0;border-radius:7px;background:#2b6cb0;color:#fff;font-size:14px;
font-weight:600;cursor:pointer}
button:hover{background:#255990}button:disabled{opacity:.55;cursor:default}
button.sec{background:transparent;color:var(--mut);border:1px solid var(--bd);font-weight:400}
.pills{display:flex;flex-wrap:wrap;gap:8px;margin:2px 0 14px}
.pill{padding:4px 12px;border-radius:20px;font-size:13px;font-weight:600}
.OK{background:var(--okbg);color:var(--ok)}.MISS,.FAIL{background:var(--badbg);color:var(--bad)}
.VER,.NOVER{background:var(--warnbg);color:var(--warn)}.RUN{background:var(--runbg);color:var(--run)}
.NOJOB{background:var(--nonebg);color:var(--none)}
.warn{background:var(--warnbg);color:var(--warn);border-radius:8px;padding:9px 12px;margin-bottom:9px;font-size:13px}
h2{font-size:15px;margin:22px 0 4px;display:flex;align-items:baseline;gap:9px;flex-wrap:wrap}
h2 .tag{font:12px Consolas,monospace;color:var(--mut);font-weight:400}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(215px,1fr));gap:7px;margin-top:9px}
.cell{border:1px solid var(--bd);border-radius:7px;padding:7px 9px;font-size:12.5px;cursor:default}
.cell b{display:block;font-size:13px}
.cell small{color:var(--mut);font-size:11px}
.br{font:12px Consolas,"Cascadia Mono",monospace;margin:3px 0 1px;word-break:break-all}
.br.diff{color:var(--bad);font-weight:700}
.sfx{display:block;font:10.5px Consolas,monospace;color:var(--mut);opacity:.8;margin-top:2px}
details{margin-top:10px}
summary{cursor:pointer;color:var(--mut);font-size:12.5px;user-select:none}
summary:hover{color:var(--fg)}
.cell.s-OK{background:var(--okbg);border-color:transparent}
.cell.s-MISS,.cell.s-FAIL{background:var(--badbg);border-color:transparent}
.cell.s-VER,.cell.s-NOVER{background:var(--warnbg);border-color:transparent}
.cell.s-RUN{background:var(--runbg);border-color:transparent}
.cell.s-NOJOB{background:var(--nonebg);border-color:transparent;opacity:.65}
table{width:100%;border-collapse:collapse;margin-top:9px;font-size:12.5px}
th,td{text-align:left;padding:6px 9px;border-bottom:1px solid var(--bd);vertical-align:top}
th{color:var(--mut);font-weight:600;font-size:12px}
td a{color:inherit}
.st{font-weight:700;white-space:nowrap}
.st.OK{color:var(--ok)}.st.MISS,.st.FAIL{color:var(--bad)}
.st.VER,.st.NOVER{color:var(--warn)}.st.RUN{color:var(--run)}
.empty{color:var(--mut);padding:22px;text-align:center}
.tbwrap{overflow-x:auto}
.h2r{margin-left:auto;font-size:12px;font-weight:400}
.lnk{color:var(--run);cursor:pointer;text-decoration:underline;font-size:12.5px}
#poll{color:var(--run);font-size:12.5px;margin-top:8px}
</style></head><body><div class="wrap">
<h1>Jenkins 发版核对</h1>
<div class="sub">把发布单整段复制粘进来 → 点「开始检查」。站点编号和组件 tag 都自动解析，只读不触发构建。</div>

<div class="card" id="credcard" style="display:none">
  <div class="sub" style="margin:0 0 10px">
    用你自己的 Jenkins 账号查询 —— 看得到什么由你在 Jenkins 里的权限决定。
    凭据只存在本浏览器标签页（sessionStorage），关掉即失效，服务器不保存。
    <span id="tokenlink"></span>
  </div>
  <div class="ctl" style="margin:0">
    <div class="f"><label>Jenkins 用户名</label><input type="text" id="ju" placeholder="登录名，不是邮箱"></div>
    <div class="f"><label>API Token</label><input type="password" id="jt" placeholder="在 /me/security/ 生成"></div>
    <div class="f"><button id="savecred">保存</button></div>
    <div class="f"><button id="clearcred" class="sec">清除</button></div>
    <div class="f"><span id="credstate" class="sub" style="margin:0;padding-bottom:8px"></span></div>
  </div>
</div>

<div class="card">
  <textarea id="txt" placeholder="把发布单整段粘贴到这里，例如：

【发布站点】
（国家）AR001→SiteA
（国家）AR002→SiteB
...

【发布步骤】
3.发布代码
    LotteryApi      →   tag:   master_V3.08_001
    WebIntranetApi  →   tag:   master_V3.08_001
    web             →   分支:  masterBranch/main-3.04
    ThirdApi:       →   分支:  uat
    ThirdJob:       →   分支:  uat"></textarea>
  <div class="ctl">
    <div class="f"><label>发版日期 <span id="tzhint" style="opacity:.75"></span></label><input type="date" id="date"></div>
    <div class="f"><label>往后几天</label><input type="number" id="days" value="1" min="1" max="30" style="width:76px"></div>
    <div class="f"><label>排除站点（逗号分隔）</label><input type="text" id="ex" value="AR000"></div>
    <div class="f chk"><input type="checkbox" id="all"><label for="all">忽略站点清单，核对全部站点</label></div>
    <div class="f chk"><input type="checkbox" id="prob"><label for="prob">只看有问题的</label></div>
    <div class="f"><button id="go">开始检查</button></div>
    <div class="f"><button id="refresh" class="sec" title="跳过缓存，重新拉取 Jenkins">强制刷新</button></div>
  </div>
</div>
<div id="out"></div>
</div>
<script>
const $=i=>document.getElementById(i);
const LABEL={OK:'已发布',MISS:'未发布',FAIL:'构建失败',VER:'版本不符',
             NOVER:'版本未知',RUN:'构建中',NOJOB:'无此任务'};
// 需要人看一眼的状态，顺序即严重程度。NOVER 排最后：它不是「发错了」，是「查不出发的啥」。
const BAD=['MISS','FAIL','VER','RUN','NOVER'];
// 默认日期由服务端给（见 /api/config）。别用浏览器本地日期 —— 时间窗是按服务端时区切的，
// 浏览器在 UTC+8、容器在 UTC+5:30 的话会差一天。拿不到时才回落到浏览器日期。
$('date').value=new Date().toLocaleDateString('sv');   // sv locale 天然是 YYYY-MM-DD

// ---- 发布单存本地：核对完常要刷新页面重查，每次都重新粘一遍太蠢 ----
// 存 localStorage 不存 sessionStorage：关掉标签页第二天接着核对也还在。
// 里面是内网站点清单，别跟凭据混在一起，也不上传服务端。
const DK='jrc_draft';
try{const d=localStorage.getItem(DK);if(d)$('txt').value=d;}catch(e){}
$('txt').addEventListener('input',()=>{try{localStorage.setItem(DK,$('txt').value);}catch(e){}});

// ---- 凭据：只放 sessionStorage，不落 localStorage，关标签页即失效 ----
const CK='jrc_user',TK='jrc_token';
function creds(){return {u:sessionStorage.getItem(CK)||'',t:sessionStorage.getItem(TK)||''};}
function paintCred(){
  const c=creds();
  $('credstate').textContent=c.u&&c.t?('当前：'+c.u):'未填写';
  $('ju').value=c.u;
}
$('savecred').onclick=()=>{
  const u=$('ju').value.trim(),t=$('jt').value.trim();
  if(!u||!t){alert('用户名和 Token 都要填');return;}
  sessionStorage.setItem(CK,u);sessionStorage.setItem(TK,t);$('jt').value='';paintCred();
};
$('clearcred').onclick=()=>{sessionStorage.removeItem(CK);sessionStorage.removeItem(TK);$('jt').value='';paintCred();};

fetch('/api/config').then(r=>r.json()).then(cfg=>{
  if(cfg.needCreds){$('credcard').style.display='';paintCred();}
  if(cfg.today){$('date').value=cfg.today;}
  if(cfg.tz){$('tzhint').textContent='时间按 '+cfg.tz+' 显示';}
  if(cfg.jenkinsUrl){
    $('tokenlink').innerHTML=' <a href="'+esc(cfg.jenkinsUrl)+'/me/security/" target="_blank">去生成 Token →</a>';
  }
}).catch(()=>{});

// ---- 「构建中」自动复查：发版收尾时总有几个还在跑，否则要守着手点 ----
let pollTimer=null,pollLeft=0,pollRounds=0;
function stopPoll(msg){
  if(pollTimer){clearInterval(pollTimer);pollTimer=null;}
  const el=$('poll'); if(el)el.textContent=msg||'';
}
function startPoll(n){
  stopPoll();
  // 封顶 20 轮（10 分钟）。构建卡住的时候没必要一直空转打 Jenkins。
  if(pollRounds>=20){const el=$('poll');if(el)el.textContent='已自动复查 20 轮，还有 '+n+' 项在构建中，请手动刷新。';return;}
  pollLeft=30;
  const tick=()=>{
    const el=$('poll'); if(!el){stopPoll();return;}
    el.innerHTML='还有 '+n+' 项在构建中 · '+pollLeft+' 秒后自动复查 <span class="lnk" onclick="stopPoll(\'已停止自动复查\')">停止</span>';
    if(pollLeft--<=0){stopPoll();pollRounds++;check(true,true);}
  };
  tick(); pollTimer=setInterval(tick,1000);
}

async function check(force,isPoll){
  stopPoll();
  if(!isPoll)pollRounds=0;      // 手动点一次就重新开始计轮
  const t=$('txt').value.trim();
  if(!t){$('out').innerHTML='<div class="card empty">先把发布单粘贴到上面的文本框</div>';return;}
  $('go').disabled=true;$('go').textContent='检查中…';
  try{
    const c=creds();
    const hdr={'Content-Type':'application/json'};
    if(c.u&&c.t){hdr['X-Jenkins-User']=c.u;hdr['X-Jenkins-Token']=c.t;}
    const r=await fetch('/api/check',{method:'POST',headers:hdr,
      body:JSON.stringify({text:t,date:$('date').value,days:+$('days').value,
        exclude:$('ex').value.split(/[,，\s]+/),all:$('all').checked,force:!!force})});
    const d=await r.json();
    if(d.error){
      $('out').innerHTML='<div class="card warn">出错：'+esc(d.error)+'</div>';
      if(d.needCreds){$('credcard').style.display='';$('credcard').scrollIntoView({behavior:'smooth'});}
      return;
    }
    render(d);
  }catch(e){$('out').innerHTML='<div class="card warn">请求失败：'+esc(e.message)+'</div>';}
  finally{$('go').disabled=false;$('go').textContent='开始检查';}
}
$('go').onclick=()=>check(false);
$('refresh').onclick=()=>check(true);
$('prob').onchange=()=>{if(window.__d)render(window.__d);};
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
// 和后端同一套归一化：Git 插件把分支记成 refs/remotes/origin/xxx，比对前要剥前缀
const norm=s=>String(s||'').replace(/^refs\/remotes\//,'').replace(/^refs\/heads\//,'').replace(/^origin\//,'');

function render(d){
  window.__d=d;
  const only=$('prob').checked;
  let h='';
  (d.warnings||[]).forEach(w=>h+='<div class="warn">⚠ '+esc(w)+'</div>');
  if(!d.ok){$('out').innerHTML=h||'<div class="card empty">没解析出内容</div>';return;}

  h+='<div class="card"><div class="pills">';
  ['OK','VER','NOVER','FAIL','MISS','RUN','NOJOB'].forEach(k=>{if(d.summary[k])
    h+='<span class="pill '+k+'">'+LABEL[k]+' '+d.summary[k]+'</span>';});
  h+='</div><div class="sub" style="margin:0">站点来源：'+esc(d.source)+' · '+d.sites.length+
     ' 个站点 × '+d.components.length+' 个组件 · 时间窗 '+esc(d.date)+
     (d.days>1?' 起 '+d.days+' 天':'')+' · 数据快照 '+esc(d.cachedAt)+(d.tz?' ('+esc(d.tz)+')':'')+'</div></div>';

  const bad=d.rows.filter(r=>BAD.includes(r.state));
  if(bad.length){
    h+='<div class="card"><h2 style="margin-top:0">需要处理 <span class="tag">'+bad.length+' 项</span>'+
       '<span class="h2r"><button class="sec" id="copybad">复制清单</button></span></h2>'+
       '<div id="poll"></div><div class="tbwrap"><table>'+
       '<tr><th>状态</th><th>站点</th><th>任务</th><th>说明</th></tr>';
    bad.sort((a,b)=>BAD.indexOf(a.state)-BAD.indexOf(b.state));
    bad.forEach(r=>{h+='<tr><td class="st '+r.state+'">'+LABEL[r.state]+'</td><td>'+esc(r.site)+
      (r.siteName?' <small>'+esc(r.siteName)+'</small>':'')+'</td><td>'+
      (r.url?'<a href="'+esc(r.url)+'" target="_blank">'+esc(r.job||'-')+'</a>':esc(r.job||'-'))+
      '</td><td>'+esc(r.detail)+'</td></tr>';});
    h+='</table></div></div>';
  }

  h+='<div class="card">';
  d.components.forEach(c=>{
    let rows=d.rows.filter(r=>r.compKey===c.key);
    if(only)rows=rows.filter(r=>BAD.includes(r.state));
    h+='<h2>'+esc(c.name)+(c.group?' <span class=\"pill VER\" style=\"font-size:12px\">'+esc(c.group)+'</span>':'')+(c.site?' <span class=\"pill RUN\" style=\"font-size:12px\">仅 '+esc(c.site)+'</span>':'')+' <span class=\"tag\">视图 '+esc(d.rows.find(r=>r.compKey===c.key)?.view||'')+
       ' · 发布单写的是「'+esc(c.raw)+'」 · 期望 '+esc(c.expect)+'</span></h2>';
    if(!rows.length){h+='<div class="sub" style="margin:6px 0 0">'+(only?'该组件全部通过':'无数据')+'</div>';return;}
    // 分支名相同不等于代码相同：期间有人往分支推了新提交，先发的站点就落后了。
    // 这个只有比 SHA 才看得出来，光看分支名会漏。
    const all=d.rows.filter(r=>r.compKey===c.key&&r.sha);
    const shas={};all.forEach(r=>{(shas[r.sha]=shas[r.sha]||[]).push(r)});
    const keys=Object.keys(shas);
    if(keys.length>1){
      // 用后端给的时间戳数值排。别用 time 字符串——Math.max("07-14 16:49:00") 是 NaN，会把最旧的标成最新
      const lastTs=k=>Math.max.apply(null,shas[k].map(r=>r.ts||0));
      const firstTs=k=>Math.min.apply(null,shas[k].map(r=>r.ts||0));
      keys.sort((a,b)=>lastTs(b)-lastTs(a));          // 最新的排最前
      const newest=keys[0];
      const fmt=t=>{const d=new Date(t*1000);const p=n=>String(n).padStart(2,'0');
                    return p(d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes());};
      h+='<div class="warn" style="margin:8px 0 0">⚠ 分支名一样但代码不一样：本组件出现 '+keys.length+
         ' 个不同 SHA，说明期间分支上有新提交，先发的站点代码是旧的。'+
         keys.map(k=>'<br>&nbsp;&nbsp;<code>'+esc(k)+'</code> · '+
           [...new Set(shas[k].map(r=>r.site))].length+' 个站点 · '+
           fmt(firstTs(k))+' ~ '+fmt(lastTs(k))+
           (k===newest?' <b>（最新）</b>':' <b style="color:var(--bad)">（落后）</b>')+' · '+
           esc([...new Set(shas[k].map(r=>r.site))].join(', '))).join('')+
         '</div>';
    }
    h+='<div class="grid">';
    rows.forEach(r=>{
      const diff=r.actual&&norm(r.actual)!==norm(c.expect);
      h+='<div class="cell s-'+r.state+'" title="'+esc(r.job+'\n'+r.detail)+'">'+
      '<b>'+esc(r.site)+(r.siteName?' <small>'+esc(r.siteName)+'</small>':'')+'</b>'+
      '<div class="br'+(diff?' diff':'')+'">'+esc(r.actual||'—')+'</div>'+
      '<span class="st '+r.state+'" style="font-size:11.5px">'+LABEL[r.state]+'</span>'+
      (r.num?' <small>#'+r.num+'</small>':'')+
      (r.sha?' <small>'+esc(r.sha)+'</small>':'')+
      (r.time?' <small>'+esc(r.time.slice(0,11))+'</small>':'')+
      (r.jobSuffix?'<span class="sfx">'+esc(r.jobSuffix)+'</span>':'')+'</div>';});
    h+='</div>';
    // 明细表：色块看趋势，表格看逐条，可复制可贴群
    h+='<details><summary>展开明细表（'+rows.length+' 条 · 站点/任务/分支/SHA/构建号/时间/触发人）</summary>'+
       '<div class="tbwrap"><table><tr><th>状态</th><th>站点</th><th>任务</th><th>实际分支/tag</th>'+
       '<th>SHA</th><th>构建</th><th>时间</th><th>耗时</th><th>触发人</th></tr>';
    rows.forEach(r=>{
      const diff=r.actual&&norm(r.actual)!==norm(c.expect);
      h+='<tr><td class="st '+r.state+'">'+LABEL[r.state]+'</td><td>'+esc(r.site)+
        (r.siteName?' <small>'+esc(r.siteName)+'</small>':'')+'</td><td>'+
        (r.url?'<a href="'+esc(r.url)+'" target="_blank">'+esc(r.job||'-')+'</a>':esc(r.job||'-'))+
        '</td><td'+(diff?' style="color:var(--bad);font-weight:700"':'')+'>'+esc(r.actual||'—')+'</td><td>'+
        esc(r.sha||'')+'</td><td>'+(r.num?'#'+r.num:'')+'</td><td>'+esc(r.time||'')+'</td><td>'+
        (r.dur?r.dur+'s':'')+'</td><td>'+esc(r.who||'')+'</td></tr>';});
    h+='</table></div></details>';
  });
  h+='</div>';
  $('out').innerHTML=h;

  // 核对完要把结果贴到群里。表格里手选会连样式一起带走，这里直接给纯文本。
  const cb=$('copybad');
  if(cb)cb.onclick=()=>{
    const txt=['发版核对 '+d.date+(d.days>1?' 起 '+d.days+' 天':'')+' · '+
               BAD.filter(k=>d.summary[k]).map(k=>LABEL[k]+' '+d.summary[k]).join(' / ')]
      .concat(bad.map(r=>'['+LABEL[r.state]+'] '+r.site+(r.siteName?'('+r.siteName+')':'')+
                          ' '+(r.job||'-')+'  '+(r.detail||''))).join('\n');
    const done=()=>{cb.textContent='已复制';setTimeout(()=>cb.textContent='复制清单',1500);};
    if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(txt).then(done,()=>fallback(txt,done));}
    else fallback(txt,done);
  };
  // 构建中的项会自己变，隔 30 秒重查一次，省得守着手点
  if(d.summary.RUN)startPoll(d.summary.RUN);
}

function fallback(txt,done){
  const ta=document.createElement('textarea');
  ta.value=txt;ta.style.position='fixed';ta.style.opacity='0';
  document.body.appendChild(ta);ta.select();
  try{document.execCommand('copy');done();}catch(e){alert('复制失败，请手动选中表格');}
  document.body.removeChild(ta);
}
</script></body></html>"""


PAGE_BYTES = PAGE.encode("utf-8")   # 18KB，每次 GET 都重新 encode 是白干


class Handler(BaseHTTPRequestHandler):
    # 开 keep-alive：一次核对要打 /api/config + /api/check，HTTP/1.0 每次都得重新建连接
    # （含 TLS 握手）。所有响应都带准确的 Content-Length，可以安全地开。
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype):
        raw = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def creds(self):
        """
        取本次请求使用的 Jenkins 凭据。
        多用户部署时每个人在网页上填自己的用户名 + Token，随请求头带上来，服务端不落盘；
        这样「谁能看到什么」直接由 Jenkins 自身权限决定，不需要另做一套账号体系。
        本机自用时可以回落到服务端环境变量（JENKINS_ALLOW_SERVER_CREDS=1）。
        """
        u = (self.headers.get("X-Jenkins-User") or "").strip()
        t = (self.headers.get("X-Jenkins-Token") or "").strip()
        if u and t:
            return u, t
        if ALLOW_SERVER_CREDS and JENKINS_USER and JENKINS_TOKEN:
            return JENKINS_USER, JENKINS_TOKEN
        return "", ""

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            self._send(200, PAGE_BYTES, "text/html")
        elif p == "/api/config":
            # 前端据此决定要不要显示「填写自己的 Jenkins 凭据」表单。
            # today/tz 也由服务端给：时间窗是按服务端时区切的，若用浏览器本地日期当默认值，
            # 两边时区不同就会差一天（比如浏览器 UTC+8、容器 UTC+5:30）。
            self._send(200, json.dumps({
                "jenkinsUrl": JENKINS_URL,
                "needCreds": not (ALLOW_SERVER_CREDS and JENKINS_USER and JENKINS_TOKEN),
                "today": datetime.now().strftime("%Y-%m-%d"),
                "tz": tz_label(),
            }, ensure_ascii=False), "application/json")
        elif p == "/healthz":
            self._send(200, "ok", "text/plain")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        if self.path != "/api/check":
            self.close_connection = True       # body 没读，keep-alive 下必须断开
            self._send(404, json.dumps({"error": "not found"}), "application/json")
            return
        try:
            # 先把请求体读掉再做别的检查：开了 keep-alive，任何「没读完 body 就回包」
            # 都会让连接里剩下的字节被当成下一个请求的开头，整条连接就串了。
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                self.close_connection = True
                self._send(413 if n > MAX_BODY else 400, json.dumps(
                    {"error": "请求体大小不合法（%d 字节）" % n}, ensure_ascii=False), "application/json")
                return
            body = self.rfile.read(n)
            if not JENKINS_URL:
                raise RuntimeError("服务端没配 JENKINS_URL")
            user, token_ = self.creds()
            if not user or not token_:
                self._send(200, json.dumps(
                    {"error": "请先在页面右上角填写你自己的 Jenkins 用户名和 API Token", "needCreds": True},
                    ensure_ascii=False), "application/json")
                return
            req = json.loads(body.decode("utf-8"))
            res = run_check(
                req.get("text", ""),
                req.get("date") or datetime.now().strftime("%Y-%m-%d"),
                int(req.get("days") or 1),
                req.get("exclude") or [],
                bool(req.get("all")),
                bool(req.get("force")),
                user, token_,
            )
            self._send(200, json.dumps(res, ensure_ascii=False), "application/json")
        except urllib.error.HTTPError as e:
            msg = friendly_http(e)
            if e.code in (401, 403):
                self._send(200, json.dumps({"error": msg, "needCreds": e.code == 401},
                                           ensure_ascii=False), "application/json")
                return
            self._send(200, json.dumps({"error": msg}, ensure_ascii=False), "application/json")
        except urllib.error.URLError as e:
            self._send(200, json.dumps({"error": friendly_neterr(e)}, ensure_ascii=False), "application/json")
        except Exception as e:
            self._send(200, json.dumps({"error": "%s: %s" % (type(e).__name__, e)}, ensure_ascii=False), "application/json")


def probe_admin():
    """`python app.py --probe-admin` —— 后台 Jenkins 连不上时的自检。

    必须在**容器里**跑：宿主机是 CentOS 7.9（OpenSSL 1.0.2k，最高 TLS 1.2），
    而 WAF 只收 TLS 1.3，宿主机上 curl 连握手都成功不了，`-s` 还会把错误吞掉，
    表现成「什么都没输出」，看着像没反应，其实是根本没连上。
        docker compose exec jenkins-check python /app/app.py --probe-admin
    """
    print("=== 后台 Jenkins 自检 ===")
    print("ADMIN_JENKINS_URL   = %s" % (ADMIN_URL or "(空)"))
    print("ADMIN_JENKINS_USER  = %s" % (ADMIN_USER or "(空)"))
    print("ADMIN_JENKINS_TOKEN = %s" % ("已设置，%d 位" % len(ADMIN_TOKEN) if ADMIN_TOKEN else "(空)"))
    print("构建 job / 部署视图  = %s / %s" % (ADMIN_BUILD_JOB, ADMIN_DEPLOY_VIEW))
    if not admin_ready():
        print("\n结论：三项没配齐，容器里读不到 —— 后台不会被核对。")
        print("      改的是 /data/Check-JenkinsRelease/.env 吗？改完必须 "
              "`docker compose up -d --build`，只 restart 不会重新读镜像里的代码。")
        return 1
    import urllib.error
    waf = False
    for label, path in (("列 %s 视图" % ADMIN_DEPLOY_VIEW,
                         "/view/" + urllib.parse.quote(ADMIN_DEPLOY_VIEW, safe="") + "/api/json"
                         + _q("jobs[name]")),
                        ("读 %s" % ADMIN_BUILD_JOB,
                         "/job/" + urllib.parse.quote(ADMIN_BUILD_JOB, safe="") + "/api/json"
                         + _q("builds[number]{0,1}"))):
        print("\n--- %s ---" % label)
        try:
            data = jenkins_get(path, ADMIN_USER, ADMIN_TOKEN, timeout=60, base=ADMIN_URL)
        except urllib.error.HTTPError as e:
            print("HTTP %d" % e.code)
            for k, v in dict(e.headers).items():
                if k.lower() in ("server", "cf-ray", "x-jenkins", "www-authenticate"):
                    print("   %s: %s" % (k, v))
            hl = {k.lower() for k in dict(e.headers)}
            if e.code == 403 and "x-jenkins" not in hl and "cf-ray" in hl:
                waf = True
            print("→ " + friendly_http(e, label))
            continue
        except Exception as e:
            print("连不上：%s" % (str(getattr(e, "reason", e))[:200]))
            print("→ " + friendly_neterr(e))
            continue
        jobs = [j.get("name") for j in (data.get("jobs") or [])]
        if jobs:
            print("OK，%d 个 job：%s" % (len(jobs), "、".join(jobs[:20])))
        else:
            print("OK，返回：%s" % json.dumps(data, ensure_ascii=False)[:200])
        waf = False
    if waf:
        # 判成 WAF 拦截时，下一个问题必然是「那加哪个 IP」。别让人再去搜一遍：
        # 容器出网走宿主机 NAT，所以这里查到的就是 Cloudflare 看到的那个源 IP。
        print("\n--- 该把哪个 IP 加进白名单 ---")
        try:
            with urllib.request.urlopen("https://api.ipify.org", timeout=10) as r:
                print("这台服务器的出口 IP：%s" % r.read().decode().strip())
        except Exception as e:
            print("查出口 IP 失败（%s）。在宿主机上跑 `curl -4 ifconfig.me` 也行。"
                  % str(getattr(e, "reason", e))[:80])
        print("把它加进 Cloudflare 里 %s 那条规则的白名单。"
              % (ADMIN_URL.split("//")[-1] or "admin-jenkins"))
        print("注意：AR 那套 (jenkins.ihhfzad.com) 放行了不代表这套也放行 —— 规则是按域名分别配的。")
    return 0


def main():
    if "--probe-admin" in sys.argv:
        sys.exit(probe_admin())
    if not JENKINS_URL:
        print("缺 JENKINS_URL。设置环境变量后重试，例如：")
        print("  export JENKINS_URL='https://jenkins.example.com'")
        sys.exit(1)
    if HOST not in ("127.0.0.1", "localhost", "::1") and ALLOW_SERVER_CREDS and JENKINS_USER:
        # 对外监听 + 用服务端凭据 = 谁访问都在用部署者的 Token，等于绕过 Jenkins 权限
        print("!! 危险配置：监听 %s 的同时启用了服务端凭据。" % HOST)
        print("!! 任何能访问该端口的人都会以 %s 的身份查询 Jenkins。" % JENKINS_USER)
        print("!! 部署到服务器请设置 JENKINS_ALLOW_SERVER_CREDS=0，让每个人填自己的 Token。")
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print("Jenkins  : %s" % JENKINS_URL)
    print("凭据模式 : %s" % ("服务端共用（仅限本机自用）" if (ALLOW_SERVER_CREDS and JENKINS_USER) else "每人填自己的 Token"))
    print("已启动   : http://%s:%d   Ctrl+C 停止" % (HOST, PORT))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
