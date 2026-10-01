#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BOX64_PATHMAP 路径映射 patch：guest 路径前缀重写（通用 GNU 程序适配）。

背景：box64 翻译指令流而非库文件，x86 guest 程序在 Android/proot 宿主上
运行时路径前缀不通用（如 guest 访问 /tmp 但宿主实际要落到 $PREFIX/../tmp）。
本 patch 在 syscall 分发层与 libc wrapper 层做前缀重写，支持 env：

    BOX64_PATHMAP="/from:/to,/from2:/to2"

语义：前缀匹配（边界=串尾或'/'），最长前缀优先；每条路径单次映射
（映射结果不再二次映射，防止链式规则导致双重改写）；未命中返回原指针。

注入（双层）：
  layer1 syscall 路：x64Syscall_linux / my_syscall（x64syscall.c）、
    x86Syscall（x86syscall.c 与 x86syscall_32.c 两份）入口按 nr 查
    pathmask 表后重写对应参数寄存器，下游 wrap 直透与 switch case
    自动生效。注意 my_syscall 是 libc syscall() 包装：参数从 R_RSI
    起（号在 R_EDI），与 x64Syscall_linux（号在 R_EAX，参数 R_RDI 起）
    寄存器视图不同，分两段。
  layer2 libc 路：wrappedlibc.c 的 my_* path wrapper 内部重写
    （64 位与 box32 的 GOM 汇入同一 my_* 实现）。
新建：src/include/pathmap.h、src/libtools/pathmap.c（CMake ELFLOADER_SRC）。

哨兵：BOX64-BUILD: pathmap
用法: patch_pathmap.py <box64源码目录>
"""
import os
import sys

SENTINEL = "BOX64-BUILD: pathmap"


def fail(msg):
    print(f"[patch_pathmap] 失败: {msg}", file=sys.stderr)
    sys.exit(1)


def apply_file(path, jobs, must_exist=True):
    if not os.path.isfile(path):
        if must_exist:
            fail(f"文件不存在: {path}")
        print(f"[patch_pathmap] 跳过缺失文件: {path}")
        return
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    if SENTINEL in src:
        print(f"[patch_pathmap] {os.path.basename(path)} 已应用过，跳过")
        return
    for anchor, repl in jobs:
        n = src.count(anchor)
        if n != 1:
            fail(f"{os.path.basename(path)} 锚点命中 {n} 次（期望 1）:\n{anchor[:120]}")
        src = src.replace(anchor, repl, 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print(f"[patch_pathmap] {os.path.basename(path)} 应用 {len(jobs)} 处")


PATHMAP_H = """#ifndef BOX64_PATHMAP_H
#define BOX64_PATHMAP_H
#include <stdint.h>
/* BOX64-BUILD: pathmap 前缀映射：未命中返回原指针，命中返回 thread 缓冲 */
const char* box64_pathmap(const char* path);
/* 逻辑参数位掩码：bit0=arg0 ... bit5=arg5；0=非 path 类 syscall */
uint8_t box64_pathmask64(uint32_t nr);
uint8_t box64_pathmask32(uint32_t nr);
#endif
"""

PATHMAP_C = r"""// SPDX-License-Identifier: MIT
// BOX64-BUILD: pathmap —— BOX64_PATHMAP 路径前缀映射实现
#define _GNU_SOURCE
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include "pathmap.h"

#define PM_MAX 8
#define PM_FLEN 256
#define PM_TLEN 256
#define PM_BUFSZ 2048

typedef struct {
    char from[PM_FLEN];
    char to[PM_TLEN];
} pm_ent_t;

static pm_ent_t pm_tbl[PM_MAX];
/* -1=未解析, 0=无映射, >0=条目数；volatile 供无锁快路径 */
static volatile int pm_n = -1;
static pthread_mutex_t pm_lock = PTHREAD_MUTEX_INITIALIZER;

static size_t pm_trim(const char* s, size_t len)
{
    /* 去首尾空格（不移动指针，只缩 len；首空格由调用方偏移处理不了，
       这里只裁尾部空格，头部空格在复制前跳过） */
    while (len && (s[len - 1] == ' ' || s[len - 1] == '\t'))
        --len;
    return len;
}

