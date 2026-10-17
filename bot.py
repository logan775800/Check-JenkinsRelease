# -*- coding: utf-8 -*-
"""
jenkins-bot：在 Slack 里按站点出发布计划（P0：只读，不触发任何构建）。

用法（频道里 @jenkins-bot）：
    @jenkins-bot 服务            后面贴发布单的「服务 → 分支」那几行（也可以整份发布单）
    @jenkins-bot AR047           出 AR047 的发布计划：每个视图发哪个 job、传什么参数
    @jenkins-bot AR047 中台      站点分组不明时手动指定（指定一次会记住）
    @jenkins-bot 当前            看现在记着的服务清单
    @jenkins-bot 帮助

解析发布单、站点/组件识别、分支简写比对全部复用 app.py，两边口径一致。
跑在 Socket Mode：机器人主动连 Slack，服务器不需要开端口、不需要公网回调地址。
"""
import html
import json
import logging
import os
import re
import threading
import time

import app
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jenkins-bot")

STATE_FILE = os.environ.get("BOT_STATE", "/state/state.json")
J_USER, J_TOKEN = app.JENKINS_USER, app.JENKINS_TOKEN
INDEX_TTL = 120
PARAM_NAMES = ("TAG", "BRANCH_NAME", "BRANCH")   # 同 app.read_build：各类 job 参数名不统一

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

# ---------------------------------------------------------------- Jenkins 索引
_idx = {"ts": 0, "jobs": None}
_idx_lock = threading.Lock()


