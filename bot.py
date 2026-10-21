# -*- coding: utf-8 -*-
"""
jenkins-bot：在 Slack 里按站点发布 Jenkins 服务。

用法（频道里 @jenkins-bot）：
    @jenkins-bot 服务            后面贴发布单的「服务 → 分支」那几行（也可以整份发布单）
    @jenkins-bot AR047           出 AR047 的发布计划，下面带 [确认发布] [取消]
    @jenkins-bot AR047 中台      站点分组不明时手动指定（指定一次会记住）
    @jenkins-bot 当前            看现在记着的服务清单
    @jenkins-bot 帮助

流程（Logan 定的）：先 @服务 → 再 @站点 → 核对计划 → 点确认 → 发完回 成功/失败/发错 → 接着 @下一个站。
只有 BOT_ALLOWED_USERS 里的人点「确认发布」才有效，别人点只会收到一条仅自己可见的提示。

后台 Admin：build_admin 由他自己先打包，机器人**不执行** build_admin，只核对它最近一次成功构建的
分支是否等于发布单版本；对了才把本站的后台部署 job 一起发，不对就跳过并说明。

解析发布单、站点/组件识别、分支简写比对全部复用 app.py，两边口径一致。
跑在 Socket Mode：机器人主动连 Slack，服务器不需要开端口、不需要公网回调地址。
"""
import base64
import html
import http.cookiejar
import json
import logging
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime

import app
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jenkins-bot")

STATE_FILE = os.environ.get("BOT_STATE", "/state/state.json")
J_USER, J_TOKEN = app.JENKINS_USER, app.JENKINS_TOKEN
INDEX_TTL = 120
PARAM_NAMES = ("TAG", "BRANCH_NAME", "BRANCH")   # 同 app.read_build：各类 job 参数名不统一
# 能点「确认发布」的 Slack user id。默认空 = 谁都不能发（安全的默认值），在 /data/release-bot/.env 里配
ALLOWED = {u.strip() for u in os.environ.get("BOT_ALLOWED_USERS", "").split(",") if u.strip()}
PLAN_TTL = 15 * 60          # 计划出了 15 分钟没确认就作废：Jenkins 状态可能已经变了
STALE_SERVICES = float(os.environ.get("BOT_STALE_HOURS", "12")) * 3600   # 服务清单多久没更新算「可能过期」
POLL_EVERY = 8
RUN_TIMEOUT = 40 * 60       # 单个站点发布最长等多久，超了报「超时」而不是一直挂着

# BOT_OFFLINE=1：本地测试 handle() 时不去 Slack 校验 token
bolt = App(token=os.environ["SLACK_BOT_TOKEN"],
           token_verification_enabled=os.environ.get("BOT_OFFLINE") != "1")

# ---------------------------------------------------------------- 状态（服务清单、站点分组）
_state_lock = threading.Lock()


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"channels": {}, "site_groups": {}}


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)      # 先写临时文件再改名：写到一半崩了不会留下半截 JSON


STATE = load_state()
PLANS = {}                  # plan_id -> 计划（只在内存：机器人重启后旧按钮自然失效）
RUNNING = set()             # 正在发布的站点，防止同一站点并发两次
_plans_lock = threading.Lock()

# ---------------------------------------------------------------- Jenkins 访问
AR, ADMIN = "ar", "admin"


def _cred(which):
    if which == ADMIN:
        return app.ADMIN_URL, app.ADMIN_USER, app.ADMIN_TOKEN
    return app.JENKINS_URL, J_USER, J_TOKEN


def jget(which, path, timeout=60):
    base, u, t = _cred(which)
    return app.jenkins_get(path, u, t, timeout=timeout, base=base)


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# 报错行：dotnet 的 error CSxxxx/MSBxxxx、各种 Exception、npm ERR!、git fatal 等；warning 行一律不要
_ERR = re.compile(r"(\berror\b|Exception\b|\bFAILED\b|\bfatal:|npm ERR!|\bERROR\b|Traceback)", re.I)
_WARN = re.compile(r"\bwarning\b|\b0 Error\(s\)", re.I)     # 「0 Error(s)」是编译汇总，不是报错


def console_errors(which, job, num, max_err=8, tail=3, max_bytes=8 * 1024 * 1024):
    """
    失败原因：整份日志里挑报错行（完整显示，不截断）+ 最后几行。
    10-17 AR035 WebExtendApi：最后 20 行里 19 行是编译警告，真正的报错只有一行
    「error MSB3030 …nlog.config not found」，还被截在 200 字符看不到是哪个文件 —— 只取尾巴不够。
    """
    base, u, t = _cred(which)
    req = urllib.request.Request(base + "/job/%s/%s/consoleText" % (q(job), num))
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{u}:{t}".encode()).decode())
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) JenkinsReleaseBot/1.0")
    with urllib.request.urlopen(req, timeout=60, context=_ssl_ctx()) as r:
        raw = r.read(max_bytes)
    lines = [_ANSI.sub("", ln).rstrip() for ln in raw.decode("utf-8", "replace").splitlines()]
    lines = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("[Pipeline]")]
    errs, seen = [], set()
    for ln in lines:
        if _ERR.search(ln) and not _WARN.search(ln) and ln not in seen:
            seen.add(ln)
            errs.append(ln[:800])
    errs = errs[-max_err:]                       # 报错多时取最后几条（越靠后越接近真正失败点）
    last = [ln[:300] for ln in lines[-tail:] if ln[:800] not in errs]
    if not errs:                                  # 没认出报错行就退回看尾巴
        return [ln[:300] for ln in lines[-20:]]
    return errs + (["…"] if last else []) + last


def console_tail(which, job, num, n=20, max_bytes=8 * 1024 * 1024):
    """失败构建日志的最后 n 行。整份 consoleText 拉下来取尾巴：失败不常见，换来的是不依赖 progressiveText 的边界行为。"""
    base, u, t = _cred(which)
    req = urllib.request.Request(base + "/job/%s/%s/consoleText" % (q(job), num))
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{u}:{t}".encode()).decode())
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) JenkinsReleaseBot/1.0")
    with urllib.request.urlopen(req, timeout=60, context=_ssl_ctx()) as r:
        raw = r.read(max_bytes)
    lines = [_ANSI.sub("", ln).rstrip() for ln in raw.decode("utf-8", "replace").splitlines()]
    # Pipeline 日志结尾是一长串「[Pipeline] }」「[Pipeline] // node」收尾行，不滤掉的话
    # 最后 20 行全是它们，真正的报错反而被挤出去
    lines = [ln[:200] for ln in lines if ln.strip() and not ln.lstrip().startswith("[Pipeline]")]
    return lines[-n:]


def _ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def jpost(which, path):
    """POST 触发构建，返回 Location（队列项地址）。

    API Token 认证一般豁免 CSRF；万一这台开了强制 crumb（403 且带 X-Jenkins），
    就用同一个 cookie 会话取 crumb 再发一次 —— 新版 Jenkins 的 crumb 绑会话，不带 cookie 取了也白取。
    """
    base, u, t = _cred(which)
    auth = "Basic " + base64.b64encode(f"{u}:{t}".encode()).decode()
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) JenkinsReleaseBot/1.0"   # WAF 按 UA 拦
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar),
                                         urllib.request.HTTPSHandler(context=_ssl_ctx()))

    def _do(extra):
        req = urllib.request.Request(base + path, data=b"", method="POST")
        req.add_header("Authorization", auth)
        req.add_header("User-Agent", ua)
        for k, v in extra.items():
            req.add_header(k, v)
        with opener.open(req, timeout=60) as r:
            return r.headers.get("Location", "")

    try:
        return _do({})
    except urllib.error.HTTPError as e:
        if e.code != 403 or not e.headers.get("X-Jenkins"):
            raise
    req = urllib.request.Request(base + "/crumbIssuer/api/json")
    req.add_header("Authorization", auth)
    req.add_header("User-Agent", ua)
    with opener.open(req, timeout=30) as r:
        c = json.loads(r.read().decode())
    return _do({c["crumbRequestField"]: c["crumb"]})


def q(s):
    return urllib.parse.quote(s, safe="")


_idx = {"ts": 0, "jobs": None}
_idx_lock = threading.Lock()


def jenkins_index(force=False):
    """一次拉 AR 那套全部 job 的参数定义 + 最后一次构建的参数。返回 {job名: {...}}。"""
    with _idx_lock:
        if not force and _idx["jobs"] is not None and time.time() - _idx["ts"] < INDEX_TTL:
            return _idx["jobs"]
        tree = ("jobs[name,url,property[parameterDefinitions[name,defaultParameterValue[value]]],"
                "lastBuild[number,result,building,timestamp,actions[parameters[name,value],"
                "lastBuiltRevision[branch[name]]]]]")
        data = jget(AR, "/api/json" + app._q(tree), timeout=180)
        jobs = {}
        for j in data.get("jobs") or []:
            name = j.get("name") or ""
            site, comp = app.site_of(name), app.component_of(name)
            if not site or not comp:
                continue
            pname, default = "", ""
            for p in j.get("property") or []:
                for d in (p or {}).get("parameterDefinitions") or []:
                    if d.get("name") in PARAM_NAMES and not pname:
                        pname = d["name"]
                        default = ((d.get("defaultParameterValue") or {}).get("value")) or ""
            lb = j.get("lastBuild") or {}
            last, got = "", ""
            for a in lb.get("actions") or []:
                for p in (a or {}).get("parameters") or []:
                    if p.get("name") == pname:
                        last = p.get("value") or ""
                rev = (a or {}).get("lastBuiltRevision")
                if rev and not got:
                    got = app.normalize_ref(((rev.get("branch") or [{}])[0]).get("name") or "")
            jobs[name] = {"site": site, "comp": comp, "pname": pname, "got": got,
                          "default": default, "last": last or got, "num": lb.get("number"),
                          "result": lb.get("result"), "building": bool(lb.get("building")),
                          "ts": (lb.get("timestamp") or 0) / 1000.0}
        _idx.update(ts=time.time(), jobs=jobs)
        return jobs


