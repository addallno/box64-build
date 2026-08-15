#!/usr/bin/env python3
"""给 box64 源码打补丁：musl 无 isnanf/isinff（glibc 扩展），替换为 C99 isnan/isinf 宏。
C99 的 isnan(x)/isinf(x) 宏按 sizeof 自动适配 float/double/long double，语义等价。"""

REPL = {  # 优先匹配更长的 f 变体
    "isnanf(": "isnan(",
    "isinff(": "isinf(",
}

# musl 无 glibc 的 mallopt/M_ARENA_*，从 os_linux.c 移除该调优块
MALLOPT_MARKER = "// setting 32bits malloc options"


def patch_text(s: str):
    """按 dict 键长降序替换，避免 isnanf 被 isnan 前缀干扰（用正则确保整词边界）。"""
    pattern = re.compile(r"\b(" + "|".join(re.escape(k) for k in REPL) + r")")
    return pattern.sub(lambda m: REPL[m.group(1)], s)


def remove_mallopt_block(s: str):
    """仅注释 os_linux.c 的 mallopt 相关行（musl 无此 glibc 接口），不触碰 #ifndef/#endif 结构。"""
    lines = s.split("\n")
    out = []
    removed = 0
    for line in lines:
        if MALLOPT_MARKER in line:
            removed += 1
            out.append("    // " + line + " (musl 无此 glibc 接口, patch)")
            continue
        if "mallopt(" in line:
            removed += 1
            out.append("    // " + line)
            continue
        out.append(line)
    return "\n".join(out), removed
import os
import re
import sys

root = sys.argv[1]
total = 0
for dirpath, _dirs, files in os.walk(os.path.join(root, "src")):
    for fn in files:
        if not fn.endswith(".c") and not fn.endswith(".h"):
            continue
        path = os.path.join(dirpath, fn)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            s = f.read()
        new = patch_text(s)
        n = sum(s.count(k) for k in REPL)
        if fn == "os_linux.c":
            new, m = remove_mallopt_block(new)
            n += m
        if n:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new)
            total += n
            print(f"{path}: {n} 处")
print(f"共替换 {total} 处")
if not total:
    print("警告: 未找到需要替换的 glibc 浮点宏")
