# -*- coding: utf-8 -*-
"""
离线回归：把 jenkins_get 换成假的，验证 run_check 的判定和取数逻辑。

不碰真 Jenkins，不需要 Token，改完 app.py 直接跑：
    python tools\\selftest.py
"""
import os, sys, time, threading, urllib.parse

os.environ["JENKINS_URL"] = "https://fake"
os.environ["JENKINS_ALLOW_SERVER_CREDS"] = "1"
os.environ["JENKINS_MAX_BUILDS"] = "5"          # 调小才好造「被截断」的场景
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app

NOW = time.mktime(time.strptime("2026-08-07 10:00:00", "%Y-%m-%d %H:%M:%S"))
DAY = 86400
CALLS = []


def mkbuild(num, ts, result="SUCCESS", tag="master_V3.08_001", sha="abcdef1234",
            building=False, param="TAG", rev=True):
    acts = []
    if param:
        acts.append({"parameters": [{"name": param, "value": tag}]})
    if rev:
        acts.append({"lastBuiltRevision": {"SHA1": sha, "branch": [{"name": "refs/remotes/origin/" + tag}]}})
    acts.append({"causes": [{"userName": "logan"}]})
    return {"number": num, "result": result, "building": building, "timestamp": int(ts * 1000),
            "duration": 42000, "url": "https://fake/job/x/%d/" % num, "actions": acts}


JOBS = {}


def add(name, builds):
    JOBS[name] = builds


# 正常发布：窗口内成功、tag 对
for n in (1, 2, 3):
    add("AR00%d-Pages-印度-site%d" % (n, n), [mkbuild(10, NOW, param="BRANCH_NAME")])
add("AR004-Pages-印度-site4", [mkbuild(10, NOW, tag="master_V3.07_009", param="BRANCH_NAME")])   # 发错版本
add("AR005-Pages-印度-site5", [mkbuild(10, NOW, result="FAILURE", param="BRANCH_NAME")])          # 构建失败
add("AR006-Pages-印度-site6", [mkbuild(9, NOW - 30 * DAY, param="BRANCH_NAME")])                  # 真·未发布
# 截断陷阱：5 条构建全都晚于时间窗，正好取满 MAX_BUILDS
add("AR007-Pages-印度-site7", [mkbuild(20 - i, NOW + (5 - i) * DAY, param="BRANCH_NAME") for i in range(5)])
add("AR008-Pages-印度-site8", [mkbuild(10, NOW, param=None, rev=False)])                          # 没记录版本
add("AR009-Pages-印度-site9", [mkbuild(10, NOW, result=None, building=True, param="BRANCH_NAME")])  # 构建中
add("AR010-Pages", [mkbuild(10, NOW, param="BRANCH_NAME")])                # 两段式 job 名
add("AR001-Pages-印度-site1-h5", [mkbuild(11, NOW, param="BRANCH_NAME")])  # 同站点同组件多 job
for s in ("AR001", "AR002", "AR020"):
    add("%s-LotteryApi-印度-x" % s, [mkbuild(10, NOW)])


# tree 参数里的 [ ] { } , 是 safe 字符，不会被百分号编码，所以路径可以直接按子串认
NAMES_PATH = "jobs[name,url]"
BULK_PATH = "jobs[name,builds["


def is_names(p):
    return NAMES_PATH in p


def is_bulk(p):
    return BULK_PATH in p


def fake_get(path, user, token_, timeout=180):
    CALLS.append(path)
    if is_names(path):
        return {"jobs": [{"name": n, "url": "https://fake/job/%s/" % n} for n in JOBS]}
    if path.startswith("/job/"):
        name = urllib.parse.unquote(path.split("/job/")[1].split("/api/json")[0])
        if name not in JOBS:
            raise RuntimeError("no such job " + name)
        return {"builds": JOBS[name][:app.MAX_BUILDS_PER_JOB]}
    if is_bulk(path):
        return {"jobs": [{"name": n, "builds": b[:app.MAX_BUILDS_PER_JOB]} for n, b in JOBS.items()]}
    raise RuntimeError("unexpected path " + path)


app.jenkins_get = fake_get


def reset():
    app._cache.clear(); app._locks.clear(); del CALLS[:]


def run(text, **kw):
    kw.setdefault("date_str", "2026-08-07"); kw.setdefault("days", 1)
    kw.setdefault("exclude", []); kw.setdefault("all_sites", False)
    kw.setdefault("force", False); kw.setdefault("user", "logan"); kw.setdefault("token_", "t")
    return app.run_check(text, **kw)


def states(res):
    out = {}
    for r in res["rows"]:
        out.setdefault(r["state"], []).append(r["site"])
    return {k: sorted(set(v)) for k, v in out.items()}


fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (("   " + str(extra)) if not cond else ""))
    if not cond:
        fails.append(label)


