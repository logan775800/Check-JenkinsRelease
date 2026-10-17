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
                "lastBuild[number,result,building,timestamp,actions[parameters[name,value]]]]")
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
            last = ""
            for a in lb.get("actions") or []:
                for p in (a or {}).get("parameters") or []:
                    if p.get("name") == pname:
                        last = p.get("value") or ""
            jobs[name] = {"site": site, "comp": comp, "pname": pname,
                          "default": default, "last": last, "num": lb.get("number"),
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
HELP = (
    "*jenkins-bot 用法*\n"
    "1. `@jenkins-bot 服务` 换行贴发布单的「服务 → 分支」几行（整份发布单也行，站点分组会一起记住）\n"
    "2. `@jenkins-bot AR047` 出这个站的发布计划，核对后点 *确认发布*\n"
    "3. 站点分组不明时：`@jenkins-bot AR047 中台`（记住，下次不用再说）\n"
    "4. `@jenkins-bot 当前` 看记着的服务清单\n"
    "后台：build_admin 你自己先打包，我只核对版本对不对，对了才发本站后台。"
)


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
    return "• *%s*%s → `%s`　_发布单写的「%s」_" % (view, scope, c["expect"], c["raw"])


# ---------------------------------------------------------------- 命令：服务 / 当前
def cmd_services(channel, text, user):
    comps, warns = app.parse_components(text)
    if not comps:
        return ("没从这段里认出任何服务。格式像这样：\n```V1-LotteryApi   →  分支：master_V3.11_015\n"
                "Web             →  分支：非中台 uat ／ 中台 v3-uat```" +
                ("\n" + "\n".join("⚠ " + w for w in warns) if warns else ""))
    groups = {k: v for k, v in app.parse_groups(text).items() if v}
    with _state_lock:
        STATE["channels"][channel] = {"comps": comps, "by": user, "at": time.time()}
        STATE["site_groups"].update(groups)
        save_state(STATE)
    out = ["已记下 %d 个服务（之后 @我 站点编号就按这份出计划）：" % len(comps)]
    out += [comp_line(c) for c in comps]
    if groups:
        out.append("站点分组也记住了：" + "、".join("%s=%s" % kv for kv in sorted(groups.items())))
    out += ["⚠ " + w for w in warns]
    return "\n".join(out)


def cmd_current(channel):
    ch = STATE["channels"].get(channel)
    if not ch:
        return "这个频道还没记服务清单。先 `@jenkins-bot 服务` 贴发布单。"
    return ("当前服务清单（%s 由 <@%s> 设置）：\n" % (fmt_ts(ch["at"]), ch["by"]) +
            "\n".join(comp_line(c) for c in ch["comps"]))


# ---------------------------------------------------------------- 计划
def build_plan(site, comps, group_hint, jobs):
    """返回 dict：head / lines（展示）/ targets（确认后要触发的）/ extra（提示）。"""
    site_jobs = {n: j for n, j in jobs.items() if j["site"] == site}
    if not site_jobs:
        return {"site": site, "head": "*%s*：Jenkins 上没有这个站点的任何 job，编号写错了？" % site,
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
                lines.append("• *%s* `%s` → ❓ 没有 TAG/BRANCH_NAME 参数，不知道该传什么，不发" % (view, n))
                continue
            if val is None:
                lines.append("• *%s* `%s` → ❌ %s，不发" % (view, n, note))
                continue
            flags = [note] if note else []
            if branch_switch(j["last"], val):
                flags.append("↔ *换分支*：上次 `%s`，本次 `%s`" % (app.normalize_ref(j["last"]), val))
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
                       "text": "先把上面的 ❓ 说清楚，我再给你确认按钮。"}]})
    return blocks


def cmd_plan(channel, text, thread, say):
    ch = STATE["channels"].get(channel)
    if not ch:
        say(text="这个频道还没记服务清单。先 `@jenkins-bot 服务` 换行贴发布单。", thread_ts=thread)
        return
    sites = app.parse_sites(text)
    g = "非中台" if re.search(r"非中台", text) else ("中台" if re.search(r"中台", text) else "")
    if g:
        with _state_lock:
            for s in sites:
                STATE["site_groups"][s] = g
            save_state(STATE)
    jobs = jenkins_index(force=True)          # 出计划用最新状态：要据此判断「正在构建」
    for s in sites:                           # 一个站一条消息、一组按钮，各确认各的
        p = build_plan(s, ch["comps"], g, jobs)
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


def summary_text(site, disp, targets, user, started):
    """发完后的结论：像 release 机器人那样一张表列出本次每个服务的实际分支，问题项再单独列链接。"""
    cats = {}
    for t in targets:
        cats.setdefault(_verdict(t)[2], []).append(t)
    n_ok, n_bad, n_wrong, n_skip = (len(cats.get(k, [])) for k in ("ok", "bad", "wrong", "skip"))
    if n_ok == len(targets):
        title = "✅ *%s %s 全部发布成功 %d/%d*，分支已逐个核对" % (site, disp, n_ok, len(targets))
    else:
        title = "*%s %s 发布结果*：✅ 成功 %d　❌ 失败 %d　⚠ 发错 %d　⏭ 跳过 %d" % (
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
    lines.append("<@%s> 用时 %d 分钟（%s 起）。可以接着 @我 下一个站。" % (
        user, (time.time() - started) // 60, fmt_ts(started)))
    return "\n".join(lines)


def run_release(p, user, client, resume=None):
    """resume 不为空 = 机器人重启后接着跟踪（resume 是持久化的那份记录）。"""
    site, channel = p["site"], p["channel"]
    if resume:
        targets, started = resume["targets"], resume["started"]
    else:
        targets, started = [dict(t) for t in p["targets"]], time.time()

    def render(final=False):
        head = "*%s %s* %s（<@%s> %s 确认）" % (site, p.get("disp", ""), "发布结束" if final else "发布中",
                                            user, fmt_ts(started))
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
        # 4. 结论单独发一条，@ 确认人，方便手机上看到推送
        client.chat_postMessage(channel=channel, thread_ts=p.get("thread") or p["ts"],
                                text=summary_text(site, p.get("disp", ""), targets, user, started))
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
    log.info("confirm %s by %s: %s", p["site"], user, [t["job"] for t in p["targets"]])
    threading.Thread(target=run_release, args=(p, user, client), daemon=True).start()


@bolt.action("cancel")
def on_cancel(ack, body, client):
    ack()
    pid = body["actions"][0]["value"]
    with _plans_lock:
        p = PLANS.pop(pid, None)
    txt = "已取消%s，没有触发任何构建。" % ((" " + p["site"]) if p else "")
    client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], text=txt,
                       blocks=_sections("~%s~\n%s" % (p["head"] if p else "", txt) if p else txt))


# ---------------------------------------------------------------- 入口
def handle(text, channel, user, thread=None, say=None):
    """返回要回复的文本；出计划这种带按钮的由 cmd_plan 自己 say，返回 None。"""
    t = strip_mention(text)
    if not t or t in ("帮助", "help", "?", "？"):
        return HELP
    if t.startswith("当前"):
        return cmd_current(channel)
    if t.startswith("服务") or app.parse_components(t)[0]:
        return cmd_services(channel, re.sub(r"^服务[:：]?", "", t), user)
    if app.parse_sites(t):
        cmd_plan(channel, t, thread, say)
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
    SocketModeHandler(bolt, os.environ["SLACK_APP_TOKEN"]).start()
