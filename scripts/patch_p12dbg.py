#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
临时打点 patch v2（B-12 waker 定位，结论后从挂链移除）：
sem_post/sem_wait 原为 GO（直转 host，x64Syscall 不可见），改为 GOM+my_ 打印；
my_pthread_create 打印创建者。
输出标记：P12POST / P12WAIT / P12CREATE
哨兵：BOX64-BUILD: p12dbg
用法：python3 patch_p12dbg.py <box64源码目录>
"""
import sys
import os

SENTINEL = "BOX64-BUILD: p12dbg"


def fail(msg):
    print(f"[patch_p12dbg] 失败: {msg}", file=sys.stderr)
    sys.exit(1)


def sub1(src, old, new, tag):
    n = src.count(old)
    if n != 1:
        fail(f"{tag}: 锚点命中 {n} 次（期望 1）")
    return src.replace(old, new, 1)


def main():
    if len(sys.argv) != 2:
        fail("用法: patch_p12dbg.py <box64目录>")
    root = sys.argv[1]
    jobs = []

    # ---- threads.c：include + my_pthread_create 打印 + my_sem_* 实现 ----
    tc = os.path.join(root, "src", "libtools", "threads.c")
    if not os.path.isfile(tc):
        fail(f"文件不存在: {tc}")
    s = open(tc, encoding="utf-8").read()
    if SENTINEL not in s:
        # 1) semaphore.h include
        s = sub1(
            s,
            "#include <pthread.h>\n",
            "#include <pthread.h>\n#include <semaphore.h> // BOX64-BUILD: p12dbg sem 打点需要\n",
            "threads.c include",
        )
        # 2) my_pthread_create 入口打印
        s = sub1(
            s,
            "EXPORT int my_pthread_create(x64emu_t *emu, void* t, void* attr, void* start_routine, void* arg)\n{\n",
            "EXPORT int my_pthread_create(x64emu_t *emu, void* t, void* attr, void* start_routine, void* arg)\n"
            "{\n"
            '\tprintf_log(LOG_INFO, "P12CREATE tid=%d start=%p arg=%p\\n", GetTID(), start_routine, arg); // BOX64-BUILD: p12dbg\n',
            "threads.c create打印",
        )
        # 3) my_sem_post / my_sem_wait 实现（插在 my_pthread_create 之前）
        sem_impl = (
            "// BOX64-BUILD: p12dbg 临时 sem 打点（GOM 目标；直调 musl sem_*，与现成 my_ 模式一致）\n"
            "EXPORT int my_sem_post(x64emu_t* emu, sem_t* s)\n"
            "{\n"
            '\t(void)emu;\n'
            '\tprintf_log(LOG_INFO, "P12POST tid=%d sem=%p\\n", GetTID(), (void*)s);\n'
            "\treturn sem_post(s);\n"
            "}\n"
            "EXPORT int my_sem_wait(x64emu_t* emu, sem_t* s)\n"
            "{\n"
            '\t(void)emu;\n'
            '\tprintf_log(LOG_INFO, "P12WAIT tid=%d sem=%p\\n", GetTID(), (void*)s);\n'
            "\treturn sem_wait(s);\n"
            "}\n"
            "\n"
        )
        s = sub1(
            s,
            "EXPORT int my_pthread_create(x64emu_t *emu, void* t, void* attr, void* start_routine, void* arg)\n",
            sem_impl + "EXPORT int my_pthread_create(x64emu_t *emu, void* t, void* attr, void* start_routine, void* arg)\n",
            "threads.c sem实现",
        )
        jobs.append(tc)
        open(tc, "w", encoding="utf-8").write(s)
        print("[patch_p12dbg] threads.c: 3 处注入完成")
    else:
        print("[patch_p12dbg] threads.c 已应用过，跳过")

    # ---- x64syscall.c：futex/clone 直发路径打点（v1 合并回来）----
    xs = os.path.join(root, "src", "emu", "x64syscall.c")
    if not os.path.isfile(xs):
        fail(f"文件不存在: {xs}")
    s0 = open(xs, encoding="utf-8").read()
    if SENTINEL not in s0:
        anchor = (
            "    uint32_t s = R_EAX; // EAX? (syscalls only go up to 547 anyways)\n"
            "    int log = 0;\n"
        )
        s0 = sub1(s0, anchor, anchor +
            "    {\n"
            "        static int p12n = 0; // BOX64-BUILD: p12dbg ENTER 计数探针\n"
            '        if((++p12n) <= 8 || (p12n % 500) == 0)\n'
            '            printf_log(LOG_INFO, "P12ENTER tid=%d s=%d n=%d\\n", GetTID(), s, p12n);\n'
            "    }\n"
            "    if(s == 202 || s == 56 || s == 435) { // BOX64-BUILD: p12dbg v1 合并\n"
            "        if(s == 202)\n"
            '            printf_log(LOG_INFO, "P12FUTEX tid=%d addr=%p op=%u arg=%d\\n",\n'
            "                GetTID(), (void*)R_RDI, (unsigned)R_RSI, (int)R_RDX);\n"
            "        else\n"
            '            printf_log(LOG_INFO, "P12CLONE tid=%d s=%d flag=%u\\n",\n'
            "                GetTID(), s, (unsigned)R_RDI);\n"
            "    }\n",
            "x64syscall.c 打点")
        open(xs, "w", encoding="utf-8").write(s0)
        print("[patch_p12dbg] x64syscall.c: v1 打点注入完成")
    else:
        print("[patch_p12dbg] x64syscall.c 已应用过，跳过")

    # ---- static_threads.h：my_sem_* 原型（GOM 的 &my_##N 需要可见声明）----
    st = os.path.join(root, "src", "libtools", "static_threads.h")
    if not os.path.isfile(st):
        fail(f"文件不存在: {st}")
    s = open(st, encoding="utf-8").read()
    if "int my_sem_post(x64emu_t* emu, sem_t* s);" not in s:
        s = sub1(
            s,
            "#include <pthread.h>\n",
            "#include <pthread.h>\n#include <semaphore.h> // BOX64-BUILD: p12dbg\n",
            "static_threads.h include",
        )
        s = sub1(
            s,
            "int my_pthread_create(x64emu_t *emu, void* t, void* attr, void* start_routine, void* arg);",
            "int my_sem_post(x64emu_t* emu, sem_t* s); // BOX64-BUILD: p12dbg\n"
            "int my_sem_wait(x64emu_t* emu, sem_t* s); // BOX64-BUILD: p12dbg\n"
            "int my_pthread_create(x64emu_t *emu, void* t, void* attr, void* start_routine, void* arg);",
            "static_threads.h 原型",
        )
        open(st, "w", encoding="utf-8").write(s)
        print("[patch_p12dbg] static_threads.h: 原型注入完成")
    else:
        print("[patch_p12dbg] static_threads.h 已应用过，跳过")

    # ---- private.h：GO -> GOM ----
    ph = os.path.join(root, "src", "wrapped", "wrappedlibpthread_private.h")
    if not os.path.isfile(ph):
        fail(f"文件不存在: {ph}")
    s = open(ph, encoding="utf-8").read()
    changed = False
    if "GOM(sem_post, iFEp) // BOX64-BUILD: p12dbg" not in s:
        s = sub1(s, "GO(sem_post, iFp)\n",
                 "GOM(sem_post, iFEp) // BOX64-BUILD: p12dbg\n", "private.h sem_post")
        changed = True
    if "GOM(sem_wait, iFEp) // BOX64-BUILD: p12dbg" not in s:
        s = sub1(s, "GO(sem_wait, iFp)\n",
                 "GOM(sem_wait, iFEp) // BOX64-BUILD: p12dbg\n", "private.h sem_wait")
        changed = True
    if changed:
        open(ph, "w", encoding="utf-8").write(s)
        print("[patch_p12dbg] private.h: GO->GOM 完成")
    else:
        print("[patch_p12dbg] private.h 已应用过，跳过")

    # ---- dynarec_native.c：FillBlock 两行加 tid + CreateEmptyBlock P12EMPTY（v6）----
    dn = os.path.join(root, "src", "dynarec", "dynarec_native.c")
    if not os.path.isfile(dn):
        fail(f"文件不存在: {dn}")
    s = open(dn, encoding="utf-8").read()
    if "P12EMPTY" not in s:
        s = sub1(
            s,
            'printf_log(LOG_INFO, "FillBlock at %p triggered a segfault, truncating at %d(%p)\\n", (void*)addr, helper.size, (void*)end);',
            'printf_log(LOG_INFO, "FillBlock tid=%d at %p triggered a segfault, truncating at %d(%p)\\n", GetTID(), (void*)addr, helper.size, (void*)end); // BOX64-BUILD: p12dbg',
            "dynarec_native truncating tid",
        )
        s = sub1(
            s,
            'printf_log(LOG_INFO, "FillBlock at %p triggered a segfault (state=%d, size=%d), canceling\\n", (void*)addr, state, helper.size);',
            'printf_log(LOG_INFO, "FillBlock tid=%d at %p triggered a segfault (state=%d, size=%d), canceling\\n", GetTID(), (void*)addr, state, helper.size); // BOX64-BUILD: p12dbg',
            "dynarec_native canceling tid",
        )
        s = sub1(
            s,
            "    // all done...\n"
            "    ClearCache(actual_p+sizeof(void*), JMPNEXT_SIZE-sizeof(void*));   // need to clear the cache before execution...\n"
            "    return block;",
            '    printf_log(LOG_INFO, "P12EMPTY tid=%d addr=%p\\n", GetTID(), (void*)addr); // BOX64-BUILD: p12dbg\n'
            "    // all done...\n"
            "    ClearCache(actual_p+sizeof(void*), JMPNEXT_SIZE-sizeof(void*));   // need to clear the cache before execution...\n"
            "    return block;",
            "dynarec_native P12EMPTY",
        )
        open(dn, "w", encoding="utf-8").write(s)
        print("[patch_p12dbg] dynarec_native.c: tid+P12EMPTY 注入完成")
    else:
        print("[patch_p12dbg] dynarec_native.c 已应用过，跳过")

    # ---- dynarec.c：解释器回退分支 P12INTERP 限流打印（v6）----
    dc = os.path.join(root, "src", "dynarec", "dynarec.c")
    if not os.path.isfile(dc):
        fail(f"文件不存在: {dc}")
    s = open(dc, encoding="utf-8").read()
    if "P12INTERP" not in s:
        s = sub1(
            s,
            "            if(!block || !block->block || !block->done || ACCESS_FLAG(F_TF)) {\n"
            "                // no block, or block doesn't have DynaRec content (yet, temp is not null)\n",
            "            if(!block || !block->block || !block->done || ACCESS_FLAG(F_TF)) {\n"
            "                // no block, or block doesn't have DynaRec content (yet, temp is not null)\n"
            "                { // BOX64-BUILD: p12dbg done=0/空块解释器回退限流打点\n"
            "                    static int p12i = 0;\n"
            '                    if((++p12i) <= 20 || (p12i % 500) == 0)\n'
            '                        printf_log(LOG_INFO, "P12INTERP n=%d tid=%d rip=%p block=%p done=%d\\n", p12i, GetTID(), (void*)R_RIP, block, block ? (int)block->done : -1);\n'
            "                }\n",
            "dynarec.c P12INTERP",
        )
        open(dc, "w", encoding="utf-8").write(s)
        print("[patch_p12dbg] dynarec.c: P12INTERP 注入完成")
    else:
        print("[patch_p12dbg] dynarec.c 已应用过，跳过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