def resolve_value(expect, comp, jobs):
    """
    发布单里的值 -> 真正要传给 Jenkins 的参数值。
    核对时 v3-uat 能对上 feature/v3-uat，但**触发构建必须传全名**，传简写 Jenkins 会去拉一个不存在的分支。
    拿该组件所有 job 的历史参数值和默认值做字典：
      - 有完全相同的 -> 原样
      - 只有一个「/ 尾段相同」的 -> 用它（v3-uat -> feature/v3-uat）
      - 多个 -> 拿不准，返回 None 让人确认
      - 一个都没有 -> 新 tag 之类，原样传，但标注出来
    返回 (值或None, 说明)
    """
    seen = set()
    for j in jobs.values():
        if j["comp"] == comp:
            for v in (j["last"], j["default"]):
                if v:
                    seen.add(app.normalize_ref(v))
    e = app.normalize_ref(expect)
    if e in seen:
        return e, ""
    hits = sorted(v for v in seen if v.endswith("/" + e))
    if len(hits) == 1:
        return hits[0], "简写 %s 已展开" % e
    if len(hits) > 1:
        return None, "简写 %s 对应多个分支：%s，请写全名" % (e, " / ".join(hits))
    return e, "Jenkins 历史里没出现过，按原样传（新 tag？）"


_TAG = re.compile(r"_V\d+\.\d+_\d+$")


def branch_switch(last, val):
    """上次和本次不是同一个东西，且不是 tag 升版本（master_V3.11_012 -> 015）= 换了分支，要提醒。"""
    a, b = app.normalize_ref(last), app.normalize_ref(val)
    if not a or app.ref_matches(a, b):
        return False
    return not (_TAG.search(a) and _TAG.search(b))


# build_admin 一个版本只打一次包，后面的站点直接用。
# _admin_lock 保证「查状态 + 触发」是原子的：两个站前后脚确认，只有第一个会触发，第二个看到在途就等。
# _admin_inflight 记机器人自己触发、还没出结果的那次（刚进队列时 lastBuild 还看不到它，光查 Jenkins 会重复触发）。
_admin_lock = threading.Lock()
_admin_inflight = {}        # version -> {"queue": 队列路径, "num": 构建号}


def admin_status(expect):
    """
    build_admin 相对发布单版本的状态，返回 (状态, 说明, 构建号)：
      ready  最近一次成功构建就是这个版本 —— 不用再打包
      wait   正在打这个版本（他手点的或机器人触发的）—— 等它
      build  还没打过这个版本 —— 需要执行一次
      error  读不到
    """
    exp = app.normalize_ref(expect)
    try:
        d = jget(ADMIN, "/job/%s/api/json" % q(app.ADMIN_BUILD_JOB) + app._q(
            "lastBuild[number,building,actions[parameters[name,value]]],lastSuccessfulBuild[number,timestamp,"
            "actions[parameters[name,value],lastBuiltRevision[SHA1,branch[name]]]]"))
    except Exception as e:
        return "error", "读不到 %s：%s" % (app.ADMIN_BUILD_JOB, str(getattr(e, "reason", e))[:120]), None
    lb = d.get("lastBuild") or {}
    if lb.get("building") and app.ref_matches(app.read_build(lb)["want"], exp):
        return "wait", "%s #%s 正在打包 `%s`" % (app.ADMIN_BUILD_JOB, lb.get("number"), exp), lb.get("number")
    inf = _admin_inflight.get(exp)
    if inf:
        return "wait", "%s 已排队打包 `%s`" % (app.ADMIN_BUILD_JOB, exp), inf.get("num")
    ls = d.get("lastSuccessfulBuild")
    if ls:
        b = app.read_build(ls)
        ver = b["want"] or b["got"]
        if app.ref_matches(ver, exp):
            return "ready", "%s #%s（%s）已是 `%s`，不用再打包" % (
                app.ADMIN_BUILD_JOB, b["num"], b["time"][:11], ver), b["num"]
        return "build", "%s 最新成功 #%s 是 `%s`，要先打一次 `%s`" % (
            app.ADMIN_BUILD_JOB, b["num"], ver or "?", exp), None
    return "build", "%s 还没有成功过的构建，要先打一次 `%s`" % (app.ADMIN_BUILD_JOB, exp), None


def _wait_build(which, job, queue=None, num=None, deadline=None, on_progress=None):
    """等一次构建结束。queue 和 num 至少给一个。返回 read_build 结果；超时/取消返回 None 和原因。"""
    while time.time() < deadline:
        try:
            if num is None:
                qi = jget(which, queue.rstrip("/") + "/api/json" + app._q("cancelled,executable[number,url]"))
                if qi.get("cancelled"):
                    return None, "在队列里被取消"
                if qi.get("executable"):
                    num = qi["executable"]["number"]
                    if on_progress:
                        on_progress(num)
            if num is not None:
                b = jget(which, "/job/%s/%s/api/json" % (q(job), num) + app._q(
                    "number,result,building,timestamp,duration,url,actions[parameters[name,value],"
                    "lastBuiltRevision[SHA1,branch[name]]]"))
                if not b.get("building") and b.get("result"):
                    return app.read_build(b), ""
        except urllib.error.HTTPError as e:
            if not (e.code == 404 and num is None):
                log.warning("wait %s: %s", job, e)
        except Exception as e:
            log.warning("wait %s: %s", job, e)
        time.sleep(POLL_EVERY)
    return None, "超时"


def ensure_admin_build(expect, deadline, on_progress):
    """保证 build_admin 有一次成功的 expect 版本。需要时触发一次（同版本全局只触发一次）。返回 (ok, 说明)。"""
    exp = app.normalize_ref(expect)
    job = app.ADMIN_BUILD_JOB
    with _admin_lock:
        st, note, num = admin_status(exp)
        if st == "ready":
            return True, note
        if st == "error":
            return False, note
        queue = None
        if st == "build":
            loc = jpost(ADMIN, "/job/%s/buildWithParameters?TAG=%s" % (q(job), q(exp)))
            queue = urllib.parse.urlparse(loc).path if loc else ""
            if not queue:
                return False, "触发了 %s 但 Jenkins 没返回队列地址，请去页面确认" % job
            _admin_inflight[exp] = {"queue": queue, "num": None}
            log.info("triggered %s TAG=%s -> %s", job, exp, loc)
            rlog(event="trigger", site="", job=job, param="TAG", value=exp, queue=queue)
            note = "已触发 %s 打包 `%s`" % (job, exp)
        else:
            queue = (_admin_inflight.get(exp) or {}).get("queue")
    on_progress(note)
    try:
        def prog(n):
            if exp in _admin_inflight:
                _admin_inflight[exp]["num"] = n
            on_progress("%s #%s 正在打包 `%s`" % (job, n, exp))
        b, why = _wait_build(ADMIN, job, queue=queue, num=num, deadline=deadline, on_progress=prog)
    finally:
        with _admin_lock:
            _admin_inflight.pop(exp, None)
    if not b:
        return False, "%s 打包 `%s` %s" % (job, exp, why)
    if b["result"] != "SUCCESS":
        return False, "%s #%s 打包 `%s` 失败（%s）" % (job, b["num"], exp, b["result"])
    return True, "%s #%s 打包 `%s` 成功" % (job, b["num"], exp)


def admin_site_jobs(site):
    names, _ = app.admin_deploy_jobs()
    return sorted(n for n in names if app.site_of(n) == site and n != app.ADMIN_BUILD_JOB)


