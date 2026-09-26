#!/usr/bin/env python3
"""修复 clone 根因：musl clone() 包装层对含 CLONE_CHILD_CLEARTID|SETTLS|THREAD 的 flags
直接返回 EINVAL（反汇编确认 libc.a clone() C 层 ccmp+b.eq 路径），导致 guest pthread_create
全灭。改为直接调用底层 __clone（musl pthread 内部同路径，已实测无此检查），弱符号以兼容
glibc 构建（glibc clone() 本身无此坑，回退原调用）。同时带 [CLONERAW] 诊断打点。

用法: patch_clone_raw.py <box64源码目录>
幂等: 检测 [CLONERAW] 标记已存在则跳过。
"""
import sys
import os

MARK = "[CLONERAW]"

# 锚点1: 声明插入位置（include 区）
DECL_ANCHOR = "#include <sched.h>\n"
DECL_TEXT = (
    "#include <sched.h>\n"
    "/* 需直接调底层 __clone: musl 公开 clone() 对线程型 flags(含 CLEARTID|SETTLS|THREAD) 返回 EINVAL */\n"
    "extern int __clone(int (*)(void*), void*, int, void*, ...) __attribute__((weak));\n"
)

# 锚点2: x64Syscall_linux 的新栈分支 clone 调用
OLD1 = (
    "                    flags&=~CLONE_SETTLS;   // to be handled differently\n"
    "                    int64_t ret = clone(clone_fn_syscall, (void*)((uintptr_t)mystack+1024*1024), flags, args, R_RDX, NULL, R_R10);\n"
    "                    S_RAX = ret;\n"
)
NEW1 = (
    "                    flags&=~CLONE_SETTLS;   // to be handled differently\n"
    "                    int64_t ret;\n"
    "                    if(__clone)  // 绕过 musl clone() 包装层的 EINVAL 检查 [CLONERAW]\n"
    "                        ret = __clone(clone_fn_syscall, (void*)((uintptr_t)mystack+1024*1024), flags, args, R_RDX, NULL, R_R10);\n"
    "                    else\n"
    "                        ret = clone(clone_fn_syscall, (void*)((uintptr_t)mystack+1024*1024), flags, args, R_RDX, NULL, R_R10);\n"
    "                    if(ret<0) fprintf(stderr, \"[CLONERAW] newstk ret=%ld errnolike=%ld flags=0x%lx rip=%llx\\n\", (long)ret, (long)-ret, (unsigned long)flags, (unsigned long long)R_RIP);\n"
    "                    S_RAX = ret;\n"
)

# 锚点3: my_syscall 的新栈分支 clone 调用
OLD2 = (
    "                flags &= ~CLONE_SETTLS;   // guest TLS is applied to the emulated FS base in clone_fn_syscall\n"
    "                long ret = clone(clone_fn_syscall, (void*)((uintptr_t)mystack+1024*1024), flags, args, R_RCX, NULL, R_R8);\n"
    "                return ret;\n"
)
NEW2 = (
    "                flags &= ~CLONE_SETTLS;   // guest TLS is applied to the emulated FS base in clone_fn_syscall\n"
    "                long ret;\n"
    "                if(__clone)  // 绕过 musl clone() 包装层的 EINVAL 检查 [CLONERAW]\n"
    "                    ret = __clone(clone_fn_syscall, (void*)((uintptr_t)mystack+1024*1024), flags, args, R_RCX, NULL, R_R8);\n"
    "                else\n"
    "                    ret = clone(clone_fn_syscall, (void*)((uintptr_t)mystack+1024*1024), flags, args, R_RCX, NULL, R_R8);\n"
    "                if(ret<0) fprintf(stderr, \"[CLONERAW] my_syscall ret=%ld flags=0x%lx\\n\", ret, (unsigned long)flags);\n"
    "                return ret;\n"
)


def patch(srcdir: str) -> int:
    path = os.path.join(srcdir, "src", "emu", "x64syscall.c")
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    if MARK in src:
        print(f"patch_clone_raw: 已应用过，跳过 ({path})")
        return 0
    n = 0
    for old, new in ((DECL_ANCHOR, DECL_TEXT), (OLD1, NEW1), (OLD2, NEW2)):
        c = src.count(old)
        if c != 1:
            print(f"patch_clone_raw: 锚点出现 {c} 次（需为1）: {old[:60]!r}")
            return 1
        src = src.replace(old, new, 1)
        n += 1
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print(f"patch_clone_raw: 已应用 {n} 处替换 -> {path}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"用法: {sys.argv[0]} <box64源码目录>")
        sys.exit(2)
    sys.exit(patch(sys.argv[1]))
