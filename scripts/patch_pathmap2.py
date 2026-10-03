#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pathmap2：补齐 wrapped 直通函数的 BOX64_PATHMAP 映射（增量，独立哨兵）。

背景：动态 guest（如 steamcmd 的 breakpad）调 access/__xstat64/mkdir 时，
- __xstat 系列虽是 GOM(my_ 实现) 但实现体内未插 pathmap；
- mkdir/access 是 GOW/GO native 直通，完全绕过映射。
两者在无 /tmp 的宿主（Termux）上全 ENOENT → breakpad Fatal assert。

改动：
  wrappedlibc.c：
    1) my___xstat / my___lxstat / my___fxstatat 函数体补 box64_pathmap；
    2) 新增 my_mkdir / my_access（box64_pathmap + 转发宿主 libc）。
  wrappedlibc_private.h：
    GOW(access, iFpi) → GOWM(access, iFEpi)
    GOW(mkdir,  iFpu) → GOWM(mkdir,  iFEpu)
    （iFEpi/iFEpu typedef 在 generated/wrapper.c 与 wrapper32.h 均已存在）
    mkdirat 不改：iFEipu 无 typedef，且 glibc mkdir 走独立 syscall 不转 mkdirat。

哨兵：BOX64-BUILD: pathmap2
用法: patch_pathmap2.py <box64源码目录>
"""
import os
import sys

SENTINEL = "BOX64-BUILD: pathmap2"

NEW_FUNCS = (
    "// BOX64-BUILD: pathmap2 mkdir/access 路径映射（原为 native 直通）\n"
    "EXPORT int my_mkdir(x64emu_t* emu, const char* path, mode_t mode)\n"
    "{\n"
    "    (void)emu;\n"
    "    return mkdir((const char*)box64_pathmap(path), mode);\n"
    "}\n"
    "EXPORT int my_access(x64emu_t* emu, const char* path, int mode)\n"
    "{\n"
    "    (void)emu;\n"
    "    return access((const char*)box64_pathmap(path), mode);\n"
    "}\n"
)


def fail(msg):
    print(f"[patch_pathmap2] 失败: {msg}", file=sys.stderr)
    sys.exit(1)


def apply(src, old, new, want, name, fname):
    n = src.count(old)
    if n != want:
        fail(f"{fname} 锚点 [{name}] 命中 {n} 次（期望 {want}）: {old[:80]!r}")
    return src.replace(old, new)


def patch(srcdir: str) -> int:
    p_libc = os.path.join(srcdir, "src", "wrapped", "wrappedlibc.c")
    p_priv = os.path.join(srcdir, "src", "wrapped", "wrappedlibc_private.h")
    for p in (p_libc, p_priv):
        if not os.path.isfile(p):
            fail(f"文件不存在: {p}")

    with open(p_libc, "r", encoding="utf-8") as f:
        libc = f.read()
    with open(p_priv, "r", encoding="utf-8") as f:
        priv = f.read()

    if SENTINEL in libc and SENTINEL in priv:
        print("patch_pathmap2: 已应用过，跳过")
        return 0

    # ---- 1) stat 家族补 pathmap（锚点=签名+首行+`(void)`行，唯一） ----
    def stat_job(sig, var):
        head = sig + "{\n"
        old = head + "    (void)emu; (void)v;\n"
        new = (
            head
            + "    (void)emu; (void)v;\n"
            + f"    {var} = (void*)box64_pathmap((const char*){var});  // {SENTINEL}\n"
        )
        return (old, new)

    libc = apply(libc, *stat_job(
        "EXPORT int my___xstat(x64emu_t* emu, int v, void* path, void* buf)\n",
        "path"), 1, "my___xstat", "wrappedlibc.c")
    libc = apply(libc, *stat_job(
        "EXPORT int my___lxstat(x64emu_t* emu, int v, void* name, void* buf)\n",
        "name"), 1, "my___lxstat", "wrappedlibc.c")
    libc = apply(libc, *stat_job(
        "EXPORT int my___fxstatat(x64emu_t* emu, int v, int d, void* path, void* buf, int flags)\n",
        "path"), 1, "my___fxstatat", "wrappedlibc.c")

    # ---- 2) 新增 my_mkdir/my_access，插在 my_stat 定义前 ----
    anchor = "EXPORT int my_stat(x64emu_t *emu, void* filename, void* buf)\n{\n"
    libc = apply(libc, anchor, NEW_FUNCS + anchor, 1,
                 "my_stat 前插新函数", "wrappedlibc.c")

    # ---- 3) private.h：GOW/GO → GOWM ----
    priv = apply(priv, "GOW(access, iFpi)\n", "GOWM(access, iFEpi)\n",
                 1, "GOW(access)", "wrappedlibc_private.h")
    priv = apply(priv, "GOW(mkdir, iFpu)\n", "GOWM(mkdir, iFEpu)\n",
                 1, "GOW(mkdir)", "wrappedlibc_private.h")

    with open(p_libc, "w", encoding="utf-8") as f:
        f.write(libc)
    with open(p_priv, "w", encoding="utf-8") as f:
        f.write(priv)
    print("patch_pathmap2: 已应用 -> wrappedlibc.c(4处), wrappedlibc_private.h(2处)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"用法: {sys.argv[0]} <box64源码目录>")
        sys.exit(2)
    sys.exit(patch(sys.argv[1]))