MANIFEST = """【发布站点】
（印度）AR001→site1
（印度）AR002→site2
（印度）AR003→site3
（印度）AR004→site4
（印度）AR005→site5
（印度）AR006→site6
（印度）AR007→site7
（印度）AR008→site8
（印度）AR009→site9
（印度）AR010→site10

【发布步骤】
1.发代码
    web    →    分支：master_V3.08_001
"""

print("\n== 1. 状态判定 ==")
reset()
r = run(MANIFEST)
st = states(r)
check("AR001/002/003/010 判 OK", set(st.get("OK", [])) >= {"AR001", "AR002", "AR003", "AR010"}, st)
check("AR004 版本不符 VER", st.get("VER") == ["AR004"], st)
check("AR005 构建失败 FAIL", st.get("FAIL") == ["AR005"], st)
check("AR008 版本未知 NOVER", st.get("NOVER") == ["AR008"], st)
check("AR009 构建中 RUN", st.get("RUN") == ["AR009"], st)
check("AR006/AR007 未发布 MISS", sorted(st.get("MISS", [])) == ["AR006", "AR007"], st)
check("两段式 job 名 AR010-Pages 能匹配", "AR010" in st.get("OK", []), st)
check("同站点多 job 都出行", len([x for x in r["rows"] if x["site"] == "AR001"]) == 2,
      [x["job"] for x in r["rows"] if x["site"] == "AR001"])

print("\n== 2. 截断告警 ==")
trunc = [w for w in r["warnings"] if "都晚于时间窗" in w]
check("发出截断告警", len(trunc) == 1, r["warnings"])
check("只算 AR007 一个", "1 个任务" in (trunc[0] if trunc else ""), trunc)
a7 = [x for x in r["rows"] if x["site"] == "AR007"][0]
a6 = [x for x in r["rows"] if x["site"] == "AR006"][0]
check("AR007 行写了「可能没查全」", "可能没查全" in a7["detail"], a7["detail"])
check("AR006 行仍是「时间窗内没有构建」", a6["detail"] == "时间窗内没有构建", a6["detail"])

print("\n== 3. 按需拉 ==")
job_calls = [c for c in CALLS if c.startswith("/job/")]
check("没走一次性全量", not any(is_bulk(c) for c in CALLS), CALLS[:3])
check("只拉计划内的 11 个 job", len(job_calls) == 11, len(job_calls))
check("没拉无关的 LotteryApi", not any("LotteryApi" in c for c in job_calls), job_calls)

print("\n== 4. 缓存与强制刷新 ==")
before = len(CALLS)
run(MANIFEST)
check("TTL 内二次核对不打 Jenkins", len(CALLS) == before, CALLS[before:])
run(MANIFEST, force=True)
check("强制刷新会重新拉", len(CALLS) > before)

print("\n== 5. 超阈值退回一次性全量 ==")
reset()
app.BULK_THRESHOLD = 3
run(MANIFEST)
check("走了 bulk", any(is_bulk(c) for c in CALLS), CALLS)
check("没有逐个拉", not any(c.startswith("/job/") for c in CALLS))
app.BULK_THRESHOLD = 200

print("\n== 6. 逐个拉被拒 → 早停并退回全量 ==")
reset()
orig = app.jenkins_get


tried = []


def flaky(path, user, token_, timeout=180):
    if path.startswith("/job/"):
        tried.append(path)                     # 抛之前先计数，否则测不到到底打了几次
        raise RuntimeError("429 rate limited")
    return orig(path, user, token_, timeout)


app.jenkins_get = flaky
r6 = run(MANIFEST)
check("退回全量后仍拿到结果", bool(states(r6).get("OK")), states(r6))
check("确实调了 bulk", any(is_bulk(c) for c in CALLS))
# 11 个待拉，连错 5 个就收手。并发 6，最坏情况是在途那批也打完，所以上界取 11
check("早停：没把 11 个全打完（实际 %d 个）" % len(tried), len(tried) < 11, len(tried))
app.jenkins_get = orig

print("\n== 7. 全量 + 分组式发布单 ==")
GROUPED = """【发布站点】
中台站点
（印度）AR001→site1
非中台站点
（印度）AR002→site2

【发布步骤】
    web  分支：
        中台版本分支：  master_V3.08_001
        非中台版本分支：master_V3.07_009
"""
reset()
r7 = run(GROUPED, all_sites=True)
nog = [w for w in r7["warnings"] if "找不到分组归属" in w]
check("全量模式发出「找不到分组归属」告警", len(nog) == 1, r7["warnings"])
check("告警点名了漏掉的站点", ("AR005" in nog[0]) if nog else False, nog)
reset()
r7b = run(GROUPED)
check("站点都有分组时不误报", not [w for w in r7b["warnings"] if "找不到分组归属" in w], r7b["warnings"])
check("中台站点按 3.08 判 OK", any(x["site"] == "AR001" and x["state"] == "OK" for x in r7b["rows"]), states(r7b))
check("非中台站点按 3.07 判 VER", any(x["site"] == "AR002" and x["state"] == "VER" for x in r7b["rows"]), states(r7b))