static void pm_parse(const char* env)
{
    int n = 0;
    const char* p = env;
    while (*p && n < PM_MAX) {
        const char* comma = strchr(p, ',');
        const char* end = comma ? comma : p + strlen(p);
        const char* colon = (const char*)memchr(p, ':', (size_t)(end - p));
        if (colon && colon > p) {
            const char* fp = p;
            size_t fl = (size_t)(colon - fp);
            while (fl && (*fp == ' ' || *fp == '\t')) { ++fp; --fl; }
            fl = pm_trim(fp, fl);
            const char* tp = colon + 1;
            size_t tl = (size_t)(end - tp);
            tl = pm_trim(tp, tl);
            if (fl >= PM_FLEN) fl = PM_FLEN - 1;
            if (tl >= PM_TLEN) tl = PM_TLEN - 1;
            /* from 去尾斜杠（保留根 "/"） */
            while (fl > 1 && fp[fl - 1] == '/')
                --fl;
            if (fl && tl) {
                memcpy(pm_tbl[n].from, fp, fl);
                pm_tbl[n].from[fl] = 0;
                memcpy(pm_tbl[n].to, tp, tl);
                pm_tbl[n].to[tl] = 0;
                ++n;
            }
        }
        if (!comma)
            break;
        p = comma + 1;
    }
    pm_n = n;
}

const char* box64_pathmap(const char* path)
{
    if (!path)
        return path;
    if (pm_n == 0)
        return path;    // 快路径：未配置 BOX64_PATHMAP
    pthread_mutex_lock(&pm_lock);
    if (pm_n < 0) {
        const char* env = getenv("BOX64_PATHMAP");
        if (env && *env)
            pm_parse(env);
        else
            pm_n = 0;
    }
    int best = -1;
    size_t bestlen = 0;
    for (int i = 0; i < pm_n; ++i) {
        size_t l = strlen(pm_tbl[i].from);
        if (l == 0 || l < bestlen)
            continue;
        if (strncmp(path, pm_tbl[i].from, l) != 0)
            continue;
        /* 前缀边界：串尾或 '/'，防止 /tmpx 命中 /tmp */
        if (path[l] != '\0' && path[l] != '/')
            continue;
        best = i;
        bestlen = l;
    }
    if (best < 0) {
        pthread_mutex_unlock(&pm_lock);
        return path;
    }
    static __thread char buf[PM_BUFSZ];
    const char* rest = path + bestlen;
    size_t need = strlen(pm_tbl[best].to) + strlen(rest);
    if (need >= sizeof(buf)) {
        pthread_mutex_unlock(&pm_lock);
        return path;    // 超长降级：不映射
    }
    strcpy(buf, pm_tbl[best].to);
    strcat(buf, rest);
    pthread_mutex_unlock(&pm_lock);
    // 单次映射：结果不再二次匹配（防链式规则双重改写）
    return buf;
}