# ---------------------------------------------------------------- 文本工具
HELP = """*jenkins-bot 指令大全*  （频道里 @jenkins-bot 加下面的指令；私聊机器人不用 @）

*一、每次发版的标准流程*
1. `@jenkins-bot 服务` 换行贴发布单的「服务 → 分支」几行
　 整份发布单贴进来也行，里面的站点清单、中台/非中台分组会一起记住
　 例：`Web → 分支：非中台 uat ／ 中台 feature/v3-uat`（分组用 ／ 或 、 隔开）
2. `@jenkins-bot 站点` 换行贴这次要发的站点清单（带「中台站 / 非中台站」标题）
　 → 频道里发一条 📋 *发版看板* ，之后有站点在发布时每分钟自动刷新
3. `@jenkins-bot AR047` 出这个站的发布计划（每个视图发哪个 job、传什么分支）
　 核对无误点 *确认发布* ，不对点 *取消* ；一次最多 5 个站：`@jenkins-bot AR047 AR048`
4. 发完在线程里回结论表：每个服务的 Git 实际分支、构建号、时间、代码版本
5. 接着 `@jenkins-bot 下一个站编号`

*单独发某个视图*  （临时需求，比如「ar107 帮忙部署下 Web，分支 feature/v3-uat」）
• `@jenkins-bot web ar107` 只发 AR107 的 Web-Pages，分支用当前服务清单里 Web 那行
• `@jenkins-bot web ar107 feature/v3-uat` 临时指定分支（只这一次，不改服务清单）
• `@jenkins-bot web,thirdjob ar107 ar108` 多个视图、多个站
　 视图名（和 Jenkins 一样，大小写随意）：AgentApi / Agent_Web / LotteryApi / RebateJob / ThirdApi / ThirdJob / Web-Pages / WebExtendApi / WebIntranetApi
　 也认简写：web=Web-Pages、代理后台=Agent_Web；后台那套 Jenkins 的写 admin
　 RebateJob 没有分支参数（流水线固定拉 master），指定别的分支会被拒绝

*二、查看*
• `@jenkins-bot 进度` 这次发版哪些站发完了 + 按服务核对表（要求分支、对上几个站、代码版本是否一致）
　 后面可以直接贴服务清单或站点清单，先更新再算
• `@jenkins-bot 看板` 把发版看板重新发到频道最下面
• `@jenkins-bot 当前` 看机器人现在记着的服务清单
• `@jenkins-bot 记录 AR035` 这个站机器人发过的记录（谁、什么时候、什么分支、结果；手点的不在里面）
• `@jenkins-bot 帮助` 或 `help` 看这份说明

*三、特殊情况*
• 站点分组不明时会问你：`@jenkins-bot AR047 中台` / `@jenkins-bot AR047 非中台` （记住，下次不用再说）
• 服务清单超过 12 小时没更新：计划先不给确认按钮，重发 `@jenkins-bot 服务` 后再出计划
• 站点正在发布中再 @ 它：直接回当前进度，不会重复发
• 构建失败：线程里附报错行，结论下面有 *重发失败的 N 个* / *不重发* 按钮（15 分钟内有效）

*四、规则*
• 只有授权的人点「确认发布」「重发」才有效；计划 15 分钟内有效，点过一次就失效（连点不会发两遍）
• 分支简写会展开成全名再传（`v3-uat` → `feature/v3-uat`），参数明确传入，不用 Jenkins 默认值
• 后台 Admin：build_admin 同一版本只打一次包；版本已对上就直接发本站后台，没对上先打包再发
• 「发错」= 构建成功但 Git 实际拉到的分支不对；「跳过」= 触发时那个 job 正在构建，没重复触发
• 时间按 UTC+4 显示
"""
MAX_PLAN_SITES = 5          # 一次最多出几个站的计划：防止把站点清单误当成「发这些站」刷出几十条计划


def strip_mention(text):
    """
    Slack 发过来的文本要先还原再解析：
      - 粘贴多行会被自动包成代码块，首行变成 ```V1-LotteryApi、末行值变成 feature/v3-uat```
        （10-17 实测：LotteryApi 整行没认出、Pages 期望值带了三个反引号）
      - < > & 会被转成 &lt; &gt; &amp;，「->」到这里是「-&gt;」
    """
    t = re.sub(r"<@[A-Z0-9]+>", "", text or "")
    t = html.unescape(t)
    t = t.replace("```", "\n").replace("`", "")
    return t.strip()


def fmt_ts(ts):
    return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "—"


def comp_line(c):
    view = app.COMPONENT_VIEW.get(c["name"], app.ADMIN_VIEW if c["name"] == app.ADMIN_COMP else c["name"])
    scope = ("（%s）" % c["group"]) if c["group"] else (("（仅 %s）" % c["site"]) if c["site"] else "")
    return "• *%s* %s → `%s`　_发布单写的「%s」_" % (view, scope, c["expect"], c["raw"])


# ---------------------------------------------------------------- 命令：服务 / 当前
def cmd_services(channel, text, user):
    comps, warns = app.parse_components(text)
    if not comps:
        return ("没从这段里认出任何服务。格式像这样：\n```V1-LotteryApi   →  分支：master_V3.11_015\n"
                "Web             →  分支：非中台 uat ／ 中台 v3-uat```" +
                ("\n" + "\n".join("⚠ " + w for w in warns) if warns else ""))
    groups = {k: v for k, v in app.parse_groups(text).items() if v}
    sites = app.parse_sites(text)
    with _state_lock:
        old = STATE["channels"].get(channel) or {}
        ch = {"comps": comps, "by": user, "at": time.time(), "text": text,
              # 整份发布单里带了站点就顺便记下站点清单；没带就沿用之前 @站点 记的
              "sites": sites or old.get("sites", []), "sites_text": text if sites else old.get("sites_text", "")}
        if old.get("board"):
            ch["board"] = old["board"]          # 10-21：整份替换时看板 ts 丢了，之后看板再也不更新且不报错
        STATE["channels"][channel] = ch
        STATE["site_groups"].update(groups)
        save_state(STATE)
    n_raw = len({c["raw"] for c in comps})
    split = [c["raw"] for c in comps if c["group"] or c["site"]]
    out = ["已记下 %d 个服务%s（之后 @我 站点编号就按这份出计划）：" % (
        n_raw, ("，其中 %s 按分组拆成 %d 条" % ("、".join(sorted(set(split))), len(split))) if split else "")]
    out += [comp_line(c) for c in comps]
    if groups:
        out.append("站点分组也记住了：" + "、".join("%s=%s" % kv for kv in sorted(groups.items())))
    if sites:
        board_async(channel, create=True)
        out.append("站点清单也记下了：%d 个站。📋 发版看板已发在频道里，有站点在发布时每分钟自动刷新" % len(sites))
    out += ["⚠ " + w for w in warns]
    return "\n".join(out)


def cmd_sites(channel, text, user):
    sites = app.parse_sites(text)
    if not sites:
        return "没认出站点编号。格式像发布单里那样：`（印度）AR001→SiteA`，一行一个。"
    groups = {k: v for k, v in app.parse_groups(text).items() if v}
    with _state_lock:
        ch = STATE["channels"].setdefault(channel, {"comps": [], "by": user, "at": time.time(), "text": ""})
        ch.update(sites=sites, sites_text=text)
        STATE["site_groups"].update(groups)
        save_state(STATE)
    gs = {}
    for s in sites:
        gs.setdefault(groups.get(s) or "未分组", []).append(s)
    board_async(channel, create=True)          # 新一轮发版：频道里发一条看板，之后有站点在发布时每分钟自动刷新
    return ("已记下这次发版的 %d 个站点（%s）。\n📋 发版看板已发在频道里，有站点在发布时每分钟自动刷新；"
            "也可以随时 `@jenkins-bot 进度`。" % (len(sites), "、".join("%s %d" % (g, len(v)) for g, v in gs.items())))


# ---------------------------------------------------------------- 发版看板：频道里一条消息，发布中每分钟自动刷新
_board_lock = threading.Lock()
_board_busy = set()                 # 正在刷新的频道：上一次还没算完就跳过，不叠着打 Jenkins
BOARD_EVERY = int(os.environ.get("BOT_BOARD_EVERY", "60"))


def refresh_board(channel, create=False, force=True):
    """内容就是「进度」。create=True 发一条新的（新一轮发版 / 他要求重发看板），否则原地更新那一条。
    force=False 走 run_check 的缓存（有效期约 1 分钟），给定时刷新用，免得每分钟全量打一次 Jenkins。"""
    ch = STATE["channels"].get(channel)
    if not ch or not ch.get("sites") or not ch.get("comps"):
        return
    if not create and not ch.get("board"):
        return                                   # 没开过看板就不自动开，免得频道里冒出意料外的消息
    with _board_lock:
        if channel in _board_busy:
            return
        _board_busy.add(channel)
    try:
        try:
            body = cmd_progress(channel, force=force)
        except Exception as e:
            log.exception("board progress failed")
            body = "看板刷新失败：%s: %s" % (type(e).__name__, str(e)[:200])
        text = "📋 *发版看板*  有站点在发布时每 %d 秒自动刷新 · 最后更新 %s\n\n%s" % (
            BOARD_EVERY, time.strftime("%m-%d %H:%M:%S"), body)
        try:
            if create or not (STATE["channels"].get(channel) or {}).get("board"):
                r = bolt.client.chat_postMessage(channel=channel, text=text, blocks=_sections(text))
                with _state_lock:
                    # 写进频道「当前」的记录：算看板要几秒，这期间清单可能被整份替换，写旧对象等于丢了
                    STATE["channels"].setdefault(channel, {})["board"] = {"ts": r["ts"], "at": time.time()}
                    save_state(STATE)
            else:
                ts = (STATE["channels"].get(channel) or {}).get("board", {}).get("ts")
                bolt.client.chat_update(channel=channel, ts=ts, text=text, blocks=_sections(text))
        except Exception:
            log.exception("board post/update failed")
    finally:
        with _board_lock:
            _board_busy.discard(channel)


def board_async(channel, create=False, force=True):
    threading.Thread(target=refresh_board, args=(channel, create, force), daemon=True).start()


def board_ticker():
    """10-21 他嫌看板慢：原来只在一个站整个发完（5~6 分钟）才更新。
    现在只要有站点在发布，就每 BOARD_EVERY 秒刷新一次；没有在发的就不刷，不白打 Jenkins。"""
    while True:
        time.sleep(BOARD_EVERY)
        try:
            if not RUNNING:
                continue
            for cid, ch in list(STATE["channels"].items()):
                if ch.get("board"):
                    refresh_board(cid, force=False)
        except Exception:
            log.exception("board ticker")


# ---------------------------------------------------------------- 发布记录：每次确认/触发/结果都追加一行，永久保留
RELEASE_LOG = os.path.join(os.path.dirname(STATE_FILE), "releases.jsonl")
_rlog_lock = threading.Lock()


