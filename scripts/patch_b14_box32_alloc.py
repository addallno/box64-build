#!/usr/bin/env python3
"""box32 guest 分配族路由 actual_*（32 位 steamcmd SIGABRT，BUGS B-14）：

现象：linux32/steamcmd 在 box32 下启动即 SIGABRT。DEBUG 日志
`Calling malloc (...) => Warning, pointer 0x7f... is not a 32bits value`
——wrapper32 的 pEL_32 对返回值 to_ptrv（box32.h TEST_ABORT）发现宿主堆
指针 >4GB 即 box64_abort。

根因（v2 定论）：32 位表 GOM(malloc,calloc,...) 在 STATICBUILD 下展开为
`&my32_##N`，而 `wrapped32/wrappedlibc.c:3393` 的 my32_malloc 是裸
`calloc(1,size)`，my32_calloc/free/strdup/memalign/valloc/
malloc_usable_size 只有 glibc_missing_symbols 弱桩（return 0/空操作）——
上游依赖 mallochook.c EXPORT interpose 层（#ifndef STATICBUILD）重定向，
静态构建裁掉该层后全部直通宿主分配器；proot 下宿主 mmap/brk 在 0x2000...
或 0x7f...，>4GB → to_ptrv abort。GO2 别名（__libc_*/aligned_alloc/
__strdup）STATICBUILD 下直接取 &宿主符号，同样是宿主堆。

改动：
1. wrapped32/wrappedlibc_private.h: 分配族 GOW/GO→GOM（挂 my32_*）、
   GO2 目标宿主符号→my32_*（aligned_alloc/__libc_*/__strdup/__strndup）、
   GOW(posix_memalign)/GOW(strndup)→GOM、GO(__strndup)→GO2
2. wrapped32/wrappedlibc.c: my32_malloc 改 actual_calloc；新增全族
   strong 覆盖弱桩（my32_calloc/realloc/free/memalign/malloc_usable_size/
   strdup/valloc/pvalloc/posix_memalign/strndup）；my32_reallocarray 改
   actual_realloc
3. wrapped32/wrappedlib_init32.h: my64_decls 块补 my_* extern 声明
4. wrapped/wrappedlibc.c: 64 位 my_malloc 改 actual_calloc+my_* 族
   （64 位进程 box64_is32bits=0 走 box_* 与原直通等价，零行为变化）

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

# 64 位侧（wrapped/wrappedlibc.c）：my_malloc 改 actual_* + 补 my_* 族
MALLOC_OLD = """EXPORT void* my_malloc(unsigned long size)
{
    return calloc(1, size);
}
"""
MALLOC_NEW = """EXPORT void* my_malloc(unsigned long size)
{
    // BOX64-BUILD: b14-box32-alloc: static 裁掉 mallochook EXPORT 层后，
    // box32 guest 分配必须经 actual_*（box32 时 box32_* 落 32 位堆）
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
    extern size_t malloc_usable_size(void* ptr); // musl stdlib.h 未声明，防隐式 int() 与 gen 头冲突
    // actual_* 的 box_malloc_usable_size 在 STATICBUILD 下无声明（且 mallochook
    // 变量可能为 NULL），显式分支绕开；32 位堆走 box32_，其余直通宿主
    if(box64_is32bits) return box32_malloc_usable_size(ptr);
    return malloc_usable_size(ptr);
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

# 32 位侧（wrapped32/wrappedlibc.c）：my32_malloc 裸 calloc → actual_* 全族 strong
MY32_MALLOC_OLD = """EXPORT void* my32_malloc(unsigned long size)
{
    return calloc(1, size);
}
"""
MY32_MALLOC_NEW = """EXPORT void* my32_malloc(unsigned long size)
{
    // BOX64-BUILD: b14-box32-alloc: 裸宿主 calloc 返回 >4GB 指针，
    // wrapper32 返回值 to_ptrv（TEST_ABORT）即 abort；改走 actual_*
    //（box64_is32bits 时 box32_* 落 MAP_32BIT 32 位堆）
    return actual_calloc(1, size);
}
EXPORT void* my32_calloc(size_t nmemb, size_t size)
{
    return actual_calloc(nmemb, size);
}
EXPORT void* my32_realloc(void* ptr, size_t size)
{
    return actual_realloc(ptr, size);
}
EXPORT void my32_free(void* ptr)
{
    actual_free(ptr);
}
EXPORT void* my32_memalign(size_t align, size_t size)
{
    return actual_memalign(align, size);
}
EXPORT size_t my32_malloc_usable_size(void* ptr)
{
    extern size_t malloc_usable_size(void* ptr); // musl stdlib.h 未声明，防隐式 int() 与 gen 头冲突
    // 同 my_malloc_usable_size：STATICBUILD 下 box_malloc_usable_size 无声明，显式分支
    if(box64_is32bits) return box32_malloc_usable_size(ptr);
    return malloc_usable_size(ptr);
}
EXPORT char* my32_strdup(const char* s)
{
    if(!s) return NULL;
    if(box64_is32bits) return box32_strdup(s);
    return box_strdup(s);
}
EXPORT void* my32_valloc(size_t size)
{
    return actual_memalign(box64_pagesize, size);
}
EXPORT void* my32_pvalloc(size_t size)
{
    size_t p = box64_pagesize;
    if(!size) size = 1;
    return actual_memalign(p, (size + p - 1) & ~(p - 1));
}
EXPORT int my32_posix_memalign(void** p, size_t align, size_t size)
{
    void* r = actual_memalign(align, size);
    if(!r) return ENOMEM;
    if(p) *p = r;
    return 0;
}
EXPORT char* my32_strndup(const char* s, size_t n)
{
    if(!s) return NULL;
    size_t len = strnlen(s, n);
    char* d = actual_calloc(1, len + 1);
    if(d) memcpy(d, s, len);
    return d;
}
// locale 桩：guest i386 locale 对象与 musl 结构不兼容，直通宿主 __newlocale
// 会在 musl 内部 memcpy 读到垃圾指针崩（访问 0xffffffffffff00，q57 SIGSEGV）。
// 统一返回共享假 C-locale 对象（box64 静态区 <4GB，box32 下 guest 可读）
static int box32_locale_dummy = 0;
EXPORT void* my32_newlocale(x64emu_t* emu, int mask, const char* name, void* out)
{
    (void)emu; (void)mask; (void)name; (void)out;
    return &box32_locale_dummy;
}
EXPORT void* my32_duplocale(x64emu_t* emu, void* loc)
{
    (void)emu; (void)loc;
    return &box32_locale_dummy;
}
EXPORT void* my32_uselocale(x64emu_t* emu, void* loc)
{
    (void)emu; (void)loc;
    return &box32_locale_dummy;
}
EXPORT void my32_freelocale(x64emu_t* emu, void* loc)
{
    (void)emu; (void)loc;
}
"""

REALLOCARRAY_OLD = """EXPORT void* my32_reallocarray(void* ptr, size_t nmemb, size_t size)
{
    return realloc(ptr, nmemb*size);
}
"""
REALLOCARRAY_NEW = """EXPORT void* my32_reallocarray(void* ptr, size_t nmemb, size_t size)
{
    return actual_realloc(ptr, nmemb*size);
}
"""

JOBS = [
    ("src/wrapped32/wrappedlib_init32.h", [
        (EXTERN_ANCHOR, EXTERN_NEW),
    ]),
    ("src/wrapped32/wrappedlibc_private.h", [
        # 分配族 GOW/GO → GOM（挂 my32_*）
        ("GOW(calloc, pELL)\n", "GOM(calloc, pELL)\n"),
        ("GO(realloc, pEpL)\n", "GOM(realloc, pEpL)\n"),
        ("GO(free, vEp)\n", "GOM(free, vEp)\n"),
        ("GOW(memalign, pELL)\n", "GOM(memalign, pELL)\n"),
        ("GOW(strdup, pEp)\n", "GOM(strdup, pEp)\n"),
        ("GO(__strdup, pEp)\n", "GO2(__strdup, pEp, my32_strdup)\n"),
        ("GOW(valloc, pEL)\n", "GOM(valloc, pEL)\n"),
        ("GOW(malloc_usable_size, LEp)\n", "GOM(malloc_usable_size, LEp)\n"),
        # GO2 别名原本 STATICBUILD 直取 &宿主符号 → 改指 my32_*
        ("GO2(aligned_alloc, pELL, memalign)\n", "GO2(aligned_alloc, pELL, my32_memalign)\n"),
        ("GO2(__libc_calloc, pELL, calloc)\n", "GO2(__libc_calloc, pELL, my32_calloc)\n"),
        ("GO2(__libc_free, vEp, free)\n", "GO2(__libc_free, vEp, my32_free)\n"),
        ("GO2(__libc_malloc, pEL, malloc)\n", "GO2(__libc_malloc, pEL, my32_malloc)\n"),
        ("GO2(__libc_memalign, pELL, memalign)\n", "GO2(__libc_memalign, pELL, my32_memalign)\n"),
        ("GO2(__libc_pvalloc, pEL, pvalloc)\n", "GO2(__libc_pvalloc, pEL, my32_pvalloc)\n"),
        ("GO2(__libc_realloc, pEpL, realloc)\n", "GO2(__libc_realloc, pEpL, my32_realloc)\n"),
        ("GO2(__libc_valloc, pEL, valloc)\n", "GO2(__libc_valloc, pEL, my32_valloc)\n"),
        # posix_memalign / strndup 直通宿主 → GOM
        ("GOW(posix_memalign, iEBp_LL)\n", "GOM(posix_memalign, iEBp_LL)\n"),
        ("GOW(strndup, pEpL)\n", "GOM(strndup, pEpL)\n"),
        ("GO(__strndup, pEpL)\n", "GO2(__strndup, pEpL, my32_strndup)\n"),
        # locale 族直通宿主 musl 崩（i386/musl locale 对象 ABI 不兼容）→ my32_* 假桩
        ("GOW(newlocale, aEipa)\n", "GOM(newlocale, aEipa)\n"),
        ("GO(__newlocale, aEipa)\n", "GO2(__newlocale, aEipa, my32_newlocale)\n"),
        ("GO(__duplocale, aEa)\n", "GO2(__duplocale, aEa, my32_duplocale)\n"),
        ("GOW(freelocale, vEA)\n", "GOM(freelocale, vEA)\n"),
        ("GO(__freelocale, vEA)\n", "GO2(__freelocale, vEA, my32_freelocale)\n"),
        ("GOW(uselocale, aEa)\n", "GOM(uselocale, aEa)\n"),
        ("GO(__uselocale, aEa)\n", "GO2(__uselocale, aEa, my32_uselocale)\n"),
    ]),
    ("src/wrapped32/wrappedlibc.c", [
        (MY32_MALLOC_OLD, MY32_MALLOC_NEW),
        (REALLOCARRAY_OLD, REALLOCARRAY_NEW),
    ]),
    ("src/wrapped/wrappedlibc.c", [
        (MALLOC_OLD, MALLOC_NEW),
    ]),
    # 方案2：GOM(mysymbolmap)/GO2(symbol2map)/wmy 三族 resolve 后注册 raw→桥 alternate，
    # 解释器 call [GOT=raw] 时经 getAlternate 转桥，桥 f=my32_* 桩（reloc 打印 raw 不影响执行）
    ("src/librarian/library.c", [
        ('#include "library_inner.h"\n',
         '#include "library_inner.h"\n#include "alternate.h" // B-14: resolve 后注册 raw→桥 alternate\n'),
        # WrappedLib_GetGlobal 入口实证（locale 过滤）
        ("    if (!getSymbolInMaps(lib, name, 1, &addr, &size, &wk, *version, *vername, local, *veropt)) {\n"
         "        return 0;\n"
         "    }\n",
         "    int dbg_get = getSymbolInMaps(lib, name, 1, &addr, &size, &wk, *version, *vername, local, *veropt);\n"
         "    if(strstr(name, \"locale\")) printf_log(LOG_INFO, \"B14GG lib=%s name=%s get=%d addr=%p wk=%d\\n\", lib->name, name, dbg_get, (void*)addr, wk);\n"
         "    if (!dbg_get) {\n"
         "        return 0;\n"
         "    }\n"),
        # getSymbolInSymbolMaps 入口实证
        ("    const khint_t hash = kh_hash(symbolmap, name);\n"
         "    void* symbol;\n",
         "    const khint_t hash = kh_hash(symbolmap, name);\n"
         "    void* symbol;\n"
         "    if(strstr(name, \"locale\")) printf_log(LOG_INFO, \"B14SIM in name=%s noweak=%d\\n\", name, noweak);\n"),
        # symbol2map kh 命中与否实证
        ("    k = kh_get_with_hash(symbol2map, lib->w.symbol2map, name, hash);\n",
         "    k = kh_get_with_hash(symbol2map, lib->w.symbol2map, name, hash);\n"
         "    if(strstr(name, \"locale\")) printf_log(LOG_INFO, \"B14s2kh lib=%s name=%s k=%d end=%d\\n\", lib->name, name, (int)k, (int)kh_end(lib->w.symbol2map));\n"),
        # symbol2map 命中后 weak/resolved 实证
        ("    if (k!=kh_end(lib->w.symbol2map))  {\n"
         "        symbol2_t *s = &kh_value(lib->w.symbol2map, k);\n",
         "    if (k!=kh_end(lib->w.symbol2map))  {\n"
         "        symbol2_t *s = &kh_value(lib->w.symbol2map, k);\n"
         "        if(strstr(name, \"locale\")) printf_log(LOG_INFO, \"B14s2w name=%s weak=%d resolved=%d addr=%p noweak=%d\\n\", name, s->weak, s->resolved, (void*)s->addr, noweak);\n"),
        # 族级命中实证：datamap/stsymbolmap/symbolmap/wsymbolmap
        ("    khint_t k = kh_get(datamap, lib->w.datamap, name);\n"
         "    if (k!=kh_end(lib->w.datamap)) {\n",
         "    khint_t k = kh_get(datamap, lib->w.datamap, name);\n"
         "    if (k!=kh_end(lib->w.datamap)) {\n"
         "        if(strstr(name, \"locale\")) printf_log(LOG_INFO, \"B14hit data name=%s lib=%s\\n\", name, lib->name);\n"),
        ("    k = kh_get_with_hash(symbolmap, lib->w.stsymbolmap, name, hash);\n"
         "    if (k!=kh_end(lib->w.stsymbolmap)) {\n",
         "    k = kh_get_with_hash(symbolmap, lib->w.stsymbolmap, name, hash);\n"
         "    if (k!=kh_end(lib->w.stsymbolmap)) {\n"
         "        if(strstr(name, \"locale\")) printf_log(LOG_INFO, \"B14hit st name=%s lib=%s\\n\", name, lib->name);\n"),
        ("    k = kh_get_with_hash(symbolmap, lib->w.symbolmap, name, hash);\n"
         "    if (k!=kh_end(lib->w.symbolmap)) {\n",
         "    k = kh_get_with_hash(symbolmap, lib->w.symbolmap, name, hash);\n"
         "    if (k!=kh_end(lib->w.symbolmap)) {\n"
         "        if(strstr(name, \"locale\")) printf_log(LOG_INFO, \"B14hit s name=%s lib=%s\\n\", name, lib->name);\n"),
        ("        k = kh_get_with_hash(symbolmap, lib->w.wsymbolmap, name, hash);\n",
         "        k = kh_get_with_hash(symbolmap, lib->w.wsymbolmap, name, hash);\n"
         "        if(strstr(name, \"locale\")) printf_log(LOG_INFO, \"B14hit ws k=%d end=%d name=%s lib=%s\\n\", (int)k, (int)kh_end(lib->w.wsymbolmap), name, lib->name);\n"),
        # mysymbolmap：块内 s2 可见，resolve 后 s->addr=桥
        ("                s->resolved = 1;\n"
         "            }\n"
         "        }\n"
         "        *addr = s->addr;\n"
         "        *size = sizeof(void*);\n"
         "        *weak = 0;\n"
         "        return 1;\n"
         "    }\n"
         "    // check in stsymbolmap (return struct...)\n",
         "                s->resolved = 1;\n"
         "            }\n"
         "            printf_log(LOG_INFO, \"B14DBG my sym=%s symbol=%p addr=%p s2=%d\\n\", name, symbol, (void*)s->addr, s2?1:0);\n"
         "            if(s->addr && (void*)s->addr != symbol)\n"
         "                addAlternate(symbol, (void*)s->addr);\n"
         "        }\n"
         "        *addr = s->addr;\n"
         "        *size = sizeof(void*);\n"
         "        *weak = 0;\n"
         "        return 1;\n"
         "    }\n"
         "    // check in stsymbolmap (return struct...)\n"),
        # wmysymbolmap：双闭合独特缩进
        ("            } else {\n"
         "                s->addr = AddCheckBridge(lib->w.bridge, s->w, symbol, 0, name);\n"
         "                s->resolved = 1;\n"
         "            }\n"
         "            }\n"
         "            *addr = s->addr;\n"
         "            *size = sizeof(void*);\n"
         "            *weak = 1;\n",
         "            } else {\n"
         "                s->addr = AddCheckBridge(lib->w.bridge, s->w, symbol, 0, name);\n"
         "                s->resolved = 1;\n"
         "            }\n"
         "            printf_log(LOG_INFO, \"B14DBG wmy sym=%s symbol=%p addr=%p s2=%d\\n\", name, symbol, (void*)s->addr, s2?1:0);\n"
         "            if(s->addr && (void*)s->addr != symbol)\n"
         "                addAlternate(symbol, (void*)s->addr);\n"
         "            }\n"
         "            *addr = s->addr;\n"
         "            *size = sizeof(void*);\n"
         "            *weak = 1;\n"),
        # symbol2map：*weak = s->weak 唯一定位，块内插
        ("                s->addr = AddCheckBridge(lib->w.bridge, s->w, symbol, 0, name);\n"
         "                s->resolved = 1;\n"
         "            }\n"
         "            *addr = s->addr;\n"
         "            *size = sizeof(void*);\n"
         "            *weak = s->weak;\n",
         "                s->addr = AddCheckBridge(lib->w.bridge, s->w, symbol, 0, name);\n"
         "                s->resolved = 1;\n"
         "                printf_log(LOG_INFO, \"B14DBG s2map sym=%s symbol=%p addr=%p\\n\", name, symbol, (void*)s->addr);\n"
         "                if(s->addr && (void*)s->addr != symbol)\n"
         "                    addAlternate(symbol, (void*)s->addr);\n"
         "            }\n"
         "            *addr = s->addr;\n"
         "            *size = sizeof(void*);\n"
         "            *weak = s->weak;\n"),
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