uint8_t box64_pathmask64(uint32_t nr)
{
    static const struct { uint32_t nr; uint8_t pm; } t[] = {
        {2, 1},        // open(path,...)
        {4, 1},        // stat(path, buf)
        {6, 1},        // lstat(path, buf)
        {21, 1},       // access(path, mode)
        {59, 1},       // execve(path, argv, envp)
        {76, 1},       // truncate(path, len)
        {80, 1},       // chdir(path)
        {82, 3},       // rename(old, new)
        {83, 1},       // mkdir(path, mode)
        {84, 1},       // rmdir(path)
        {86, 3},       // link(old, new)
        {87, 1},       // unlink(path)
        {88, 3},       // symlink(target, linkpath)
        {89, 1},       // readlink(path, buf, sz)
        {90, 1},       // chmod(path, mode)
        {92, 1},       // chown(path, uid, gid)
        {94, 1},       // lchown(path, uid, gid)
        {133, 1},      // mknod(path, mode, dev)
        {137, 1},      // statfs(path, buf)
        {161, 1},      // chroot(path)
        {235, 1},      // utimes(path, times)
        {254, 1},      // inotify_add_watch(fd, path, mask)
        {257, 2},      // openat(dirfd, path, flags, mode)
        {258, 2},      // mkdirat(dirfd, path, mode)
        {259, 2},      // mknodat(dirfd, path, mode, dev)
        {260, 2},      // fchownat(dirfd, path, uid, gid, flags)
        {261, 2},      // futimesat(dirfd, path, times)
        {262, 2},      // newfstatat(dirfd, path, buf, flags)
        {263, 2},      // unlinkat(dirfd, path, flags)
        {264, 10},     // renameat(olddirfd, oldpath, newdirfd, newpath)
        {265, 10},     // linkat(olddirfd, oldpath, newdirfd, newpath, flags)
        {266, 5},      // symlinkat(target, newdirfd, linkpath) arg0+arg2
        {267, 2},      // readlinkat(dirfd, path, buf, sz)
        {268, 2},      // fchmodat(dirfd, path, mode, flags)
        {269, 2},      // faccessat(dirfd, path, mode, flags)
        {280, 2},      // utimensat(dirfd, path, times, flags)
        {316, 10},     // renameat2 与 renameat 同布局
        {322, 2},      // execveat(dirfd, path, argv, envp, flags)
        {332, 2},      // statx(dirfd, path, flags, mask, buf)
        {439, 2},      // faccessat2(dirfd, path, mode, flags)
    };
    for (size_t i = 0; i < sizeof(t) / sizeof(t[0]); ++i)
        if (t[i].nr == nr)
            return t[i].pm;
    return 0;
}