def rlog(**kw):
    """容器日志满 10MB 就轮转掉了；追查「这个站上次是谁、什么时候、用什么参数发的」要靠这个文件。"""
    kw = dict(at=time.strftime("%Y-%m-%d %H:%M:%S"), **kw)
    try:
        with _rlog_lock, open(RELEASE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(kw, ensure_ascii=False) + "\n")
    except Exception:
        log.exception("release log write failed")


def cmd_history(text):
    sites = app.parse_sites(text)
    if not sites:
        return "格式：`@jenkins-bot 记录 AR035`（看这个站最近的发布记录）"
    try:
        with open(RELEASE_LOG, encoding="utf-8") as f:
            rows = [json.loads(ln) for ln in f if ln.strip()]
    except FileNotFoundError:
        rows = []
    out = []
    for s in sites:
        rs = [r for r in rows if r.get("site") == s and r.get("event") in ("confirm", "result")][-25:]
        if not rs:
            out.append("*%s* ：机器人还没发过这个站（记录从 10-17 开始）" % s)
            continue
        out.append("*%s 最近的发布记录*  （机器人发的；手点的不在这里）" % s)
        lines = []
        for r in rs:
            if r["event"] == "confirm":
                lines.append("%s  %s确认 %d 个 job" % (r["at"][5:16], "重发 " if r.get("retry") else "", len(r.get("jobs", []))))
            else:
                lines.append("%s    %s %-16s %-18s #%s %s %s" % (
                    r["at"][5:16], {"ok": "✅", "bad": "❌", "wrong": "⚠", "skip": "⏭"}.get(r.get("verdict"), "·"),
                    r.get("view", ""), r.get("branch") or "—", r.get("num") or "—", r.get("result") or "", r.get("sha") or ""))
        out.append("```" + "\n".join(lines) + "```")
    return "\n".join(out)


def comps_to_text(comps):
    """已识别的服务清单 -> 发布单格式的服务行（parse_components 能原样认回来）。
    用发布单原写法 raw 而不是组件名：组件 Web(代理后台) 的名字按别名会被认成 Pages。"""
    by = {}
    for c in comps:
        by.setdefault(c["raw"], []).append(c)
    out = []
    for raw, cs in by.items():
        if len(cs) == 1 and not cs[0]["group"] and not cs[0]["site"]:
            out.append("%s → 分支：%s" % (raw, cs[0]["expect"]))
        else:
            out.append("%s → 分支：%s" % (raw, " ／ ".join(
                "%s %s" % (c["group"] or c["site"].lower(), c["expect"]) if (c["group"] or c["site"]) else c["expect"]
                for c in cs)))
    return "\n".join(out)


def _pad(s, n):
    """按显示宽度补空格：等宽代码块里中文占 2 格，用 %-17s 会把「Web-Pages 非中台」那行挤歪。"""
    w = sum(2 if ord(ch) > 0x2E80 else 1 for ch in s)
    return s + " " * max(1, n - w)


def service_table(r):
    """
    按服务（中台/非中台分开）汇总 run_check 的结果：要求分支、对上几个站、几个 job、实际 SHA。
    同一服务出现多个 SHA = 浮动分支在发版期间被推了新提交，先发的站代码是旧的 —— 单看分支名发现不了，
    这里把少数派的站点点名出来。tag（master_V3.11_015）理论上只会有一个 SHA。
    """
    from collections import Counter
    order, svc = [], {}
    for x in r["rows"]:
        if x["state"] == "NOJOB" or not re.match(r"^AR\d+$", x["site"]):
            continue
        k = x["compKey"]
        if k not in svc:
            order.append(k)
            name = "Admin" if x["comp"] == app.ADMIN_COMP else (x.get("view") or x["comp"])
            svc[k] = {"name": name + ((" " + x["group"]) if x.get("group") else ""), "expect": x["expect"],
                      "jobs": 0, "okjobs": 0, "sites": {}, "shas": Counter(), "shasites": {}, "actual": Counter()}
        s = svc[k]
        s["jobs"] += 1
        ok = x["state"] == "OK"
        s["sites"][x["site"]] = s["sites"].get(x["site"], True) and ok
        if x["state"] in ("VER", "FAIL", "NOVER"):
            s["bad"] = s.get("bad", 0) + 1     # 真有问题才标 ❌；还没发到的只是 ⏳
        if ok:
            s["okjobs"] += 1
            if x.get("actual"):
                s["actual"][app.normalize_ref(x["actual"])] += 1
            if x.get("sha"):
                s["shas"][x["sha"]] += 1
                s["shasites"].setdefault(x["sha"], set()).add(x["site"])
                s.setdefault("shats", {})[x["sha"]] = max(s.get("shats", {}).get(x["sha"], 0), x.get("ts") or 0)
    lines, warns = [], []
    for k in order:
        s = svc[k]
        n_site, ok_site = len(s["sites"]), sum(1 for v in s["sites"].values() if v)
        br = s["actual"].most_common(1)[0][0] if s["actual"] else s["expect"]
        if not s["shas"]:
            ver = "—"
        elif len(s["shas"]) == 1:
            ver = next(iter(s["shas"]))
        else:
            ver = "%d 个版本" % len(s["shas"])
        ico = "✅" if ok_site == n_site else ("❌" if s.get("bad") else "⏳")
        lines.append("%s %s %-18s %3d/%-3d站 %3d job  %s" % (ico, _pad(s["name"], 17), br, ok_site, n_site, s["jobs"], ver))
        if len(s["shas"]) > 1:
            # 以「最后一次构建拿到的」为最新（同网页版「代码落后」口径），不是看哪个 SHA 站多：
            # 发版后半段分支被推了新提交时，最新的反而是少数派
            newest = max(s["shats"], key=lambda h: s["shats"][h])
            old = ["`%s` %s" % (h, " ".join(sorted(s["shasites"][h]))) for h in s["shas"] if h != newest]
            warns.append("⚠ *%s* 代码版本不一致：最新是 `%s`（%d 站）；落后的：%s —— 分支名一样但代码旧，"
                         "构建那一刻分支上还没有后来的提交。要不要重发落后的站你定" % (
                             s["name"], newest, len(s["shasites"][newest]), "；".join(old)))
    return lines, warns


def cmd_progress(channel, force=True):
    """
    这次发版的站点进度：哪些站全部发完、哪些有问题、哪些发了一部分、哪些还没开始。
    直接用网页版的 run_check（同一套解析、同一套判定），时间窗从记下服务清单那天到今天。
    """
    ch = STATE["channels"].get(channel)
    if not ch or not ch.get("comps"):
        return "这个频道还没记服务清单。先 `@jenkins-bot 服务` 贴发布单。"
    if not ch.get("sites"):
        return ("我不知道这次要发哪些站。发 `@jenkins-bot 站点` 换行贴站点清单（或者把整份发布单贴进 `@jenkins-bot 服务`），"
                "我才能算出还有哪些没发。")
    # 不用存下来的原文，用已识别的服务清单重拼：10-17 早上记的清单是旧版存的、没有原文，
    # 「进度」直接报「没解析出组件行」。站点清单在前：分组标题要先于站点行出现
    text = (ch.get("sites_text") or "") + "\n" + comps_to_text(ch["comps"])
    d0 = datetime.fromtimestamp(ch["at"]).date()
    days = (datetime.now().date() - d0).days + 1
    r = app.run_check(text, d0.strftime("%Y-%m-%d"), days, ["AR000"], False, force, J_USER, J_TOKEN)
    if not r.get("ok"):
        return "算进度失败：" + "；".join(r.get("warnings") or ["没解析出内容"])
    by = {}
    for x in r["rows"]:
        if x["state"] == "NOJOB" or not re.match(r"^AR\d+$", x["site"]):
            continue                                     # 「后台构建」那种汇总行不算站点
        s = by.setdefault(x["site"], {"name": x.get("siteName") or "", "n": 0, "ok": 0, "run": 0,
                                       "miss": [], "bad": [], "last": 0})
        view = "Admin" if x["comp"] == app.ADMIN_COMP else (x.get("view") or x["comp"])
        s["n"] += 1
        if x["state"] == "OK":
            s["ok"] += 1
            s["last"] = max(s["last"], x.get("ts") or 0)
        elif x["state"] == "MISS":
            s["miss"].append(view)
        elif x["state"] == "RUN":
            s["run"] += 1
            s["miss"].append(view + " 构建中")
        else:
            s["bad"].append("%s %s" % (view, {"VER": "版本不符", "FAIL": "构建失败", "NOVER": "版本未知"}.get(
                x["state"], x["state"])))
    S = sorted(by.items())
    svc_lines, sha_warn = service_table(r)
    done = sorted([kv for kv in S if kv[1]["ok"] == kv[1]["n"]], key=lambda kv: -kv[1]["last"])
    bad = [kv for kv in S if kv[1]["bad"]]
    part = [kv for kv in S if not kv[1]["bad"] and kv[1]["ok"] < kv[1]["n"] and (kv[1]["ok"] or kv[1]["run"])]
    none = [kv for kv in S if not kv[1]["bad"] and not kv[1]["ok"] and not kv[1]["run"]]
    nm = lambda k, s: "%s %s" % (k, s["name"]) if s["name"] else k
    out = ["*发版进度：全部发完 %d / %d 站*  （时间窗 %s 起 %d 天）" % (len(done), len(S), d0.strftime("%m-%d"), days),
           "核对口径：每个站的每个服务，看时间窗内最近一次构建 —— 构建成功，且 *Git 实际拉到的分支* = 发布单要求的"
           "（中台/非中台按站点清单分组分别对）。一个站所有服务都对上才算「全部发完」。",
           "", "*按服务核对*  （要求分支 · 对上的站/该服务总站数 · job 数 · 代码版本）",
           "```" + "\n".join(svc_lines) + "```"]
    out += sha_warn
    if done:                                  # 他要的：全部发完的放最前，最近发完的在前
        out.append("\n✅ *全部发完（%d）*" % len(done))
        out.append("　" + "　".join("%s(%s)" % (k, fmt_ts(s["last"])[6:]) for k, s in done))
    if bad:
        out.append("\n⚠ *有问题（%d）*" % len(bad))
        out += ["• %s：%s" % (nm(k, s), "、".join(s["bad"])) for k, s in bad]
    if part:
        out.append("\n⏳ *发了一部分（%d）*" % len(part))
        out += ["• %s %d/%d，缺 %s" % (nm(k, s), s["ok"], s["n"], "、".join(s["miss"])) for k, s in part]
    if none:
        out.append("\n○ *还没开始（%d）*" % len(none))
        out.append("　" + "　".join(k for k, _ in none))
    miss_sites = [s for s in ch["sites"] if s not in by]
    if miss_sites:
        out.append("\n❓ 清单里有、但 Jenkins 上没有这些服务的 job：" + "　".join(miss_sites))
    out += ["⚠ " + w.replace("**", "") for w in (r.get("warnings") or [])]   # 网页版的 **加粗** 在 Slack 显示成星号
    return "\n".join(out)


