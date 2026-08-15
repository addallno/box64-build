#!/usr/bin/env python3
"""给 box64 源码打补丁：musl 无 isnanf（glibc 扩展），替换为 C99 isnan 宏。
isnanf(x) 语义 = x 是 NaN；musl 的 isnan 宏按 sizeof 自动适配 float/double/long double，等价。"""
import os
import re
import sys

root = sys.argv[1]
count = 0
for dirpath, _dirs, files in os.walk(os.path.join(root, "src")):
    for fn in files:
        if not fn.endswith(".c") and not fn.endswith(".h"):
            continue
        path = os.path.join(dirpath, fn)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            s = f.read()
        n = s.count("isnanf(")
        if n:
            s = s.replace("isnanf(", "isnan(")
            with open(path, "w", encoding="utf-8") as f:
                f.write(s)
            count += n
            print(f"{path}: {n} 处")
print(f"共替换 {count} 处 isnanf( -> isnan(")
