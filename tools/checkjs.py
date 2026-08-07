# -*- coding: utf-8 -*-
"""
给 app.py 里内联那段 JS 做括号/引号平衡校验。

这台机器上没有 node，改完页面脚本没法真跑一遍语法检查。这个脚本不做完整解析，
只扫描字符串、模板串、注释、正则字面量，然后看 ( [ { 配不配得上 ——
足够抓住「引号没闭合」「括号少一个」这类会让整页 JS 直接死掉的错。

    python tools\\checkjs.py
"""
import io, os, re, sys

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
src = io.open(APP, encoding="utf-8").read()
js = re.search(r"<script>\n(.*?)</script>", src, re.S).group(1)

BS = chr(92)                     # 反斜杠，避免在本文件里再套一层转义
QUOTES = "'" + '"' + chr(96)     # ' " `
# 出现在这些字符之后的 / 是正则字面量的开头，不是除号
REGEX_OK = set("(,=:[!&|?{};+-*%~^<>") | {"\n", ""}
PAIRS = {")": "(", "]": "[", "}": "{"}

i, n, line = 0, len(js), 1
stack, errs, prev = [], [], ""

while i < n:
    c = js[i]
    if c == "\n":
        line += 1; i += 1; continue
    if c in " \t":
        i += 1; continue
    if js.startswith("//", i):
        j = js.find("\n", i); i = n if j < 0 else j; continue
    if js.startswith("/*", i):
        j = js.find("*/", i + 2)
        line += js.count("\n", i, j if j > 0 else n); i = n if j < 0 else j + 2; continue
    if c in QUOTES:
        q, j, start = c, i + 1, line
        closed = False
        while j < n:
            if js[j] == BS:
                j += 2; continue
            if js[j] == q:
                closed = True; break
            if js[j] == "\n":
                line += 1
                if q != chr(96):
                    errs.append("第 %d 行：%s 引号跨行未闭合" % (start, q)); break
            j += 1
        if not closed and j >= n:
            errs.append("第 %d 行：%s 引号到结尾都没闭合" % (start, q))
        prev = q; i = j + 1; continue
    if c == "/" and prev in REGEX_OK:
        j, incls = i + 1, False
        while j < n and js[j] != "\n":
            if js[j] == BS:
                j += 2; continue
            if js[j] == "[":
                incls = True
            elif js[j] == "]":
                incls = False
            elif js[j] == "/" and not incls:
                break
            j += 1
        if j < n and js[j] == "/":
            prev = "/"; i = j + 1
            while i < n and js[i] in "gimsuy":
                i += 1
            continue
    if c in "([{":
        stack.append((c, line))
    elif c in ")]}":
        if not stack:
            errs.append("第 %d 行：多出一个 %s" % (line, c))
        else:
            o, ol = stack.pop()
            if o != PAIRS[c]:
                errs.append("第 %d 行：%s 对不上第 %d 行的 %s" % (line, c, ol, o))
    prev = c
    i += 1

for o, ol in stack:
    errs.append("第 %d 行的 %s 没有闭合" % (ol, o))

print("JS %d 行，问题 %d 处" % (js.count("\n") + 1, len(errs)))
for e in errs[:15]:
    print("  " + e)
sys.exit(1 if errs else 0)
