#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
临时打点 patch（B-12 waker 定位实验，结论得出后应从挂链移除）：
在 x64Syscall_linux 入口打印全部 futex / clone / clone3 调用，
用于对比 STUCK 轮与 LOGIN 轮的唤醒链（谁 post、是否有 WAKE 发出、clone 爆发点）。
哨兵：BOX64-BUILD: p12dbg
用法：python3 patch_p12dbg.py <box64源码目录>
"""
import sys
import os

SENTINEL = "BOX64-BUILD: p12dbg"

def fail(msg):
    print(f"[patch_p12dbg] 失败: {msg}", file=sys.stderr)
    sys.exit(1)

def main():
    if len(sys.argv) != 2:
        fail("用法: patch_p12dbg.py <box64目录>")
    target = os.path.join(sys.argv[1], "src", "emu", "x64syscall.c")
    if not os.path.isfile(target):
        fail(f"文件不存在: {target}")
    with open(target, "r", encoding="utf-8") as f:
        src = f.read()
    if SENTINEL in src:
        print("[patch_p12dbg] 已应用过，跳过")
        return 0

    # 锚：x64Syscall_linux 内 s/log 开头两行（全文件唯一；
    # 不能含 RESET_FLAGS 行——isnanf 的 restore-lin 会插在其后断锚）
    anchor = (
        "    uint32_t s = R_EAX; // EAX? (syscalls only go up to 547 anyways)\n"
        "    int log = 0;\n"
    )
    n = src.count(anchor)
    if n != 1:
        fail(f"锚点命中 {n} 次（期望 1）")
    injection = (
        "    // BOX64-BUILD: p12dbg 临时打点：futex/clone 全踪（B-12 唤醒链定位）\n"
        "    if(s == 202) {\n"
        "        printf_log(LOG_NONE, \"P12FUTEX tid=%d addr=%p op=%u arg=%d r8=%d\\n\",\n"
        "            GetTID(), (void*)R_RDI, (unsigned)R_RSI, (int)R_RDX, (int)R_R10);\n"
        "    } else if(s == 56 || s == 435) {\n"
        "        printf_log(LOG_NONE, \"P12CLONE tid=%d s=%d flag=%u\\n\",\n"
        "            GetTID(), s, (unsigned)R_RDI);\n"
        "    }\n"
    )
    src = src.replace(anchor, anchor + injection, 1)
    with open(target, "w", encoding="utf-8") as f:
        f.write(src)
    print("[patch_p12dbg] 已应用 1 处")
    return 0

if __name__ == "__main__":
    sys.exit(main())