print("\n== 8. 解析不出组件时不打 Jenkins ==")
reset()
r8 = run("随便写点什么，没有组件行")
check("直接返回 ok=False", r8["ok"] is False)
check("一次 Jenkins 都没打", CALLS == [], CALLS)

print("\n== 9. 真实发布单能解析 ==")
real_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "local", "发布单原文.txt")
if os.path.exists(real_path):
    with open(real_path, encoding="utf-8") as f:
        real = f.read()
    comps, _ = app.parse_components(real)
    sites = app.parse_sites(real)
    check("解析出 1 个组件 Pages", len(comps) == 1 and comps[0]["name"] == "Pages", comps)
    check("期望值 masterBranch/main-3.04", comps[0]["expect"] == "masterBranch/main-3.04", comps)
    check("解析出 35 个站点", len(sites) == 35, len(sites))
else:
    print("  SKIP  local/发布单原文.txt 不存在")

print("\n== 10. 组件名拼错要能认出来 ==")
reset()
# 真实案例：发布单把 WebIntranetApi 写成 webIntrenetApi（Intranet 的 a/e 对调），
# 旧版整个组件不核对，而报告里「没核对」和「没问题」长得一模一样。
# Intrenet 是发布单里的**既定写法**，不是笔误：必须精确识别且**一条告警都不许有**。
# 天天核对天天弹「已按最接近的…核对」就是噪音，他明确反馈过。
for spell in ("webIntrenetApi", "WebIntrenetApi", "intrenetApi", "web-intrenet-api"):
    c, w = app.parse_components(f"    {spell}  →  tag: master_V3.09_308")
    check(f"{spell} 直接识别为 WebIntranetApi",
          len(c) == 1 and c[0]["name"] == "WebIntranetApi", c)
    check(f"{spell} 不弹任何告警", not w, w)
check("期望值没被弄丢", c and c[0]["expect"] == "master_V3.09_308", c)

for typo, want in (("LoterryApi", "LotteryApi"), ("webextandapi", "WebExtendApi"),
                   ("ThirdJobb", "ThirdJob"), ("Pagess", "Pages")):
    c, _ = app.parse_components(f"    {typo} → tag: v1")
    check(f"{typo} -> {want}", len(c) == 1 and c[0]["name"] == want, c)

for junk in ("随便写个啥", "api", "aaa", "ThirdXyz"):
    c, w = app.parse_components(f"    {junk} → tag: v2")
    check(f"「{junk}」不许瞎猜", not c and any("不认识" in x for x in w), (c, w))

# 精确匹配的老路不能被近似匹配抢走
for exact, want in (("web", "Pages"), ("前端", "Pages"), ("LotteryApi", "LotteryApi")):
    c, w = app.parse_components(f"    {exact} → tag: v1")
    check(f"精确匹配 {exact} 不走近似", len(c) == 1 and c[0]["name"] == want
          and not any("相似度" in x for x in w), (c, w))

c, w = app.parse_components("    admin → tag: v1")
check("admin 仍判「不在这套 Jenkins」", not c and any("不在这套" in x for x in w), w)

# 阈值守卫：这三个数是当初量出来定 0.80 的依据，谁改阈值先看这里
import difflib as _dl
_r = lambda a, b: _dl.SequenceMatcher(None, app.canon(a), app.canon(b)).ratio()
_names = sorted(set(app.COMPONENT_ALIAS.values()))
_pairs = max(_r(a, b) for i, a in enumerate(_names) for b in _names[i + 1:])
check("阈值高于「任意两个真实组件的相似度」上限 %.3f" % _pairs, app.FUZZY_MIN > _pairs, _pairs)
check("目标错拼仍在阈值之上", _r("webIntrenetApi", "WebIntranetApi") >= app.FUZZY_MIN)

# 限定词行不许开近似匹配，否则说明文字会被当成组件吃掉
GROUPED_TYPO = """【发布站点】
中台站点
（印度）AR001→site1

【发布步骤】
    webIntrenetApi  分支：
        中台版本分支：  master_V3.08_001
"""
c, w = app.parse_components(GROUPED_TYPO)
check("错拼组件 + 分组写法仍能解析",
      len(c) == 1 and c[0]["name"] == "WebIntranetApi" and c[0]["group"] == "中台", c)

print("\n== 11. 后台(Admin)走第二套 Jenkins ==")
ADMIN_MANIFEST = """【发布站点】
（印度）AR001→site1

【发布步骤】
    web    →   分支：master_V3.08_001
    admin  →   tag: master_V3.09_308
"""

