#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pathmap3：shm_open/shm_unlink TMPDIR fallback（哨兵 BOX64-BUILD: pathmap3）。

背景：steamcmd 用 POSIX shm 做 IPC（/u10207-ValveIPCSharedObj-Steam），guest
shm_open 是 native 直通 → box64-bin 内 musl 写死 /dev/shm 前缀 → Android 无
/dev/shm → ENOENT/EACCES → threadtools.cpp(2526) "Permission denied" 断言 +
"Process failed to shm_open"。

方案：GOM 包装（非weak，getSymbolInSymbolMaps 919行无条件命中；GOWM 走 !noweak 分支会被跳过），先试宿主 shm_open，失败（ENOENT 等）时提取 basename 落
$TMPDIR/<name>（open 相同 oflag），shm_unlink 同理。
- wrappedlibc_private.h：GO(shmget) 前插 GOM(shm_open,iFEpii)+GOM(shm_unlink,iFEpi)
  （iFEpi wrapper.c:603、iFEpii :1209 typedef 均已存在；mode 按 int 传 ABI 等价）
- wrappedlibc.c：pathmap2 哨兵前插 my_shm_open/my_shm_unlink（依赖链 pathmap2→3）

用法: patch_pathmap3.py <box64源码目录>
"""
import os
import sys

SENTINEL = "BOX64-BUILD: pathmap3"

NEW_FUNCS = r'''// BOX64-BUILD: pathmap3 shm_open/shm_unlink fallback（/dev/shm 不存在时落 TMPDIR）
EXPORT int my_shm_open(x64emu_t* emu, const char* name, int oflag, int mode)
{
    (void)emu;
    int r = shm_open(name, oflag, mode);
    fprintf(stderr, "[pm3] my_shm_open name=%s oflag=%d mode=%d r=%d errno=%d\n", name?name:"(null)", oflag, mode, r, r<0?errno:0);
    if (r >= 0)
        return r;
    if (!name || name[0] == '\0')
        return r;
    const char* base = strrchr(name, '/');
    base = base ? base + 1 : name;
    if (*base == '\0')
        return r;
    const char* tmp = getenv("TMPDIR");
    if (!tmp)
        tmp = "/tmp";
    char path[4096];
    int n = snprintf(path, sizeof(path), "%s/%s", tmp, base);
    if (n < 0 || n >= (int)sizeof(path))
        return r;
    int fd = open(path, oflag, mode);
    fprintf(stderr, "[pm3] fallback open(%s) fd=%d errno=%d\n", path, fd, fd<0?errno:0);
    return fd;
}

EXPORT int my_shm_unlink(x64emu_t* emu, const char* name)
{
    (void)emu;
    int r = shm_unlink(name);
    if (r == 0)
        return r;
    if (!name || name[0] == '\0')
        return r;
    const char* base = strrchr(name, '/');
    base = base ? base + 1 : name;
    if (*base == '\0')
        return r;
    const char* tmp = getenv("TMPDIR");
    if (!tmp)
        tmp = "/tmp";
    char path[4096];
    int n = snprintf(path, sizeof(path), "%s/%s", tmp, base);
    if (n < 0 || n >= (int)sizeof(path)) {
        errno = ENAMETOOLONG;
        return -1;
    }
    return unlink(path);
}
'''


def fail(msg):
    print(f"[patch_pathmap3] 失败: {msg}", file=sys.stderr)
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
        print("patch_pathmap3: 已应用过，跳过")
        return 0

    # 1) my_shm_* 插在 pathmap2 哨兵前（build 链 pathmap2 先于 pathmap3）
    anchor = "// BOX64-BUILD: pathmap2 mkdir/access 路径映射（原为 native 直通）\n"
    libc = apply(libc, anchor, NEW_FUNCS + anchor, 1,
                 "pathmap2 哨兵", "wrappedlibc.c")

    # 2) private.h：shmget 条目前插 GOWM 绑定
    priv = apply(priv,
                 "GO(shmget, iFiLi)\n",
                 "GOM(shm_open, iFEpii)    // " + SENTINEL + " GOM非weak(919直查)"
                 "GOM(shm_unlink, iFEpi)   // " + SENTINEL + " GOM非weak(919直查)"
                 "GO(shmget, iFiLi)\n",
                 1, "GO(shmget)", "wrappedlibc_private.h")

    # --- job4: library.c resolve 观测（临时调试，定位后撤）---
    p_libr = os.path.join(srcdir, "src", "librarian", "library.c")
    with open(p_libr, "r", encoding="utf-8") as f:
        libr = f.read()
    if SENTINEL + " gsym" not in libr:
        a1 = "int getSymbolInMaps(library_t *lib, const char* name, int noweak, uintptr_t *addr, uintptr_t *size, int* weak, int version, const char* vername, int local, int veropt)\n{\n"
        ins1 = ("                if(strstr(name, \"shm\"))\n"
                "                    printf(\"[gsym] q='%s' v=%d vo=%d nw=%d lib=%s\\n\", name, version, veropt, noweak, (lib&&lib->name)?lib->name:\"?\");\n"
                "    // " + SENTINEL + " gsym\n")
        if libr.count(a1) != 1:
            raise SystemExit(f"job4a 锚点计数={libr.count(a1)}")
        libr = libr.replace(a1, a1 + ins1, 1)

        a2 = "    // check in mysymbolmap\n    khint_t k = kh_get_with_hash(symbolmap, lib->w.mysymbolmap, name, hash);\n    if (k!=kh_end(lib->w.mysymbolmap)) {\n        symbol1_t *s = &kh_value(lib->w.mysymbolmap, k);\n"
        ins2 = ("        if(strstr(name, \"shm\"))\n"
                "            printf(\"[gsym] HIT mysymbolmap '%s' resolved=%d addr=%p\\n\", name, s->resolved, (void*)s->addr);\n")
        if libr.count(a2) != 1:
            raise SystemExit(f"job4b 锚点计数={libr.count(a2)}")
        libr = libr.replace(a2, a2 + ins2, 1)
        with open(p_libr, "w", encoding="utf-8") as f:
            f.write(libr)
        print("patch_pathmap3: library.c 观测已应用(2处)")
    else:
        print("patch_pathmap3: library.c 观测已存在, 跳过")

    with open(p_libc, "w", encoding="utf-8") as f:
        f.write(libc)
    with open(p_priv, "w", encoding="utf-8") as f:
        f.write(priv)
    print("patch_pathmap3: 已应用 -> wrappedlibc.c(1处), wrappedlibc_private.h(2处)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"用法: {sys.argv[0]} <box64源码目录>")
        sys.exit(2)
    sys.exit(patch(sys.argv[1]))