def cmd_current(channel):
    ch = STATE["channels"].get(channel)
    if not ch:
        return "这个频道还没记服务清单。先 `@jenkins-bot 服务` 贴发布单。"
    return ("当前服务清单（%s 设置）：\n" % fmt_ts(ch["at"]) +
            "\n".join(comp_line(c) for c in ch["comps"]))


# ---------------------------------------------------------------- 计划
def build_plan(site, comps, group_hint, jobs):
    """返回 dict：head / lines（展示）/ targets（确认后要触发的）/ extra（提示）。"""
    site_jobs = {n: j for n, j in jobs.items() if j["site"] == site}
    if not site_jobs:
        return {"site": site, "head": "*%s*  Jenkins 上没有这个站点的任何 job，编号写错了？" % site,
                "lines": [], "targets": [], "extra": []}
    disp = ""
    for n in sorted(site_jobs):
        if site_jobs[n]["comp"] == "Pages":
            disp = n.split("-")[-1]
            break
    disp = disp or sorted(site_jobs)[0].split("-")[-1]

    need_group = any(c["group"] for c in comps)
    group = group_hint or STATE["site_groups"].get(site, "")
    gsrc = "本次指定" if group_hint else ("记住的" if group else "")

    lines, targets, extra = [], [], []
    for c in comps:
        view = app.COMPONENT_VIEW.get(c["name"], c["name"])
        if c["site"] and c["site"] != site:
            continue                                    # 站点级特例，不是这个站
        if c["group"] and (not group or c["group"] != group):
            continue                                    # 分组未知的在下面统一提示
        if c["name"] == app.ADMIN_COMP:
            st, note, _ = admin_status(c["expect"])
            ajobs = admin_site_jobs(site) if st != "error" else []
            if st == "error":
                lines.append("• *后台 Admin* → ❌ %s，不发" % note)
            elif not ajobs:
                lines.append("• *后台 Admin* → ❓ 后台 Jenkins 的 %s 视图里没有 %s 的部署 job"
                             % (app.ADMIN_DEPLOY_VIEW, site))
            step = {"ready": "✓ " + note,
                    "wait": "⏳ %s，打完再发本站后台" % note,
                    "build": "① %s（只打这一次，后面的站直接用）② 成功后再发本站后台" % note}.get(st, "")
            for n in ajobs:
                lines.append("• *后台 Admin* `%s`　不传参数\n　　%s" % (n, step))
                targets.append({"which": ADMIN, "view": "后台 Admin", "job": n, "pname": "", "value": "",
                                "admin_ver": app.normalize_ref(c["expect"])})
            continue
        # 同一站点同组件可能有多个 job（AR013 的 Pages 有两个），全部列出，一个都不能漏
        names = sorted(n for n, j in site_jobs.items() if j["comp"] == c["name"])
        if not names:
            lines.append("• *%s* → ❓ 本站没有 %s 的 job（这个站不发这个服务？）" % (view, c["name"]))
            continue
        val, note = resolve_value(c["expect"], c["name"], jobs)
        for n in names:
            j = site_jobs[n]
            if not j["pname"]:
                # 没有分支参数的（RebateJob 全是：分支写死在流水线里）：要求的分支和它实际拉的一致才发，
                # 不传参数直接 build；发完照样按 Git 实际分支核对。不一致就不发——传不进去，硬发等于发错
                fixed = j.get("got")
                want = val if val is not None else app.normalize_ref(c["expect"])
                if fixed and app.ref_matches(fixed, want):
                    lines.append("• *%s* `%s`　不传参数（流水线固定拉 `%s`）" % (view, n, fixed))
                    targets.append({"which": AR, "view": view, "job": n, "pname": "", "value": fixed})
                elif fixed:
                    lines.append("• *%s* `%s` → ❌ 没有分支参数，流水线固定拉 `%s`，发不了 `%s`；要改得去改 job 配置"
                                 % (view, n, fixed, want))
                else:
                    lines.append("• *%s* `%s` → ❓ 没有分支参数，也读不到它上次拉的分支，不发" % (view, n))
                continue
            if val is None:
                lines.append("• *%s* `%s` → ❌ %s，不发" % (view, n, note))
                continue
            flags = [note] if note else []
            if branch_switch(j["last"], val):
                flags.append("↔ *换分支*  上次 `%s`，本次 `%s`" % (app.normalize_ref(j["last"]), val))
            elif j["last"] and app.ref_matches(j["last"], val):
                flags.append("上次 #%s（%s）发的也是它" % (j["num"], fmt_ts(j["ts"])))
            if j["building"]:
                flags.append("⏳ 正在构建 #%s，确认时会跳过" % j["num"])
            lines.append("• *%s* `%s`　%s=`%s`%s" % (
                view, n, j["pname"], val, ("\n　　" + "；".join(flags)) if flags else ""))
            targets.append({"which": AR, "view": view, "job": n, "pname": j["pname"], "value": val})
    head = "*%s %s*" % (site, disp)
    if need_group:
        head += "（%s%s）" % (group or "分组未知", "，" + gsrc if gsrc else "")
    head += " — 共 %d 个 job" % len(targets)
    if need_group and not group:
        pages = [j for j in site_jobs.values() if j["comp"] == "Pages" and j["last"]]
        hint = ("上次 Pages 发的是 `%s`" % app.normalize_ref(pages[0]["last"])) if pages else ""
        extra.append("❓ 有服务按中台/非中台分别发，但我不知道 %s 属于哪组%s。回复 `@jenkins-bot %s 中台` 或 `%s 非中台`"
                     % (site, "（" + hint + "，仅供参考，不拿它推断）" if hint else "", site, site))
    return {"site": site, "disp": disp, "head": head, "lines": lines, "targets": targets, "extra": extra}


def plan_text(p):
    return "\n".join([p["head"]] + p["lines"] + p["extra"])


def _sections(text):
    """Slack section 文本上限 3000 字符，长计划按行切块。"""
    blocks, buf = [], ""
    for ln in text.split("\n"):
        if len(buf) + len(ln) + 1 > 2900:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": buf}})
            buf = ""
        buf += ln + "\n"
    if buf.strip():
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": buf}})
    return blocks


def plan_blocks(p, pid):
    blocks = _sections(plan_text(p))
    if p["targets"] and not p["extra"]:
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "action_id": "confirm", "style": "primary", "value": pid,
             "text": {"type": "plain_text", "text": "确认发布 %d 个" % len(p["targets"])},
             "confirm": {"title": {"type": "plain_text", "text": "确认发布"},
                         "text": {"type": "mrkdwn", "text": "触发 *%s* 的 %d 个 job？" % (p["site"], len(p["targets"]))},
                         "confirm": {"type": "plain_text", "text": "发布"},
                         "deny": {"type": "plain_text", "text": "再看看"}}},
            {"type": "button", "action_id": "cancel", "value": pid,
             "text": {"type": "plain_text", "text": "取消"}}]})
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                       "text": "计划 15 分钟内有效；只有授权的人点确认才会发布。"}]})
    elif p["targets"]:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                       "text": "先处理上面的 ❓/⚠，我再给你确认按钮。"}]})
    return blocks


