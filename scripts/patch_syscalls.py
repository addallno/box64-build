#!/usr/bin/env python3
"""向 box64 syscallwrap 转发表补缺失项（sendmmsg/shm 家族）。

背景：musl 静态/静态 glibc 程序直接发 syscall，x64syscall.c 的
syscallwrap[] 缺 [307]=sendmmsg 时 DNS(glibc res) 失败报
"Temporary failure in name resolution"；shmget 家族同理。
x86_64 与 aarch64 在 64 位通用 ABI 下 mmsghdr/msghdr/shmid_ds 布局
一致，参数可直接转发。
"""
import pathlib
import sys

ROOT = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(".")
TARGET = ROOT / "src/emu/x64syscall.c"

# 锚点：网络簇稳定行；插入项按 syscall 号排列无关紧要（designated initializer）
ANCHOR = "[47] = {__NR_recvmsg, 3},"
ADD = """[29] = {__NR_shmget, 3},
    [30] = {__NR_shmat, 3},
    [31] = {__NR_shmctl, 3},
    [67] = {__NR_shmdt, 1},
    [307] = {__NR_sendmmsg, 4},
    #ifdef __NR_recvmmsg
    [308] = {__NR_recvmmsg, 5},
    #endif"""

SENDMMSG_MARK = "[307] = {__NR_sendmmsg"
SENDMMSG_LINE = "[307] = {__NR_sendmmsg, 4},"
RECVMMARK = "[308] = {__NR_recvmmsg"
RECVADD = ("\n    #ifdef __NR_recvmmsg\n"
           "    [308] = {__NR_recvmmsg, 5},\n"
           "    #endif")


def main() -> int:
    src = TARGET.read_text(encoding="utf-8")
    if SENDMMSG_MARK in src and RECVMMARK in src:
        print("patch_syscalls: 已包含 sendmmsg/recvmmsg，跳过")
        return 0
    # 老版 patch（仅 sendmmsg）已打过：单独补 recvmmsg(308)
    if SENDMMSG_MARK in src:
        c = src.count(SENDMMSG_LINE)
        if c != 1:
            print(f"patch_syscalls: sendmmsg 整行锚点出现 {c} 次（需为1）", file=sys.stderr)
            return 1
        src = src.replace(SENDMMSG_LINE, SENDMMSG_LINE + RECVADD, 1)
        TARGET.write_text(src, encoding="utf-8")
        print("patch_syscalls: OK（补 +recvmmsg）")
        return 0
    if ANCHOR not in src:
        print(f"patch_syscalls: 锚点未找到: {ANCHOR}", file=sys.stderr)
        return 1
    src = src.replace(ANCHOR, ANCHOR + "\n    " + ADD, 1)
    TARGET.write_text(src, encoding="utf-8")
    print("patch_syscalls: OK（+shmget/shmat/shmctl/shmdt/sendmmsg/recvmmsg）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
