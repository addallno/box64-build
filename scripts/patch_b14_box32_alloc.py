#!/usr/bin/env python3
"""box32 guest 分配族路由 actual_*（32 位 steamcmd SIGABRT，BUGS B-14）：

现象：linux32/steamcmd 在 box32 下启动即 SIGABRT。DEBUG 日志
`Calling malloc (00000008,...) => Warning, pointer 0x2000001880 is not a
32bits value`——wrapper32 的 pEL_32 对返回值 to_ptrv（box32.h TEST_ABORT）
发现宿主堆指针 >4GB 即 box64_abort。

根因：上游 box32 依赖 mallochook.c 的 EXPORT interpose 层（Exterminate 策略，
# ifndef STATICBUILD）把裸 malloc/calloc/free 全部重定向到 actual_*
（box32 时经 box32_* 落 MAP_32BIT 32 位堆）。box64-build 静态构建为避免与
musl 重复符号裁掉了该 EXPORT 区，导致 wrapped32 表中 GOM(malloc)→my_malloc
内部裸 calloc 及 GOW(calloc/realloc/free/memalign/strdup/valloc/
malloc_usable_size) 直通宿主分配器；proot 下宿主 mmap/brk 在 0x2000000000
或 0x7f...，全部 >4GB → 返回值 to_ptrv abort。

改动：
1. wrapped32/wrappedlibc_private.h: 分配族 GOW/GO → GOM（挂接 my_*）
   （__libc_* GO2 别名自动跟随；__strdup 改 GO2 指向 strdup）
2. wrapped32/wrappedlib_init32.h: my64_decls 块补 my_* extern 声明
3. wrapped/wrappedlibc.c: my_malloc 改 actual_calloc；新增 my_calloc/
   my_realloc/my_free/my_memalign/my_malloc_usable_size/my_strdup/
   my_valloc（64 位进程走 box_* 与原直通等价，零行为变化）

用法: patch_b14_box32_alloc.py <box64源码目录>
幂等: init32.h 含哨兵 BOX64-BUILD: b14-box32-alloc 则跳过。
"""
import os
import sys

SENTINEL = "BOX64-BUILD: b14-box32-alloc"

EXTERN_ANCHOR = "extern void* my_malloc(unsigned long size);\n"
EXTERN_NEW = EXTERN_ANCHOR + """extern void* my_calloc(size_t nmemb, size_t size);
extern void* my_realloc(void* ptr, size_t size);
extern void my_free(void* ptr);
extern void* my_memalign(size_t alignment, size_t size);
extern size_t my_malloc_usable_size(void* ptr);
extern char* my_strdup(const char* s);
extern void* my_valloc(size_t size);
/* BOX64-BUILD: b14-box32-alloc */
"""

MALLOC_OLD = """EXPORT void* my_malloc(unsigned long size)
{
    return calloc(1, size);
}
"""
MALLOC_NEW = """EXPORT void* my_malloc(unsigned long size)
{
    // BOX64-BUILD: b14-box32-alloc: static 裁掉 mallochook EXPORT 层后，box32 guest 分配
    // 必须经 actual_*（box32 时 box32_* 落 32 位堆）；宿主 musl 堆在 proot 下 >4GB，
    // 直通会被 wrapper32 返回值 to_ptrv（TEST_ABORT）打断
    return actual_calloc(1, size);
}
EXPORT void* my_calloc(size_t nmemb, size_t size)
{
    return actual_calloc(nmemb, size);
}
EXPORT void* my_realloc(void* ptr, size_t size)
{
    return actual_realloc(ptr, size);
}
EXPORT void my_free(void* ptr)
{
    actual_free(ptr);
}
EXPORT void* my_memalign(size_t alignment, size_t size)
{
    return actual_memalign(alignment, size);
}
EXPORT size_t my_malloc_usable_size(void* ptr)
{
    return actual_malloc_usable_size(ptr);
}
EXPORT char* my_strdup(const char* s)
{
    if(!s) return NULL;
    if(box64_is32bits) return box32_strdup(s);
    return box_strdup(s);
}
EXPORT void* my_valloc(size_t size)
{
    return actual_memalign(box64_pagesize, size);
}
"""

JOBS = [
    ("src/wrapped32/wrappedlib_init32.h", [
        (EXTERN_ANCHOR, EXTERN_NEW),
    ]),
    ("src/wrapped32/wrappedlibc_private.h", [
        ("GOW(calloc, pELL)\n", "GOM(calloc, pELL)\n"),
        ("GO(realloc, pEpL)\n", "GOM(realloc, pEpL)\n"),
        ("GO(free, vEp)\n", "GOM(free, vEp)\n"),
        ("GOW(memalign, pELL)\n", "GOM(memalign, pELL)\n"),
        ("GOW(strdup, pEp)\n", "GOM(strdup, pEp)\n"),
        ("GO(__strdup, pEp)\n", "GO2(__strdup, pEp, strdup)\n"),
        ("GOW(valloc, pEL)\n", "GOM(valloc, pEL)\n"),
        ("GOW(malloc_usable_size, LEp)\n", "GOM(malloc_usable_size, LEp)\n"),
    ]),
    ("src/wrapped/wrappedlibc.c", [
        (MALLOC_OLD, MALLOC_NEW),
    ]),
]


def patch(srcdir: str) -> int:
    # 幂等：init32.h 已含哨兵则整体跳过
    initp = os.path.join(srcdir, "src/wrapped32/wrappedlib_init32.h")
    with open(initp, "r") as f:
        if SENTINEL in f.read():
            print("patch_b14_box32_alloc: 已应用，跳过")
            return 0
    # 先读入全部文件并校验全部锚点，全部通过才写盘（避免半应用脏状态）
    data = {}
    for rel, _ in JOBS:
        path = os.path.join(srcdir, rel)
        with open(path, "r") as f:
            data[rel] = f.read()
    for rel, changes in JOBS:
        for old, _new in changes:
            c = data[rel].count(old)
            if c != 1:
                print(f"patch_b14_box32_alloc: {rel} 锚点出现 {c} 次（需为1）: {old[:70]!r}",
                      file=sys.stderr)
                return 2
    for rel, changes in JOBS:
        for old, new in changes:
            data[rel] = data[rel].replace(old, new, 1)
    for rel, _ in JOBS:
        path = os.path.join(srcdir, rel)
        with open(path, "w") as f:
            f.write(data[rel])
        print(f"patch_b14_box32_alloc: {rel} OK")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("用法: patch_b14_box32_alloc.py <box64源码目录>", file=sys.stderr)
        sys.exit(1)
    sys.exit(patch(sys.argv[1]))