def _ago(ts):
    s = time.time() - ts
    return "%d 天" % (s // 86400) if s >= 86400 else "%d 小时" % (s // 3600)


def running_status(site):
    """正在发布的站被再次 @ 时：直接给它现在的进度（来自持久化的 running 记录，每次状态变化都会更新）。"""
    r = (STATE.get("running") or {}).get(site)
    if not r:
        return "%s 正在发布中，结束后会在原来的线程里回结论。" % site
    done = sum(1 for t in r["targets"] if t.get("state") not in ("queued", "running", "wait_admin", "triggering"))
    return "\n".join(["*%s %s* 正在发布中（%s 确认，已结束 %d/%d），当前进度：" % (
        site, r.get("disp", ""), fmt_ts(r["started"]), done, len(r["targets"]))] +
        [_line(t) for t in r["targets"]] + ["_结束后会在原来的线程里回结论，不用再 @。_"])


def parse_view_cmd(t):
    """
    「按视图发」：`web ar107` / `web ar107 feature/v3-uat` / `web,thirdjob ar107 ar108`。
    返回 (视图[(组件名, 写法)], 站点, 分支或None)；不是这种写法返回 None；视图名不在这套 Jenkins 返回错误文本。
    只认精确别名（不做近似匹配）：认错一个视图就是发错服务。
    """
    names, branch, sites = [], None, app.parse_sites(t)
    if not sites:
        return None
    for tok in [x for x in re.split(r"[\s,，、；;]+", t) if x]:
        if app.parse_sites(tok) or tok in ("中台", "非中台"):
            continue
        name, not_here, _ = app.resolve_component(tok)
        if not_here:
            return "「%s」在后台那套 Jenkins，但这里没配后台 Jenkins，发不了" % tok
        if name:
            if name not in [n for n, _ in names]:
                names.append((name, tok))
        elif branch is None and re.search(r"[A-Za-z0-9]", tok):
            branch = tok
        else:
            return None                       # 认不出的词：不当成按视图发，交给原来的逻辑
    return (names, sites, branch) if names else None


def _view_name(n):
    return "Admin" if n == app.ADMIN_COMP else app.COMPONENT_VIEW.get(n, n)


def cmd_plan(channel, text, thread, say, view=None):
    """view = parse_view_cmd 的结果时：只发指定视图；带分支就临时用这个分支（不改服务清单、不受过期限制）。"""
    ch = STATE["channels"].get(channel)
    names, vbranch = (view[0], view[2]) if view else (None, None)
    if not ch and not vbranch:
        say(text="这个频道还没记服务清单。先 `@jenkins-bot 服务` 换行贴发布单；"
                 "或者临时指定分支：`@jenkins-bot web ar107 feature/v3-uat`", thread_ts=thread)
        return
    if vbranch:
        comps = [{"key": "%s@@" % n, "name": n, "raw": raw, "expect": vbranch, "group": "", "site": ""}
                 for n, raw in names]
    elif names:
        comps = [c for c in ch["comps"] if c["name"] in [n for n, _ in names]]
        missing = [raw for n, raw in names if n not in [c["name"] for c in comps]]
        if missing:
            say(text="当前服务清单里没有 %s，不知道发哪个分支。在后面写上分支：`@jenkins-bot %s %s 分支名`"
                     % ("、".join(missing), " ".join(r for _, r in names), " ".join(app.parse_sites(text))),
                thread_ts=thread)
            return
    else:
        comps = ch["comps"]
    sites = app.parse_sites(text)
    g = "非中台" if re.search(r"非中台", text) else ("中台" if re.search(r"中台", text) else "")
    if g:
        with _state_lock:
            for s in sites:
                STATE["site_groups"][s] = g
            save_state(STATE)
    # 服务清单一直用最后一次发的，不会自己过期 —— 下次发版忘了重发，就会按上一版的分支发，
    # 而且一切看起来都正常。超过 STALE_SERVICES 没更新就先不给确认按钮，必须重发清单
    # （10-17 他把「沿用」去掉了：每次发版都重发清单）。
    stale = not vbranch and time.time() - ch.get("at", 0) > STALE_SERVICES
    jobs = None
    for s in sites:                           # 一个站一条消息、一组按钮，各确认各的
        if s in RUNNING:                      # 正在发的站：直接给当前进度，不出新计划
            say(text=running_status(s), thread_ts=thread)
            continue
        if jobs is None:
            jobs = jenkins_index(force=True)  # 出计划用最新状态：要据此判断「正在构建」
        p = build_plan(s, comps, g, jobs)
        if vbranch:
            p["head"] += "\n_只发：%s · 临时指定分支 `%s`（不改服务清单）_" % (
                "、".join(_view_name(n) for n, _ in names), vbranch)
        elif names:
            p["head"] += "\n_只发：%s · 分支取自服务清单（%s 设置）_" % (
                "、".join(_view_name(n) for n, _ in names), fmt_ts(ch["at"]))
        else:
            p["head"] += "\n_服务清单：%s 设置_" % fmt_ts(ch["at"])
        if stale:
            p["extra"].append("⚠ *服务清单是 %s 设置的（%s前）*  不确定还是不是这次发版的分支。"
                              "请重发 `@jenkins-bot 服务` + 发布单，再 @我 站点编号，我再给确认按钮。"
                              % (fmt_ts(ch["at"]), _ago(ch["at"])))
        pid = uuid.uuid4().hex[:12]
        p.update(at=time.time(), channel=channel, thread=thread)
        with _plans_lock:
            PLANS[pid] = p
        r = say(text=plan_text(p), blocks=plan_blocks(p, pid), thread_ts=thread)
        p["ts"] = r.get("ts") if r else None


# ---------------------------------------------------------------- 发布
def _verdict(t):
    """单个 job 的最终结论：(图标, 文字, 类别 ok/bad/wrong/skip)"""
    st = t.get("state")
    if st == "skip":
        return "⏭", "跳过：" + t.get("why", ""), "skip"
    if st == "trigger_err":
        return "❌", "触发失败：" + t.get("why", ""), "bad"
    if st == "admin_err":
        return "❌", "没发：" + t.get("why", ""), "bad"
    if st == "cancelled":
        return "❌", "在队列里被取消", "bad"
    if st == "timeout":
        return "⏱", "超时仍未结束（#%s）" % t.get("num", "?"), "bad"
    b = t.get("build") or {}
    res = b.get("result") or ""
    if res != "SUCCESS":
        return "❌", "构建%s" % {"FAILURE": "失败", "ABORTED": "被中止", "UNSTABLE": "不稳定"}.get(res, res or "失败"), "bad"
    if t["value"]:
        # 比 Git 实际拉到的（got），不比参数（want）—— 参数是我们自己传的，比它永远相等，等于没核对
        got, want = b.get("got"), b.get("want")
        if got and not app.ref_matches(got, t["value"]):
            return "⚠", "发错：应为 `%s`，Git 实际拉到 `%s`" % (t["value"], got), "wrong"
        if not got and want and not app.ref_matches(want, t["value"]):
            return "⚠", "发错：实际参数 `%s`，应为 `%s`" % (want, t["value"]), "wrong"
    if t.get("admin_note"):
        return "✅", "成功（%s）" % t["admin_note"], "ok"
    return "✅", "成功 %s" % (b.get("sha") or ""), "ok"


def _line(t):
    link = t.get("url")
    num = ("<%s|#%s>" % (link, t["num"])) if link and t.get("num") else ("#%s" % t["num"] if t.get("num") else "")
    st = t.get("state")
    if st == "wait_admin":
        return "⏸ *%s* `%s` 等打包：%s" % (t["view"], t["job"], t.get("why") or "检查 build_admin…")
    if st in ("queued", "triggering"):
        return "⏸ *%s* `%s` 排队中…" % (t["view"], t["job"])
    if st == "running":
        return "⏳ *%s* `%s` %s 构建中…" % (t["view"], t["job"], num)
    ico, txt, _ = _verdict(t)
    dur = (" %ss" % t["build"]["dur"]) if t.get("build") and t["build"].get("dur") else ""
    return "%s *%s* `%s` %s %s%s" % (ico, t["view"], t["job"], num, txt, dur)


def _persist(p, user, started, targets):
    """正在发布的站点写进状态文件：①部署前据此判断能不能重启 ②重启后据此接着跟踪。"""
    with _state_lock:
        STATE.setdefault("running", {})[p["site"]] = {
            "site": p["site"], "disp": p.get("disp", ""), "channel": p["channel"], "ts": p["ts"],
            "thread": p.get("thread"), "user": user, "started": started, "targets": targets}
        save_state(STATE)


def _unpersist(site):
    with _state_lock:
        if STATE.get("running", {}).pop(site, None) is not None:
            save_state(STATE)


def _build_after(t, since):
    """这个 job 在 since 之后开始的最近一次构建号（重启后找回「已触发但没记下构建号」的那次）。"""
    try:
        lb = jget(t["which"], "/job/%s/api/json" % q(t["job"]) + app._q("lastBuild[number,timestamp,url]"))
        b = lb.get("lastBuild") or {}
        if (b.get("timestamp") or 0) / 1000.0 >= since - 5:
            return b.get("number"), b.get("url", "")
    except Exception as e:
        log.warning("build_after %s: %s", t["job"], e)
    return None, ""


def _end_ts(t):
    b = t.get("build") or {}
    return (b.get("ts") or 0) + (b.get("dur") or 0)


def _retryable(t):
    """哪些值得一键重发：构建失败/被中止、触发失败、在队列被取消、后台没发成。
    不含：发错（重发大概率还是错，要先查原因）、超时（可能还在跑，重发会叠一次）、跳过（本来就在构建）。"""
    st = t.get("state")
    if st in ("trigger_err", "cancelled", "admin_err"):
        return True
    return st == "done" and (t.get("build") or {}).get("result") != "SUCCESS"


def retry_blocks(rp, pid):
    names = "、".join(t["view"] for t in rp["targets"])
    return [{"type": "actions", "elements": [
                {"type": "button", "action_id": "confirm", "style": "primary", "value": pid,
                 "text": {"type": "plain_text", "text": "重发失败的 %d 个" % len(rp["targets"])},
                 "confirm": {"title": {"type": "plain_text", "text": "重发"},
                             "text": {"type": "mrkdwn", "text": "重新触发 *%s* 的：%s？" % (rp["site"], names)},
                             "confirm": {"type": "plain_text", "text": "重发"},
                             "deny": {"type": "plain_text", "text": "先不"}}},
                {"type": "button", "action_id": "cancel", "value": pid,
                 "text": {"type": "plain_text", "text": "不重发"}}]},
            {"type": "context", "elements": [{"type": "mrkdwn",
             "text": "参数和刚才一样；15 分钟内有效。先看上面的日志确认原因再点。"}]}]


def summary_text(site, disp, targets, user, started):
    """发完后的结论：像 release 机器人那样一张表列出本次每个服务的实际分支，问题项再单独列链接。"""
    cats = {}
    for t in targets:
        cats.setdefault(_verdict(t)[2], []).append(t)
    n_ok, n_bad, n_wrong, n_skip = (len(cats.get(k, [])) for k in ("ok", "bad", "wrong", "skip"))
    if n_ok == len(targets):
        # 注意：Slack 粗体的右 * 后面必须是空格/行尾，紧跟中文标点（，：（）就不认，会原样显示星号
        title = "✅ *%s %s 全部发布成功 %d/%d*  分支已逐个核对" % (site, disp, n_ok, len(targets))
    else:
        title = "*%s %s 发布结果*  ✅ 成功 %d　❌ 失败 %d　⚠ 发错 %d　⏭ 跳过 %d" % (
            site, disp, n_ok, n_bad, n_wrong, n_skip)
    rows = []
    for t in targets:
        ico, _, _ = _verdict(t)
        b = t.get("build") or {}
        # 表里写 Git 实际拉到的分支（核对用的就是它）；后台部署 job 自己不拉代码，写它装的包版本
        br = t.get("admin_ver") or b.get("got") or b.get("want") or t.get("value") or "—"
        name = "Admin" if t["which"] == ADMIN else t["view"]
        if len([x for x in targets if x["view"] == t["view"]]) > 1:   # 一站多 job（AR013 的两个 Pages）
            name += "(" + t["job"].split("-")[-1] + ")"
        rows.append("%s %-16s %-20s %-6s %-12s %s" % (
            ico, name, br, ("#%s" % t["num"]) if t.get("num") else "—",
            fmt_ts(_end_ts(t)) if t.get("build") else "—", b.get("sha") or ""))
    lines = [title, "```" + "\n".join(rows) + "```"]
    for k in ("bad", "wrong", "skip"):
        lines += [_line(t) for t in cats.get(k, [])]
    lines.append("用时 %d 分钟（%s 起）。可以接着 @我 下一个站。" % (
(time.time() - started) // 60, fmt_ts(started)))
    return "\n".join(lines)


def run_release(p, user, client, resume=None):
    """resume 不为空 = 机器人重启后接着跟踪（resume 是持久化的那份记录）。"""
    site, channel = p["site"], p["channel"]
    if resume:
        targets, started = resume["targets"], resume["started"]
    else:
        targets, started = [dict(t) for t in p["targets"]], time.time()

    def render(final=False):
        head = "*%s %s* %s（%s 确认）" % (site, p.get("disp", ""), "发布结束" if final else "发布中",
                                       fmt_ts(started))
        body = "\n".join([head] + [_line(t) for t in targets])
        if not final:
            _persist(p, user, started, targets)
        try:
            client.chat_update(channel=channel, ts=p["ts"], text=body, blocks=_sections(body))
        except Exception:
            log.exception("chat_update failed")

    def trigger(t):
        path = "/job/%s/" % q(t["job"])
        path += ("buildWithParameters?%s=%s" % (t["pname"], q(t["value"]))) if t["pname"] else "build"
        try:
            loc = jpost(t["which"], path)
            t.update(state="queued", queue=urllib.parse.urlparse(loc).path if loc else "")
            log.info("triggered %s %s=%s -> %s", t["job"], t["pname"], t["value"], loc)
        except urllib.error.HTTPError as e:
            t.update(state="trigger_err", why=app.friendly_http(e, t["job"]))
        except Exception as e:
            t.update(state="trigger_err", why=str(getattr(e, "reason", e))[:150])
        rlog(event="trigger", site=site, job=t["job"], param=t["pname"], value=t["value"], user=user,
             queue=t.get("queue"), error=t.get("why") if t["state"] == "trigger_err" else None)

    def admin_flow(admin_ts):
        def prog(msg):
            for t in admin_ts:
                if t["state"] == "wait_admin":
                    t["why"] = msg
        try:
            ok, note = ensure_admin_build(admin_ts[0]["admin_ver"], started + RUN_TIMEOUT, prog)
        except Exception as e:
            log.exception("admin build failed")
            ok, note = False, "%s 出错：%s" % (app.ADMIN_BUILD_JOB, str(getattr(e, "reason", e))[:150])
        for t in admin_ts:
            if t["state"] != "wait_admin":
                continue
            if ok:
                t["admin_note"] = note
                trigger(t)
            else:
                t.update(state="admin_err", why=note)

    try:
        if resume:
            # 重启前没走完的：已触发但没拿到构建号的，按「确认之后开始的那次构建」找回来；
            # 找不到就如实说没触发，绝不补触发（不确定的时候重复发版比漏发更糟）
            log.info("resume %s", site)
            for t in targets:
                if t["state"] in ("triggering", "queued"):
                    num, url = _build_after(t, started)
                    if num:
                        t.update(state="running", num=num, url=url)
                    elif t["state"] == "triggering":
                        t.update(state="trigger_err", why="机器人重启时它还没触发，没有补发，需要的话重新 @我")
            admin_ts = [t for t in targets if t["state"] == "wait_admin"]
            if admin_ts:
                threading.Thread(target=admin_flow, args=(admin_ts,), daemon=True).start()
            render()
        else:
            # 1. 触发前再看一眼：正在构建的不重复触发
            jobs = jenkins_index(force=True)
            for t in targets:
                if t["which"] == AR and (jobs.get(t["job"]) or {}).get("building"):
                    t.update(state="skip", why="触发前发现它正在构建，没重复触发")
                    continue
                if t["which"] == ADMIN:
                    try:
                        lb = jget(ADMIN, "/job/%s/api/json" % q(t["job"]) + app._q("lastBuild[number,building]"))
                        if (lb.get("lastBuild") or {}).get("building"):
                            t.update(state="skip", why="触发前发现它正在构建，没重复触发")
                            continue
                    except Exception:
                        pass
                # 后台部署要等 build_admin 有这个版本的包，先挂起，由 admin_flow 放行
                t["state"] = "wait_admin" if t.get("admin_ver") else "triggering"
            render()
            # 2a. 后台：确保 build_admin 有这个版本（同版本全局只打一次），单独线程跑，不挡其它服务
            admin_ts = [t for t in targets if t["state"] == "wait_admin"]
            if admin_ts:
                threading.Thread(target=admin_flow, args=(admin_ts,), daemon=True).start()
            # 2b. 其它服务逐个触发（间隔 1 秒，别一瞬间打满 WAF）
            for t in targets:
                if t["state"] == "triggering":
                    trigger(t)
                    render()          # 每触发一个就落盘：此刻重启也知道哪些已经发出去了
                    time.sleep(1)
        # 3. 跟踪：队列 -> 拿到构建号 -> 等构建结束
        snap = lambda: [(t["state"], t.get("num"), t.get("why")) for t in targets]
        last = snap()
        q404 = {}
        while any(t["state"] in ("queued", "running", "wait_admin", "triggering") for t in targets):
            if time.time() - started > RUN_TIMEOUT:
                for t in targets:
                    if t["state"] in ("queued", "running", "wait_admin", "triggering"):
                        t["state"] = "timeout"
                break
            time.sleep(POLL_EVERY)
            for t in targets:
                try:
                    if t["state"] == "queued":
                        if not t.get("queue"):
                            num, url = _build_after(t, started)
                            if num:
                                t.update(state="running", num=num, url=url)
                            continue
                        qi = jget(t["which"], t["queue"].rstrip("/") + "/api/json" +
                                  app._q("cancelled,executable[number,url]"))
                        if qi.get("cancelled"):
                            t["state"] = "cancelled"
                        elif qi.get("executable"):
                            t.update(state="running", num=qi["executable"]["number"],
                                     url=qi["executable"].get("url", ""))
                    if t["state"] == "running":
                        b = jget(t["which"], "/job/%s/%s/api/json" % (q(t["job"]), t["num"]) + app._q(
                            "number,result,building,timestamp,duration,url,actions[parameters[name,value],"
                            "lastBuiltRevision[SHA1,branch[name]],causes[userName,shortDescription]]"))
                        if not b.get("building") and b.get("result"):
                            t.update(state="done", build=app.read_build(b))
                except urllib.error.HTTPError as e:
                    if e.code == 404 and t["state"] == "queued":
                        # 队列项刚被回收、构建号还没写回；连着几轮都 404 就按开始时间去 job 上找
                        q404[t["job"]] = q404.get(t["job"], 0) + 1
                        if q404[t["job"]] >= 3:
                            num, url = _build_after(t, started)
                            if num:
                                t.update(state="running", num=num, url=url)
                        continue
                    log.warning("poll %s: %s", t["job"], e)
                except Exception as e:
                    log.warning("poll %s: %s", t["job"], e)
            if snap() != last:                # 后台线程也会改状态，按快照比
                render()
                last = snap()
        render(final=True)
        thread = p.get("thread") or p["ts"]
        # 4a. 构建失败的附日志最后几行：多数时候看这几行就知道原因，省得点进 Jenkins 翻
        for t in targets:
            b = t.get("build") or {}
            if t.get("state") == "done" and b.get("result") != "SUCCESS" and t.get("num"):
                try:
                    lines = console_errors(t["which"], t["job"], t["num"])
                    client.chat_postMessage(channel=channel, thread_ts=thread,
                                            text="❌ *%s* `%s` #%s 报错：\n```%s```" % (
                                                t["view"], t["job"], t["num"], "\n".join(lines)[:3500]))
                except Exception as e:
                    log.warning("console tail %s: %s", t["job"], e)
        # 4b. 结论单独发一条，@ 确认人，方便手机上看到推送；有失败的带「重发失败的」按钮
        text = summary_text(site, p.get("disp", ""), targets, user, started)
        blocks = _sections(text)
        retry = [t for t in targets if _retryable(t)]
        if retry:
            pid = uuid.uuid4().hex[:12]
            rp = {"site": site, "disp": p.get("disp", ""), "channel": channel, "thread": thread,
                  "at": time.time(), "summary": text,
                  "head": "*%s %s 重发失败的 %d 个*" % (site, p.get("disp", ""), len(retry)),
                  "targets": [{k: t[k] for k in ("which", "view", "job", "pname", "value", "admin_ver") if k in t}
                              for t in retry]}
            with _plans_lock:
                PLANS[pid] = rp
            blocks += retry_blocks(rp, pid)
        client.chat_postMessage(channel=channel, thread_ts=thread, text=text, blocks=blocks)
        for t in targets:
            b = t.get("build") or {}
            rlog(event="result", site=site, job=t["job"], view="Admin" if t["which"] == ADMIN else t["view"],
                 num=t.get("num"), result=b.get("result") or t.get("state"), verdict=_verdict(t)[2],
                 branch=t.get("admin_ver") or b.get("got") or b.get("want"), sha=b.get("sha"), user=user)
        refresh_board(channel)                   # 看板原地更新（没开过看板就什么都不做）
    except Exception as e:
        log.exception("run_release failed")
        client.chat_postMessage(channel=channel, thread_ts=p.get("thread") or p["ts"],
                                text="❗ %s 发布过程出错，已停止跟踪（已触发的 job 仍在 Jenkins 上跑）：%s: %s"
                                     % (site, type(e).__name__, str(e)[:300]))
    finally:
        _unpersist(site)
        with _plans_lock:
            RUNNING.discard(site)


def resume_all(client):
    """机器人启动时接着跟踪重启前没发完的站点。"""
    for site, r in list((STATE.get("running") or {}).items()):
        p = {"site": site, "disp": r.get("disp", ""), "channel": r["channel"], "ts": r["ts"], "thread": r.get("thread")}
        with _plans_lock:
            RUNNING.add(site)
        try:
            client.chat_postMessage(channel=r["channel"], thread_ts=r.get("thread") or r["ts"],
                                    text="ℹ 机器人刚重启过，接着跟踪 %s 的发布结果。" % site)
        except Exception:
            log.exception("resume notice failed")
        threading.Thread(target=run_release, args=(p, r["user"], client, r), daemon=True).start()


@bolt.action("confirm")
def on_confirm(ack, body, client):
    ack()
    user = body["user"]["id"]
    pid = body["actions"][0]["value"]
    ch = body["channel"]["id"]
    if user not in ALLOWED:
        client.chat_postEphemeral(channel=ch, user=user, text="你没有发布权限，这个按钮只对授权的人有效。")
        return
    with _plans_lock:
        p = PLANS.get(pid)
        if not p or time.time() - p["at"] > PLAN_TTL:
            PLANS.pop(pid, None)
            msg = "这份计划已失效（超过 15 分钟、已确认过、或机器人重启过），请重新 @jenkins-bot %s" % (
                p["site"] if p else "站点编号")
            client.chat_postEphemeral(channel=ch, user=user, text=msg)
            return
        if p["site"] in RUNNING:
            client.chat_postEphemeral(channel=ch, user=user, text="%s 正在发布中，等这次结束再来。" % p["site"])
            return
        PLANS.pop(pid)                 # 先摘掉：连点两下不会触发两遍
        RUNNING.add(p["site"])
        p["ts"] = body["message"]["ts"]
    if p.get("summary"):
        # 重发：结论消息保留原样（只去掉按钮），进度另起一条消息，别把结论覆盖掉
        try:
            client.chat_update(channel=ch, ts=p["ts"], text=p["summary"],
                               blocks=_sections(p["summary"]) + [{"type": "context", "elements": [
                                   {"type": "mrkdwn", "text": "已点重发"}]}])
            r = client.chat_postMessage(channel=ch, thread_ts=p["thread"], text=p["head"] + " 开始…")
            p["ts"] = r["ts"]
        except Exception:
            log.exception("retry message failed")
    log.info("confirm %s by %s: %s", p["site"], user, [t["job"] for t in p["targets"]])
    rlog(event="confirm", site=p["site"], user=user, retry=bool(p.get("summary")),
         jobs=["%s %s=%s" % (t["job"], t["pname"], t["value"]) if t["pname"] else t["job"] for t in p["targets"]])
    threading.Thread(target=run_release, args=(p, user, client), daemon=True).start()
    board_async(ch)                     # 站点一确认，看板马上显示它在「构建中」


@bolt.action("cancel")
def on_cancel(ack, body, client):
    ack()
    pid = body["actions"][0]["value"]
    with _plans_lock:
        p = PLANS.pop(pid, None)
    if p and p.get("summary"):        # 「不重发」：保留结论，只去掉按钮
        client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], text=p["summary"],
                           blocks=_sections(p["summary"]) + [{"type": "context", "elements": [
                               {"type": "mrkdwn", "text": "已选不重发"}]}])
        return
    txt = "已取消%s，没有触发任何构建。" % ((" " + p["site"]) if p else "")
    client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], text=txt,
                       blocks=_sections("~%s~\n%s" % (p["head"] if p else "", txt) if p else txt))