uint8_t box64_pathmask32(uint32_t nr)
{
    // i386 syscall nr（与 x86_64 不同：open=5/stat=106/openat=295 ...）
    static const struct { uint32_t nr; uint8_t pm; } t[] = {
        {5, 1},        // open
        {9, 3},        // link(old, new)
        {10, 1},       // unlink
        {11, 1},       // execve
        {12, 1},       // chdir
        {14, 1},       // mknod
        {15, 1},       // chmod
        {16, 1},       // lchown
        {18, 1},       // chown
        {30, 1},       // utime
        {33, 1},       // access
        {38, 3},       // rename
        {39, 1},       // mkdir
        {40, 1},       // rmdir
        {61, 1},       // chroot
        {83, 3},       // symlink
        {85, 1},       // readlink
        {92, 1},       // truncate
        {99, 1},       // statfs
        {106, 1},      // stat
        {107, 1},      // lstat
        {271, 1},      // utimes
        {292, 1},      // inotify_add_watch
        {295, 2},      // openat
        {296, 2},      // mkdirat
        {297, 2},      // fchownat
        {298, 2},      // futimesat
        {299, 2},      // newfstatat
        {301, 2},      // unlinkat
        {302, 10},     // renameat
        {303, 10},     // linkat
        {304, 5},      // symlinkat target+arg2
        {307, 2},      // readlinkat
        {308, 2},      // fchmodat
        {309, 2},      // faccessat
        {311, 2},      // utimensat
        {355, 10},     // renameat2
        {358, 2},      // execveat
        {383, 2},      // statx
    };
    for (size_t i = 0; i < sizeof(t) / sizeof(t[0]); ++i)
        if (t[i].nr == nr)
            return t[i].pm;
    return 0;
}
"""

# ---- layer1 段（六位寄存器逐位重写；不同分发寄存器视图不同） ----
def l1_64(sig, r0, r1, r2, r3, r4, r5):
    lines = [f"    // {SENTINEL} (layer1-{sig})",
             "    {",
             "        uint8_t pm = box64_pathmask64(s);",
             "        if(pm) {"]
    for i, r in enumerate((r0, r1, r2, r3, r4, r5)):
        if r is None:
            continue
        lines.append(f"            if(pm&{1 << i}) {r} = (uintptr_t)box64_pathmap((const char*){r});")
    lines += ["        }", "    }", ""]
    return "\n".join(lines)


def l1_32(sig):
    lines = [f"    // {SENTINEL} (layer1-{sig})",
             "    {",
             "        uint8_t pm = box64_pathmask32(s);",
             "        if(pm) {"]
    for i, r in enumerate(("R_EBX", "R_ECX", "R_EDX", "R_ESI", "R_EDI", "R_EBP")):
        lines.append(f"            if(pm&{1 << i}) {r} = (uint32_t)(uintptr_t)box64_pathmap((const char*)(uintptr_t){r});")
    lines += ["        }", "    }", ""]
    return "\n".join(lines)


def main():
    if len(sys.argv) != 2:
        fail("用法: patch_pathmap.py <box64目录>")
    root = sys.argv[1]

    # ---- 新建 pathmap.h / pathmap.c ----
    incdir = os.path.join(root, "src", "include")
    libdir = os.path.join(root, "src", "libtools")
    os.makedirs(incdir, exist_ok=True)
    os.makedirs(libdir, exist_ok=True)
    for rel, content in (
        ("src/include/pathmap.h", PATHMAP_H),
        ("src/libtools/pathmap.c", PATHMAP_C),
    ):
        path = os.path.join(root, rel)
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                if SENTINEL in f.read():
                    print(f"[patch_pathmap] {rel} 已存在，跳过")
                    continue
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"[patch_pathmap] 创建 {rel}")

    # ---- CMakeLists.txt：ELFLOADER_SRC 加 pathmap.c（插行带哨兵保证幂等） ----
    cmake = os.path.join(root, "CMakeLists.txt")
    apply_file(cmake, [(
        '    "${BOX64_ROOT}/src/emu/x64syscall.c"\n',
        '    "${BOX64_ROOT}/src/emu/x64syscall.c"\n'
        '    "${BOX64_ROOT}/src/libtools/pathmap.c"  # BOX64-BUILD: pathmap\n',
    )])

    # ---- x64syscall.c：include + 两个入口 layer1 ----
    apply_file(os.path.join(root, "src", "emu", "x64syscall.c"), [
        ('#include "cpumask.h"\n',
         '#include "cpumask.h"\n#include "pathmap.h"\n'),
        # x64Syscall_linux：号在 R_EAX，参数 R_RDI 起，arg3=R_R10
        ("    uint32_t s = R_EAX; // EAX? (syscalls only go up to 547 anyways)\n",
         "    uint32_t s = R_EAX; // EAX? (syscalls only go up to 547 anyways)\n"
         + l1_64("x64linux", "R_RDI", "R_RSI", "R_RDX", "R_R10", "R_R8", "R_R9")),
        # my_syscall：libc syscall() 包装，号在 R_EDI，参数从 R_RSI 起
        ("    static uint32_t warned = 0;\n    uint32_t s = R_EDI;\n",
         "    static uint32_t warned = 0;\n    uint32_t s = R_EDI;\n"
         + l1_64("mysys", "R_RSI", "R_RDX", "R_RCX", "R_R8", "R_R9", None)),
    ])

    # ---- x86syscall_32.c：include + x86Syscall layer1（i386 ABI R_EBX..） ----
    apply_file(os.path.join(root, "src", "emu", "x86syscall_32.c"), [
        ('#include "syscall_user_dispatch.h"\n',
         '#include "syscall_user_dispatch.h"\n#include "pathmap.h"\n'),
        ('    printf_log(LOG_DEBUG, "%04d|%p: Calling 32bits syscall 0x%02X (%d) %p %p %p %p %p", GetTID()',
         l1_32("x86_32") +
         '    printf_log(LOG_DEBUG, "%04d|%p: Calling 32bits syscall 0x%02X (%d) %p %p %p %p %p", GetTID()'),
    ])

    # ---- x86syscall.c：include + x86Syscall layer1 ----
    apply_file(os.path.join(root, "src", "emu", "x86syscall.c"), [
        ('#include "syscall_user_dispatch.h"\n',
         '#include "syscall_user_dispatch.h"\n#include "pathmap.h"\n'),
        ('    printf_log(LOG_DEBUG, "%p: Calling 32bits syscall 0x%02X (%d) %p %p %p %p %p", (void*)R_RIP',
         l1_32("x86") +
         '    printf_log(LOG_DEBUG, "%p: Calling 32bits syscall 0x%02X (%d) %p %p %p %p %p", (void*)R_RIP'),
    ])

    # ---- wrappedlibc.c：include + layer2 my_* wrapper ----
    V = "(void*)"          # void* 参数形态
    C = "(const char*)"    # const char* 参数形态
    l2 = []

    def w2(anchor_head, assign, extra_old, extra_new=""):
        # anchor_head: 函数签名+首行；在 body 首部插入映射
        l2.append((anchor_head + extra_old, anchor_head + assign + extra_new))

    # void* filename/path 形态：filename = (void*)box64_pathmap(...)
    def cast_v(name):
        return f"    {name} = (void*)box64_pathmap((const char*){name});\n"

    def cast_c(name):
        return f"    {name} = box64_pathmap({name});\n"

    w2('EXPORT int my_stat(x64emu_t *emu, void* filename, void* buf)\n{\n',
       cast_v("filename"), "    (void)emu;\n")
    w2('EXPORT int my_lstat(x64emu_t *emu, void* filename, void* buf)\n{\n',
       cast_v("filename"), "    (void)emu;\n")
    w2('EXPORT int my_fstatat(x64emu_t *emu, int fd, const char* path, void* buf, int flags)\n{\n',
       cast_c("path"), "    (void)emu;\n")
    w2('EXPORT int my_statx(x64emu_t* emu, int dirfd, void* path, int flags, uint32_t mask, void* buf)\n{\n',
       cast_v("path"), "")
    w2('EXPORT ssize_t my_readlink(x64emu_t* emu, void* path, void* buf, size_t sz)\n{\n',
       cast_v("path"), "")
    w2('EXPORT int my_readlinkat(x64emu_t* emu, int fd, void* path, void* buf, size_t bufsize)\n{\n',
       cast_v("path"), "")
    w2('EXPORT FILE* my_fopen64(x64emu_t* emu, const char* path, const char* mode)\n{\n',
       cast_c("path"), "")
    w2('EXPORT int32_t my_execv(x64emu_t* emu, const char* path, char* const argv[])\n{\n',
       cast_c("path"), "")
    w2('EXPORT int32_t my_execve(x64emu_t* emu, const char* path, char* const argv[], char* const envp[])\n{\n',
       cast_c("path"), "")
    w2('EXPORT int32_t my_execvp(x64emu_t* emu, const char* path, char* const argv[])\n{\n',
       cast_c("path"), "")
    w2('EXPORT int32_t my_execvpe(x64emu_t* emu, const char* path, char* argv[], char* const envp[])\n{\n',
       cast_c("path"), "")
    w2('EXPORT void* my_realpath(x64emu_t* emu, void* path, void* resolved_path)\n{\n',
       cast_v("path"), "")
    w2('EXPORT int my_statfs64(const char* path, void* buf)\n{\n',
       cast_c("path"), "")
    # my_open：NULL 检查之后再映射（保留 EFAULT 语义）
    w2('EXPORT int32_t my_open(x64emu_t* emu, void* pathname, int32_t flags, uint32_t mode)\n{\n',
       cast_v("pathname"),
       "    if(!pathname) {\n        errno = EFAULT;\n        return -1;\n    }\n")
    # my_renameat2：新旧两个路径都映射
    w2('EXPORT int my_renameat2(int olddirfd, void* oldpath, int newdirfd, void* newpath, uint32_t flags)\n{\n',
       cast_v("oldpath") + cast_v("newpath"), "")

    # ---- wrappedlibc.c：include 必须在文件顶部（layer2 注入点在 include 区之后） ----
    apply_file(os.path.join(root, "src", "wrapped", "wrappedlibc.c"),
               [('#include "globalsymbols.h"\n',
                 '#include "globalsymbols.h"\n#include "pathmap.h"  // BOX64-BUILD: pathmap\n')] + l2)

    print("[patch_pathmap] 完成")


if __name__ == "__main__":
    main()