# --- 没配第二套时：必须还是原来那句提示，不能报错也不能假装核对了 ---
reset()
c, w = app.parse_components(ADMIN_MANIFEST)
check("没配后台 Jenkins 时不当成组件", all(x["name"] != app.ADMIN_COMP for x in c), c)
check("没配时给出「不在这套 Jenkins」提示", any("不在这套" in x for x in w), w)

# --- 配上第二套 ---
app.ADMIN_URL, app.ADMIN_USER, app.ADMIN_TOKEN = "https://admin-fake", "u", "t"
app.ADMIN_JOBS = ["Sit-Admin", "sit-admin-非saas彩票"]
ADMIN_JOBDATA = {
    "Sit-Admin": [mkbuild(7, NOW, tag="master_V3.09_308", param="BRANCH")],
    "sit-admin-非saas彩票": [mkbuild(3, NOW, tag="master_V3.08_001", param="BRANCH")],
}
admin_calls = []
_plain_get = app.jenkins_get


def get_with_admin(path, user, token_, timeout=180, base=None):
    if base == "https://admin-fake":
        admin_calls.append((path, user, token_))
        name = urllib.parse.unquote(path.split("/job/")[1].split("/api/json")[0])
        if name not in ADMIN_JOBDATA:
            raise RuntimeError("no such job " + name)
        return {"builds": ADMIN_JOBDATA[name][:app.MAX_BUILDS_PER_JOB]}
    return _plain_get(path, user, token_, timeout)


app.jenkins_get = get_with_admin
reset()
c, w = app.parse_components(ADMIN_MANIFEST)
check("配好后 admin 解析成组件", any(x["name"] == app.ADMIN_COMP for x in c), c)
check("配好后不再提示「不在这套」", not any("不在这套" in x for x in w), w)

r11 = run(ADMIN_MANIFEST)
arows = [x for x in r11["rows"] if x["comp"] == app.ADMIN_COMP]
check("两个后台 job 各出一行", len(arows) == 2, arows)
check("BRANCH 参数能读出版本",
      any(x["actual"] == "master_V3.09_308" for x in arows), arows)
check("版本对的判 OK",
      [x["state"] for x in arows if x["siteName"] == "Sit-Admin"] == ["OK"], arows)
check("版本不对的判 VER",
      [x["state"] for x in arows if x["siteName"] == "sit-admin-非saas彩票"] == ["VER"], arows)
check("后台行不伪造成按站点发（site 恒为「后台」）",
      {x["site"] for x in arows} == {"后台"}, arows)
check("用的是后台自己的凭据，不是 AR 那套",
      admin_calls and all(u == "u" and t == "t" for _p, u, t in admin_calls), admin_calls)
check("AR 站点照常核对，没被后台挤掉",
      any(x["comp"] == "Pages" and x["site"] == "AR001" for x in r11["rows"]), r11["rows"])

# --- 后台某个 job 拉不到：不能把整块后台核对带崩 ---
reset()
del admin_calls[:]


def get_admin_flaky(path, user, token_, timeout=180, base=None):
    if base == "https://admin-fake" and "Sit-Admin/api" in path.replace("%2D", "-"):
        raise RuntimeError("connection refused")
    return get_with_admin(path, user, token_, timeout, base)


app.jenkins_get = get_admin_flaky
r11b = run(ADMIN_MANIFEST)
arows_b = [x for x in r11b["rows"] if x["comp"] == app.ADMIN_COMP]
check("一个后台 job 挂了，另一个照常出结果", len(arows_b) >= 1, arows_b)
check("拉失败要告警，不能静默", any("后台 Jenkins" in x for x in r11b["warnings"]), r11b["warnings"])
app.jenkins_get = _plain_get
app.ADMIN_URL = app.ADMIN_USER = app.ADMIN_TOKEN = ""
app.ADMIN_JOBS = ["Sit-Admin"]

print("\n== 12. 并发去重 ==")
reset()
plain = app.jenkins_get


def slow_get(path, user, token_, timeout=180):
    if is_names(path):
        time.sleep(0.3)
    return plain(path, user, token_, timeout)


app.jenkins_get = slow_get
ths = [threading.Thread(target=lambda: run(MANIFEST)) for _ in range(3)]
for t in ths:
    t.start()
for t in ths:
    t.join()
check("名单只拉一次", len([c for c in CALLS if is_names(c)]) == 1,
      len([c for c in CALLS if is_names(c)]))
check("每个 job 只拉一次", len([c for c in CALLS if c.startswith("/job/")]) == 11,
      len([c for c in CALLS if c.startswith("/job/")]))
app.jenkins_get = plain

print("\n" + ("全部通过" if not fails else "失败 %d 项：%s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