def jenkins_index(force=False):
    """一次拉全部 job 的参数定义 + 最后一次构建的参数。返回 {job名: {...}}。"""
    with _idx_lock:
        if not force and _idx["jobs"] is not None and time.time() - _idx["ts"] < INDEX_TTL:
            return _idx["jobs"]
        tree = ("jobs[name,url,property[parameterDefinitions[name,defaultParameterValue[value]]],"
                "lastBuild[number,result,building,timestamp,actions[parameters[name,value]]]]")
        data = app.jenkins_get("/api/json" + app._q(tree), J_USER, J_TOKEN, timeout=180)
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
            jobs[name] = {"site": site, "comp": comp, "url": j.get("url", ""), "pname": pname,
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


# ---------------------------------------------------------------- 命令
HELP = (
    "*jenkins-bot 用法*（现在是 P0：只出计划，不会触发任何构建）\n"
    "1. `@jenkins-bot 服务` 换行贴发布单的「服务 → 分支」几行（整份发布单也行，站点分组会一起记住）\n"
    "2. `@jenkins-bot AR047` 出这个站的发布计划；多个站用空格隔开\n"
    "3. 站点分组不明时：`@jenkins-bot AR047 中台`（记住，下次不用再说）\n"
    "4. `@jenkins-bot 当前` 看记着的服务清单"
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


def plan_site(site, comps, group_hint, jobs):
    """出一个站点的计划，返回 Slack 文本。"""
    site_jobs = {n: j for n, j in jobs.items() if j["site"] == site}
    if not site_jobs:
        return "*%s*：Jenkins 上没有这个站点的任何 job，编号写错了？" % site
    disp = ""
    for n in sorted(site_jobs):
        if site_jobs[n]["comp"] == "Pages":
            disp = n.split("-")[-1]
            break
    disp = disp or sorted(site_jobs)[0].split("-")[-1]

    need_group = any(c["group"] for c in comps)
    group = group_hint or STATE["site_groups"].get(site, "")
    gsrc = "本次指定" if group_hint else ("记住的" if group else "")

    lines, todo, warns = [], 0, []
    for c in comps:
        view = app.COMPONENT_VIEW.get(c["name"], c["name"])
        if c["site"] and c["site"] != site:
            continue                                    # 站点级特例，不是这个站
        if c["group"]:
            if not group:
                continue                                # 下面统一提示
            if c["group"] != group:
                continue
        if c["name"] == app.ADMIN_COMP:
            lines.append("• *后台 Admin* → `%s`　两步：先 `build_admin` 打这个版本，再发本站后台 job"
                         "（P2 才接，现在请手动）" % c["expect"])
            continue
        # 同一站点同组件可能有多个 job（AR013 的 Pages 有两个），全部列出，一个都不能漏
        targets = sorted(n for n, j in site_jobs.items() if j["comp"] == c["name"])
        if not targets:
            lines.append("• *%s* → ❓ 本站没有 %s 的 job（这个站不发这个服务？）" % (view, c["name"]))
            continue
        val, note = resolve_value(c["expect"], c["name"], jobs)
        for n in targets:
            j = site_jobs[n]
            if not j["pname"]:
                lines.append("• *%s* `%s` → ❓ 这个 job 没有 TAG/BRANCH_NAME 参数，不知道该传什么" % (view, n))
                continue
            if val is None:
                lines.append("• *%s* `%s` → ❌ %s" % (view, n, note))
                continue
            flags = []
            if note:
                flags.append(note)
            if j["default"] and not app.ref_matches(j["default"], val):
                flags.append("⚠ 默认值是 `%s`，手点不改就会发错" % app.normalize_ref(j["default"]))
            if j["building"]:
                flags.append("⏳ 正在构建 #%s" % j["num"])
            elif j["last"] and app.ref_matches(j["last"], val) and j["result"] == "SUCCESS":
                flags.append("ℹ 上次 #%s（%s）已经是这个版本" % (j["num"], fmt_ts(j["ts"])))
            lines.append("• *%s* `%s`　%s=`%s`　_上次 %s_%s" % (
                view, n, j["pname"], val, app.normalize_ref(j["last"]) or "—",
                ("\n　　" + "；".join(flags)) if flags else ""))
            todo += 1
    head = "*%s %s*" % (site, disp)
    if need_group:
        head += "（%s%s）" % (group or "分组未知", "，" + gsrc if gsrc else "")
    head += " — 共 %d 个 job" % todo
    if need_group and not group:
        pages = [j for j in site_jobs.values() if j["comp"] == "Pages" and j["last"]]
        hint = ("上次 Pages 发的是 `%s`" % app.normalize_ref(pages[0]["last"])) if pages else ""
        warns.append("❓ 有服务按中台/非中台分别发，但我不知道 %s 属于哪组%s。回复 `@jenkins-bot %s 中台` 或 `%s 非中台`"
                     % (site, "（" + hint + "，仅供参考，不拿它推断）" if hint else "", site, site))
    return "\n".join([head] + lines + warns)


def cmd_plan(channel, text):
    ch = STATE["channels"].get(channel)
    if not ch:
        return "这个频道还没记服务清单。先 `@jenkins-bot 服务` 换行贴发布单。"
    sites = app.parse_sites(text)
    g = ""
    if re.search(r"非中台", text):
        g = "非中台"
    elif re.search(r"中台", text):
        g = "中台"
    if g:
        with _state_lock:
            for s in sites:
                STATE["site_groups"][s] = g
            save_state(STATE)
    jobs = jenkins_index()
    body = "\n\n".join(plan_site(s, ch["comps"], g, jobs) for s in sites)
    return body + "\n\n_P0：只出计划，不会触发构建。请对照视图和分支，确认无误后先手动点；P1 起这里会有「确认发布」按钮。_"


def handle(text, channel, user):
    t = strip_mention(text)
    if not t or t in ("帮助", "help", "?", "？"):
        return HELP
    if t.startswith("当前"):
        return cmd_current(channel)
    if t.startswith("服务") or app.parse_components(t)[0]:
        return cmd_services(channel, re.sub(r"^服务[:：]?", "", t), user)
    if app.parse_sites(t):
        return cmd_plan(channel, t)
    return "没看懂。\n" + HELP


@bolt.event("app_mention")
def on_mention(event, say):
    thread = event.get("thread_ts") or event.get("ts")
    try:
        msg = handle(event.get("text", ""), event.get("channel"), event.get("user"))
    except Exception as e:                  # 任何异常都要回话：静默 = 他以为机器人坏了或在跑
        log.exception("handle failed")
        msg = "出错了：%s: %s" % (type(e).__name__, str(e)[:300])
    say(text=msg, thread_ts=thread)


@bolt.event("message")
def on_message(event, say):
    # 私聊：不用 @ 也能用。频道里的普通消息不理（app_mention 那边处理）
    if event.get("channel_type") != "im" or event.get("bot_id") or event.get("subtype"):
        return
    try:
        msg = handle(event.get("text", ""), event.get("channel"), event.get("user"))
    except Exception as e:
        log.exception("handle failed")
        msg = "出错了：%s: %s" % (type(e).__name__, str(e)[:300])
    say(text=msg)


if __name__ == "__main__":
    if not (J_USER and J_TOKEN and app.JENKINS_URL):
        raise SystemExit("缺 JENKINS_URL / JENKINS_USER / JENKINS_API_TOKEN")
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    log.info("jenkins-bot starting, state=%s", STATE_FILE)
    SocketModeHandler(bolt, os.environ["SLACK_APP_TOKEN"]).start()
