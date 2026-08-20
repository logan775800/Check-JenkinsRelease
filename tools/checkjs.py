# -*- coding: utf-8 -*-
"""
给 app.py 里内联那段 JS 做静态检查。这台机器上没有 node，改完页面脚本没法真跑一遍。

两关：
  1. 括号/引号平衡 —— 抓「少一个引号」这类整页 JS 直接死掉的错；
  2. 用了但没声明的标识符 —— 抓 `st is not defined` 这类。

第二关是 2026-08-19 踩出来的：一处字符串替换静默没生效，`const st=...` 没进去、
`+st+` 进去了，平衡校验全绿，页面却白屏只留一句 "st is not defined"。
平衡校验对这种错完全无能为力，必须单独一关。

    python tools\\checkjs.py
"""
import io, os, re, sys

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
src = io.open(APP, encoding="utf-8").read()
js = re.search(r"<script>\n(.*?)</script>", src, re.S).group(1)

BS = chr(92)                     # 反斜杠，避免在本文件里再套一层转义
QUOTES = "'" + '"' + chr(96)     # ' " `
REGEX_OK = set("(,=:[!&|?{};+-*%~^<>") | {"\n", ""}
PAIRS = {")": "(", "]": "[", "}": "{"}

errs = []


def scan(text):
    """扫一遍：既做平衡校验，又产出「挖掉字符串/注释/正则」的代码骨架。

    两件事必须用同一个扫描器 —— 分开写会漏掉正则字面量里的引号，
    骨架就从那里开始整段错位（第一版就是这么产生几十个误报的）。
    骨架里用等长空格替换被挖掉的内容，行号才对得上。
    """
    out = []
    stack = []
    i, n, line = 0, len(text), 1

    def blank(seg):
        out.append("".join(ch if ch == "\n" else " " for ch in seg))

    prev = ""
    while i < n:
        c = text[i]
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            blank(text[i:j]); i = j; continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            line += text.count("\n", i, j)
            blank(text[i:j]); i = j; continue
        if c in QUOTES:
            q, j, start = c, i + 1, line
            closed = False
            while j < n:
                if text[j] == BS:
                    j += 2; continue
                if text[j] == q:
                    closed = True; break
                if text[j] == "\n":
                    line += 1
                    if q != chr(96):
                        errs.append("第 %d 行：%s 引号跨行未闭合" % (start, q)); break
                j += 1
            if not closed and j >= n:
                errs.append("第 %d 行：%s 引号到结尾都没闭合" % (start, q))
            blank(text[i:j + 1]); prev = q; i = j + 1; continue
        if c == "/" and prev in REGEX_OK:
            j, incls = i + 1, False
            while j < n and text[j] != "\n":
                if text[j] == BS:
                    j += 2; continue
                if text[j] == "[":
                    incls = True
                elif text[j] == "]":
                    incls = False
                elif text[j] == "/" and not incls:
                    break
                j += 1
            if j < n and text[j] == "/":
                j += 1
                while j < n and text[j] in "gimsuy":
                    j += 1
                blank(text[i:j]); prev = "/"; i = j; continue
        if c == "\n":
            line += 1
        elif c in "([{":
            stack.append((c, line))
        elif c in ")]}":
            if not stack:
                errs.append("第 %d 行：多出一个 %s" % (line, c))
            else:
                o, ol = stack.pop()
                if o != PAIRS[c]:
                    errs.append("第 %d 行：%s 对不上第 %d 行的 %s" % (line, c, ol, o))
        out.append(c)
        if c not in " \t\n":
            prev = c
        i += 1
    for o, ol in stack:
        errs.append("第 %d 行的 %s 没有闭合" % (ol, o))
    return "".join(out)


code = scan(js)

# ── 第二关：用了但没声明 ───────────────────────────────────────────
IDENT = re.compile(r"[A-Za-z_$][\w$]*")
KEYWORDS = set("""
if else for while do break continue function return const let var new typeof
instanceof try catch finally throw switch case default delete void in of class
extends async await yield this true false null undefined
""".split())
GLOBALS = set("""
window document navigator location console alert confirm fetch
setTimeout setInterval clearTimeout clearInterval
Math JSON Date String Number Boolean Array Object Set Map RegExp Promise Error
parseInt parseFloat isNaN isFinite encodeURIComponent decodeURIComponent
sessionStorage localStorage
""".split())

declared = set()
# 函数名
declared |= set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)", code))
# const/let/var，支持 `const a=1,b=2`：取声明语句里深度 0 上 = 或 , 前面那个名字
for m in re.finditer(r"\b(?:const|let|var)\s+", code):
    j, depth = m.end(), 0
    while j < len(code):
        ch = code[j]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif ch == ";" and depth == 0:
            break
        elif ch == "\n" and depth == 0 and code[m.end():j].count("=") > 0 \
                and not code[:j].rstrip().endswith(","):
            break
        j += 1
    seg = code[m.end():j]
    d2 = 0
    for part in re.split(r",(?![^(\[]*[)\]])", seg):
        nm = part.strip().split("=")[0].strip()
        if IDENT.fullmatch(nm):
            declared.add(nm)
        else:                      # 解构 {a,b} / [a,b]
            declared |= set(IDENT.findall(nm))
# 形参 + catch(e)
for pat in (r"\bfunction\s*[A-Za-z_$\w]*\s*\(([^)]*)\)",
            r"\(([^()]*)\)\s*=>",
            r"\bcatch\s*\(([^)]*)\)"):
    for m in re.finditer(pat, code):
        for part in m.group(1).split(","):
            nm = part.strip().split("=")[0].strip()
            declared |= set(IDENT.findall(nm))
# 单参箭头 x=>
declared |= set(re.findall(r"\b([A-Za-z_$][\w$]*)\s*=>", code))
# for(const x of/in y) 已被上面的 const 分支覆盖

used = {}
for m in IDENT.finditer(code):
    name, a, b = m.group(0), m.start(), m.end()
    if code[:a].rstrip().endswith("."):
        continue                                  # obj.foo
    if code[b:].lstrip().startswith(":"):
        continue                                  # 对象键 / label
    if name in declared or name in KEYWORDS or name in GLOBALS:
        continue
    used.setdefault(name, code[:a].count("\n") + 1)

for name, ln in sorted(used.items(), key=lambda kv: kv[1]):
    errs.append("第 %d 行：`%s` 用了但没声明（拼错？还是那处编辑没生效？）" % (ln, name))

print("JS %d 行，问题 %d 处" % (js.count("\n") + 1, len(errs)))
for e in errs[:20]:
    print("  " + e)
sys.exit(1 if errs else 0)
