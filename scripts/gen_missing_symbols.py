#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 解析 wrappedlibc_private.h，为 STATICBUILD 下所有需 & 地址的符号生成 weak stub。
# 用法: gen_missing_symbols.py <wrappedlibc_private.h 路径> <输出 .c 路径>
import re
import sys

FUNC_MACROS = {"GO", "GOW", "GOD", "GOWD", "GO2", "GOW2", "GOM", "GOWM"}
DATA_MACROS = {"DATA", "DATAV", "DATAB", "DATAM"}

func_symbols = set()   # 需 & 地址的函数符号（N 或 O）
my_func_symbols = set()  # GOM/GOWM 的 my_ 前缀（box64 自备，仅记录不 stub）
data_symbols = {}      # 需 & 地址的数据符号 -> 大小 S
my_data_symbols = set()

def parse(path):
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("//") or line.startswith("#"):
            continue
        m = re.match(r"^(GOM|GOWM|GOW|GO2|GOW2|GOD|GOWD|GO|DATA|DATAV|DATAB|DATAM)\(([^)]*)\)", line)
        if not m:
            continue
        macro = m.group(1)
        args = [a.strip() for a in m.group(2).split(",")]
        if macro in FUNC_MACROS:
            n = args[0]
            if macro in ("GOM", "GOWM"):
                my_func_symbols.add("my_" + n)
            elif macro in ("GO2", "GOW2", "GOD", "GOWD"):
                # 第三个参数是映射目标 O；若只有 2 参（GOD 在无 LD80 时变 GO）取 N
                o = args[2] if len(args) >= 3 else n
                func_symbols.add(o)
            else:
                func_symbols.add(n)
        elif macro in DATA_MACROS:
            n = args[0]
            if macro == "DATAM":
                my_data_symbols.add("my_" + n)
            else:
                data_symbols[n] = args[1] if len(args) >= 2 else "8"
    return

def gen(out_path):
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("/* 自动生成：为 musl 缺失的 glibc 符号提供 weak stub */\n")
        f.write("/* 弱符号：musl libc 有强定义则被覆盖，缺失则提供地址 */\n")
        f.write("#include <stddef.h>\n")
        f.write("#include <stdint.h>\n\n")
        for s in sorted(func_symbols):
            f.write("__attribute__((weak)) void %s(void) { }\n" % s)
        f.write("\n")
        for s in sorted(data_symbols):
            f.write("__attribute__((weak)) unsigned char %s[%s] = {0};\n" % (s, data_symbols[s]))
        f.write("\n")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("用法: %s <private.h> <输出.c>" % sys.argv[0])
        sys.exit(1)
    parse(sys.argv[1])
    gen(sys.argv[2])
    print("函数符号 %d 个, my_函数 %d 个, 数据符号 %d 个, my_数据 %d 个" %
          (len(func_symbols), len(my_func_symbols), len(data_symbols), len(my_data_symbols)))