# ---------------------------------------------------------------- 入口
def handle(text, channel, user, thread=None, say=None):
    """返回要回复的文本；出计划这种带按钮的由 cmd_plan 自己 say，返回 None。"""
    t = strip_mention(text)
    if not t or t.lower() in ("帮助", "help", "?", "？", "用法", "指令", "命令", "说明"):
        return HELP
    if t.startswith("当前"):
        return cmd_current(channel)
    if t.startswith("看板"):
        if not (STATE["channels"].get(channel) or {}).get("sites"):
            return "还没记这次发版的站点清单。先 `@jenkins-bot 站点` 贴站点清单，我再发看板。"
        board_async(channel, create=True)
        return "📋 看板已重新发在频道最下面，发布中每分钟自动刷新这一条。"
    if t.startswith("记录"):
        return cmd_history(t)
    if t.startswith("进度"):
        rest = re.sub(r"^进度[:：]?", "", t)
        pre = ""
        if app.parse_components(rest)[0]:          # 「进度」后面顺手贴了服务清单 → 先更新清单再算
            pre = cmd_services(channel, rest, user).split("\n")[0] + "\n\n"
        elif app.parse_sites(rest):                # 后面贴的是站点清单 → 先记站点再算
            pre = cmd_sites(channel, rest, user) + "\n\n"
        return pre + cmd_progress(channel)
    if t.startswith("站点"):
        return cmd_sites(channel, re.sub(r"^站点(清单)?[:：]?", "", t), user)
    # 「按视图发」必须排在「服务」判断前：`web ar107` 会被发布单解析当成一行「web → ar107」，
    # 把服务清单改成 Web=ar107
    view = None if t.startswith("服务") else parse_view_cmd(t)
    if isinstance(view, str):
        return view
    if not view and (t.startswith("服务") or app.parse_components(t)[0]):
        return cmd_services(channel, re.sub(r"^服务[:：]?", "", t), user)
    sites = app.parse_sites(t)
    if len(sites) > MAX_PLAN_SITES:
        # 几十个站多半是在贴站点清单，不是要一口气发这么多 —— 出几十条计划刷屏还容易误点
        return ("一次最多出 %d 个站的计划，你发了 %d 个。\n如果这是这次发版的站点清单，请用 `@jenkins-bot 站点` 开头再发一次。"
                % (MAX_PLAN_SITES, len(sites)))
    if sites:
        cmd_plan(channel, t, thread, say, view=view)
        return None
    return "没看懂。\n" + HELP


