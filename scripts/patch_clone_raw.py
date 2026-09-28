#!/usr/bin/env python3
"""修复 clone 根因：musl clone() 包装层对含 CLONE_CHILD_CLEARTID|SETTLS|THREAD 的 flags
直接返回 EINVAL（反汇编确认 libc.a clone() C 层 ccmp+b.eq 路径），导致 guest pthread_create
全灭。改为直接调用底层 __clone（musl pthread 内部同路径，已实测无此检查），弱符号以兼容
glibc 构建（glibc clone() 本身无此坑，回退原调用）。

批次3 #8：锚点正则化——多行精确串（缩进/注释/变量名漂移必断）改为正则锚，
仍严格要求 count==1 唯一命中，失败即报错退出。
批次3 #9：幂等标记统一英文哨兵 BOX64-BUILD: clone-raw（不再依赖中文注释字符串）。

用法: patch_clone_raw.py <box64源码目录>
"""
import sys
import os
import re

SENTINEL = "BOX64-BUILD: clone-raw"

# 锚点1: 声明插入位置（include 区，x64syscall.c 中 #include <sched.h> 唯一）
DECL_RE = re.compile(r"#include <sched\.h>\n")
DECL_TEXT = (
    "#include <sched.h>\n"
    "/* 需直接调底层 __clone: musl 公开 clone() 对线程型 flags(含 CLEARTID|SETTLS|THREAD) 返回 EINVAL */\n"
    "extern int __clone(int (*)(void*), void*, int, void*, ...) __attribute__((weak));  // " + SENTINEL + "\n"
)

# 锚点2: x64Syscall_linux 的新栈分支 clone 调用（R_RDX/R_R10 路径唯一）
OLD1_RE = re.compile(
    r"([ \t]*)flags&=~CLONE_SETTLS;[ \t]*// to be handled differently\n"
    r"([ \t]*)int64_t ret = clone\(clone_fn_syscall, \(void\*\)\(\(uintptr_t\)mystack\+1024\*1024\), flags, args, R_RDX, NULL, R_R10\);\n"
    r"([ \t]*)S_RAX = ret;\n"
)
NEW1 = (
    "{0}flags&=~CLONE_SETTLS;   // to be handled differently\n"
    "{1}int64_t ret;\n"
    "{1}if(__clone)  // " + SENTINEL + " 绕过 musl clone() 包装层的 EINVAL 检查\n"
    "{1}    ret = __clone(clone_fn_syscall, (void*)((uintptr_t)mystack+1024*1024), flags, args, R_RDX, NULL, R_R10);\n"
    "{1}else\n"
    "{1}    ret = clone(clone_fn_syscall, (void*)((uintptr_t)mystack+1024*1024), flags, args, R_RDX, NULL, R_R10);\n"
    "{2}S_RAX = ret;\n"
)

# 锚点3: my_syscall 的新栈分支 clone 调用（R_RCX/R_R8 路径唯一）
OLD2_RE = re.compile(
    r"([ \t]*)flags &= ~CLONE_SETTLS;[ \t]*// guest TLS is applied to the emulated FS base in clone_fn_syscall\n"
    r"([ \t]*)long ret = clone\(clone_fn_syscall, \(void\*\)\(\(uintptr_t\)mystack\+1024\*1024\), flags, args, R_RCX, NULL, R_R8\);\n"
    r"([ \t]*)return ret;\n"
)
NEW2 = (
    "{0}flags &= ~CLONE_SETTLS;   // guest TLS is applied to the emulated FS base in clone_fn_syscall\n"
    "{1}long ret;\n"
    "{1}if(__clone)  // " + SENTINEL + " 绕过 musl clone() 包装层的 EINVAL 检查\n"
    "{1}    ret = __clone(clone_fn_syscall, (void*)((uintptr_t)mystack+1024*1024), flags, args, R_RCX, NULL, R_R8);\n"
    "{1}else\n"
    "{1}    ret = clone(clone_fn_syscall, (void*)((uintptr_t)mystack+1024*1024), flags, args, R_RCX, NULL, R_R8);\n"
    "{2}return ret;\n"
)


def fail(msg: str) -> int:
    print(f"patch_clone_raw: {msg}", file=sys.stderr)
    return 1


def sub1(src: str, pattern: re.Pattern, repl: str, name: str):
    """正则锚唯一命中后替换；命中数 != 1 报错。"""
    hits = list(pattern.finditer(src))
    if len(hits) != 1:
        return None, fail(
            f"锚点 [{name}] 命中 {len(hits)} 次（需为 1）: {pattern.pattern[:70]!r}"
        )
    m = hits[0]
    text = repl.format(*m.groups()) if m.groups() else repl
    return src[: m.start()] + text + src[m.end() :], 0


def patch(srcdir: str) -> int:
    path = os.path.join(srcdir, "src", "emu", "x64syscall.c")
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    if SENTINEL in src:
        print(f"patch_clone_raw: 已应用过，跳过 ({path})")
        return 0
    for pattern, repl, name in (
        (DECL_RE, DECL_TEXT, "decl"),
        (OLD1_RE, NEW1, "x64Syscall clone"),
        (OLD2_RE, NEW2, "my_syscall clone"),
    ):
        src, rc = sub1(src, pattern, repl, name)
        if rc != 0:
            return rc
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print(f"patch_clone_raw: 已应用 3 处正则替换 -> {path}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"用法: {sys.argv[0]} <box64源码目录>")
        sys.exit(2)
    sys.exit(patch(sys.argv[1]))
