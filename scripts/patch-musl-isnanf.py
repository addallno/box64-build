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
    """移除 os_linux.c 中被 #ifndef ANDROID 包裹的 mallopt 调用块。"""
    marker = s.find(MALLOPT_MARKER)
    if marker == -1:
        return s
    start = s.rfind("    ", 0, marker)
    if start == -1:
        start = marker
    # 找到后续第一个 '}\n' 结尾（该 block 是 4 空格缩进的 mallopt 三连）
    end = marker
    for _ in range(6):
        nl = s.find("\n", end)
        if nl == -1:
            break
        end = nl + 1
    return s[:start] + s[end:]
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
        if fn == "os_linux.c":
            new = remove_mallopt_block(new)
        n = sum(s.count(k) for k in REPL)
        if MALLOPT_MARKER in s:
            n += 1
        if n:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new)
            total += n
            print(f"{path}: {n} 处")
print(f"共替换 {total} 处")
if not total:
    print("警告: 未找到需要替换的 glibc 浮点宏")