def _reply(event, say, thread):
    try:
        msg = handle(event.get("text", ""), event.get("channel"), event.get("user"), thread,
                     lambda **kw: say(**kw))
    except Exception as e:                  # 任何异常都要回话：静默 = 他以为机器人坏了或在跑
        log.exception("handle failed")
        msg = "出错了：%s: %s" % (type(e).__name__, str(e)[:300])
    if msg:
        say(text=msg, thread_ts=thread)


@bolt.event("app_mention")
def on_mention(event, say):
    _reply(event, say, event.get("thread_ts") or event.get("ts"))


@bolt.event("message")
def on_message(event, say):
    # 私聊：不用 @ 也能用。频道里的普通消息不理（app_mention 那边处理）
    if event.get("channel_type") != "im" or event.get("bot_id") or event.get("subtype"):
        return
    _reply(event, say, None)


if __name__ == "__main__":
    if not (J_USER and J_TOKEN and app.JENKINS_URL):
        raise SystemExit("缺 JENKINS_URL / JENKINS_USER / JENKINS_API_TOKEN")
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    log.info("jenkins-bot starting, state=%s, allowed=%s", STATE_FILE, sorted(ALLOWED))
    resume_all(bolt.client)
    threading.Thread(target=board_ticker, daemon=True).start()
    SocketModeHandler(bolt, os.environ["SLACK_APP_TOKEN"]).start()
