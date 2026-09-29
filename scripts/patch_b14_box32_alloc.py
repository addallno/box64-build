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
// 且 wrapper aEipa_32 对返回值调 to_locale()：非小值 host 指针会被
// to_struct_locale 当 locale 结构 fill（&dummy int 被误读出野指针，同样崩）。
// 返回 box64 静态 fake 缓冲（<4GB，box32 guest 直接可读，guest 还会
// 解引用如 [loc+0x3c]）；box32.c to_locale 对该地址特例直通不 fill。
extern void* box32_get_fake_locale(void);
EXPORT void* my32_newlocale(x64emu_t* emu, int mask, const char* name, void* out)
{
    (void)emu; (void)mask; (void)name; (void)out;
    printf_log(LOG_INFO, "B14STUB newlocale\\n"); // B-14 诊断：确认桥 native 目标=stub
    return box32_get_fake_locale();
}
EXPORT void* my32_duplocale(x64emu_t* emu, void* loc)
{
    (void)emu; (void)loc;
    return box32_get_fake_locale();
}
EXPORT void* my32_uselocale(x64emu_t* emu, void* loc)
{
    (void)emu; (void)loc;
    return box32_get_fake_locale();
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
        # B-14 根因：32 位 symbol2map 注册即 resolved=1 → 查询跳过建桥/alternate →
        # 返回 raw my32 地址进 GOT → 解释器执行 host 地址 → CheckExec SIGSEGV。
        # 置 0 后首查询走 AddCheckBridge（建桥 f=my32）+ addAlternate(raw→桥)。
        ("        kh_value(lib->w.symbol2map, k).resolved = 1;\n",
         "        kh_value(lib->w.symbol2map, k).resolved = 0; // B-14: 首查询时建桥+addAlternate\n"),
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
        # freeifaddrs 配套 my32 深拷贝链（直通 musl free 会崩 box32 块）
        ("GO(freeifaddrs, vEp)\n", "GO2(freeifaddrs, vEp, my32_freeifaddrs)\n"),
    ]),
    ("src/wrapped32/wrappedlibc.c", [
        (MY32_MALLOC_OLD, MY32_MALLOC_NEW),
        (REALLOCARRAY_OLD, REALLOCARRAY_NEW),
        # B-14 诊断：qsort 链打点（compar 回调参数错位 vs 数组含 NULL 的定位）
        ("static int my32_compare_r_cb(void* a, void* b, compare_r_t* arg)\n{\n",
         "static int my32_compare_r_cb(void* a, void* b, compare_r_t* arg)\n{\n"
         "    { static int n=0; if(n<5) { ++n; printf_log(LOG_INFO, \"B14QCMP #%d a=%p *a=%p b=%p *b=%p\\n\", n, a, *(void**)a, b, *(void**)b); fflush(stderr); } }\n"),
        ("EXPORT void my32_qsort(x64emu_t* emu, void* base, size_t nmemb, size_t size, void* fnc)\n{\n",
         "EXPORT void my32_qsort(x64emu_t* emu, void* base, size_t nmemb, size_t size, void* fnc)\n{\n"
         "    printf_log(LOG_INFO, \"B14QSORT base=%p n=%d sz=%d f=%p\\n\", base, (int)nmemb, (int)size, fnc);\n"
         "    { uint32_t* p=(uint32_t*)base; int i, nz=0; for(i=0;i<(int)nmemb;++i) if(p[i]) ++nz;\n"
         "      printf_log(LOG_INFO, \"B14QDUMP nz=%d/%d e0=%08x e1=%08x e2=%08x e59=%08x\\n\", nz, (int)nmemb, p[0], p[1], p[2], p[(int)nmemb-1]); fflush(stderr); }\n"),
        # B14QDONE：qsort_r 是否完整返回（区分崩在 qsort 内 vs 后续调用）
        ("    qsort_r(base, nmemb, size, (__compar_d_fn_t)my32_compare_r_cb, &args);\n}\nEXPORT void my32_qsort_r",
         "    qsort_r(base, nmemb, size, (__compar_d_fn_t)my32_compare_r_cb, &args);\n"
         "    printf_log(LOG_INFO, \"B14QDONE base=%p\\n\", base); fflush(stderr);\n"
         "}\nEXPORT void my32_qsort_r"),
        # B-14: vasprintf 族 host 缓冲 >4G（brk/宿主映射）→ to_ptrv 触发 TEST_ABORT；
        # 拷入 box32 堆后释放原缓冲，四出参点统一走 b14_chk_out
        ("EXPORT int my32_asprintf(x64emu_t* emu, ptr_t* buff, void * fmt, void * b) {\n",
         "// B-14: >4G host 缓冲拷入 box32 堆再 to_ptrv（防 box64_abort）\n"
         "static ptr_t b14_chk_out(char* p)\n"
         "{\n"
         "    if((uintptr_t)p >> 32) {\n"
         "        char* q = box32_strdup(p);\n"
         "        free(p);\n"
         "        return to_ptrv(q);\n"
         "    }\n"
         "    return to_ptrv(p);\n"
         "}\n"
         "EXPORT int my32_asprintf(x64emu_t* emu, ptr_t* buff, void * fmt, void * b) {\n"),
        ("    *buff = to_ptrv(res);\n", "    *buff = b14_chk_out(res);\n"),
        ("    *strp = to_ptrv(res);\n", "    *strp = b14_chk_out(res);\n"),
        ("    *strp = to_ptrv(p);\n", "    *strp = b14_chk_out(p);\n"),
        ("    *result_ptr = to_ptrv(p);\n", "    *result_ptr = b14_chk_out(p);\n"),
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
    # B-14: box32.c 提供 guest 可读 fake locale 缓冲；to_locale 对其直通不 fill
    ("src/box32.c", [
        ("void* from_locale(ptr_t l) {\n",
         "// B-14: fake locale 缓冲：guest 直接解引用 locale_t（如 [loc+0x3c]），\n"
         "// 静态零填充区 <4GB box32 可读；to_locale 对该地址特例直通\n"
         "static char box32_fake_locale[4096];\n"
         "void* box32_get_fake_locale(void) { return box32_fake_locale; }\n"
         "\n"
         "void* from_locale(ptr_t l) {\n"),
        ("ptr_t to_locale(void* p) {\n    if((uintptr_t)p < 0x100) {\n",
         "ptr_t to_locale(void* p) {\n"
         # volatile 防编译器（LTO）判定 p 不可能为 fake 数组而删除比较
         "    void* volatile b14fl = box32_fake_locale;\n"
         "    if(p == b14fl) {\n"
         "        return to_ptrv(p);\n"
         "    }\n"
         "    if((uintptr_t)p < 0x100) {\n"),
    ]),
    # B-14: STATICBUILD 下 box_malloc_usable_size=NULL（dlsym 裁剪），
    # 非 custom 地址 call NULL → SIGSEGV @0；防 NULL 返回 0
    ("src/mallochook.c", [
        ("size_t box32_malloc_usable_size(void* p)\n"
         "{\n"
         "    if(isCustomAddr(p))\n"
         "        return customGetUsableSize(p);\n"
         "    else\n"
         "        return box_malloc_usable_size(p);\n"
         "}\n",
        "size_t box32_malloc_usable_size(void* p)\n"
        "{\n"
        "    if(isCustomAddr(p))\n"
        "        return customGetUsableSize(p);\n"
        "    else if(box_malloc_usable_size)\n"
        "        return box_malloc_usable_size(p);\n"
        "    else {\n"
        "        extern size_t malloc_usable_size(void*); // B-14: 指针 NULL，直通 musl 真实现（堆 0xFFF8xxxx usable=0 会让 guest 逻辑出错）\n"
        "        return malloc_usable_size(p);\n"
        "    }\n"
        "}\n"),
    ]),
    # B-14: dladdr 返回的 host 侧字符串（host heap 0x7f...）to_ptrv 超 4GB →
    # box64_abort()；字符串拷到 box32 可达区，指针超范围置 0
    ("src/wrapped32/wrappedlibdl.c", [
        ("    info->dli_saddr = to_ptrv(start);\n"
         "    info->dli_fname = to_ptrv((void*)fname);\n"
         "    info->dli_fbase = to_ptrv(base);\n"
         "    // TODO: If dli_sname points to native_name, how do I avoid data tampering during usage?\n"
         "    info->dli_sname = to_ptrv((void*)sname);\n",
         "    // B-14: >4GB host 指针防 abort：字符串 box32_strdup 拷到 <4GB，指针置 0\n"
         "    #define B14_S32(s) ((void*)((uintptr_t)(s) < 0x100000000LL ? (uintptr_t)(s) : ((s) ? (uintptr_t)box32_strdup(s) : 0)))\n"
         "    #define B14_P32(p) ((void*)((uintptr_t)(p) < 0x100000000LL ? (uintptr_t)(p) : 0))\n"
         "    info->dli_saddr = to_ptrv(B14_P32(start));\n"
         "    info->dli_fname = to_ptrv(B14_S32(fname));\n"
         "    info->dli_fbase = to_ptrv(B14_P32(base));\n"
         "    info->dli_sname = to_ptrv(B14_S32(sname));\n"
         "    #undef B14_S32\n"
         "    #undef B14_P32\n"),
    ]),
    # B-14: getifaddrs host 链（>4G）深拷贝到 box32 堆，防 to_ptrv TEST_ABORT；
    # 原实现 getifaddrs((void*)res) 直接把 host 指针写 guest 出参且就地窄化，字段
    # 全是 host 指针。改为：节点整块紧凑数组（orig 藏首节点尾，同 getaddrinfo 先例），
    # name/sockaddr/ifa_data 各自 box32 拷贝；配套 my32_freeifaddrs 成对释放。
    ("src/libtools/libc_net32.c", [
        ("EXPORT int my32_getifaddrs(x64emu_t* emu, void** res)\n"
         "{\n"
         "    int ret = getifaddrs((void*)res);\n"
         "    if(!ret) {\n"
         "        // convert the chained list of ifaddrs to i386 (narrowed) in place\n"
         "        struct ifaddrs* p = *res;\n"
         "        while(p) {\n"
         "            struct i386_ifaddrs *i386 = (struct i386_ifaddrs*)p;\n"
         "            struct ifaddrs* next = p->ifa_next;\n"
         "            i386->ifa_next = to_ptrv(p->ifa_next);\n"
         "            i386->ifa_name = to_cstring(p->ifa_name);\n"
         "            i386->ifa_flags = p->ifa_flags;\n"
         "            i386->ifa_addr = to_ptrv(p->ifa_addr);\n"
         "            i386->ifa_netmask = to_ptrv(p->ifa_netmask);\n"
         "            i386->ifa_ifu = (i386->ifa_flags&IFF_BROADCAST)?to_ptrv(p->ifa_broadaddr):to_ptrv(p->ifa_dstaddr);\n"
         "            i386->ifa_data = to_ptrv(p->ifa_data);\n"
         "            p = next;\n"
         "        }\n"
         "    }\n"
         "    return ret;\n"
         "}\n",
         "// B-14: host 侧任意小缓冲拷入 box32 堆（>4G 防 abort；长度按 usable 封顶）\n"
         "static ptr_t b14_ifa_blob(void* s)\n"
         "{\n"
         "    if(!s) return 0;\n"
         "    size_t n = box32_malloc_usable_size(s);\n"
         "    if(!n) n = 128;\n"
         "    if(n > 512) n = 512;\n"
         "    void* d = box32_calloc(1, n);\n"
         "    memcpy(d, s, n);\n"
         "    return to_ptrv(d);\n"
         "}\n"
         "EXPORT int my32_getifaddrs(x64emu_t* emu, void** res)\n"
         "{\n"
         "    struct ifaddrs* host = NULL;\n"
         "    int ret = getifaddrs(&host);\n"
         "    if(ret || !host) { *res = 0; return ret; }\n"
         "    int idx = 0;\n"
         "    for(struct ifaddrs* q = host; q; q = q->ifa_next) ++idx;\n"
         "    // 布局：[i386_0][void* orig][i386_1..]（同 my32_getaddrinfo 先例）\n"
         "    struct i386_ifaddrs* r = actual_malloc(idx*sizeof(struct i386_ifaddrs)+sizeof(void*));\n"
         "    char* cur = (char*)r;\n"
         "    *(void**)(cur + sizeof(struct i386_ifaddrs)) = host;\n"
         "    char* nxt = cur + sizeof(struct i386_ifaddrs) + sizeof(void*);\n"
         "    for(struct ifaddrs* p = host; p; p = p->ifa_next) {\n"
         "        struct i386_ifaddrs* d = (struct i386_ifaddrs*)cur;\n"
         "        d->ifa_next = p->ifa_next ? (ptr_t)(uintptr_t)nxt : 0;\n"
         "        d->ifa_name = to_ptrv(box32_strdup(p->ifa_name));\n"
         "        d->ifa_flags = p->ifa_flags;\n"
         "        d->ifa_addr = b14_ifa_blob(p->ifa_addr);\n"
         "        d->ifa_netmask = b14_ifa_blob(p->ifa_netmask);\n"
         "        d->ifa_ifu = b14_ifa_blob((p->ifa_flags&IFF_BROADCAST)?p->ifa_broadaddr:p->ifa_dstaddr);\n"
         "        d->ifa_data = b14_ifa_blob(p->ifa_data);\n"
         "        cur = nxt;\n"
         "        nxt = cur + sizeof(struct i386_ifaddrs);\n"
         "    }\n"
         "    *res = r;\n"
         "    freeifaddrs(host);\n"
         "    return 0;\n"
         "}\n"
         "EXPORT void my32_freeifaddrs(x64emu_t* emu, void* a)\n"
         "{\n"
         "    if(!a) return;\n"
         "    void* orig = *(void**)((char*)a + sizeof(struct i386_ifaddrs));\n"
         "    struct i386_ifaddrs* d = (struct i386_ifaddrs*)a;\n"
         "    while(d) {\n"
         "        struct i386_ifaddrs* nx = d->ifa_next ? (struct i386_ifaddrs*)(uintptr_t)d->ifa_next : NULL;\n"
         "        box32_free(from_ptrv(d->ifa_name));\n"
         "        box32_free(from_ptrv(d->ifa_addr));\n"
         "        box32_free(from_ptrv(d->ifa_netmask));\n"
         "        box32_free(from_ptrv(d->ifa_ifu));\n"
         "        box32_free(from_ptrv(d->ifa_data));\n"
         "        d = nx;\n"
         "    }\n"
         "    if(orig) freeifaddrs(orig);\n"
         "    actual_free(a);\n"
         "}\n"),
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
