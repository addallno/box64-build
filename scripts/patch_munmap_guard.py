#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B-12 修复 patch：EXPORT munmap 按 mapallmem 标记守卫。
背景：guest munmap 经 native trampoline 直达 EXPORT munmap（custommmap.c），
无校验直接 InternalMunmap —— 当 guest 对已释放地址重复 munmap（POSIX 语义应
no-op）时，若 box64 musl realloc 已把内部结构（p_blocks）捡回该 VA，
就会拆掉 p_blocks 所在页 → FillBlock 遍历 p_blocks[1] SEGV → STUCK。
守卫：按 mapallmem 标记区分「guest 合法映射」与「无标记/内部页」，
后者直接 no-op 返回 0，不动真实页表。
哨兵：BOX64-BUILD: munmap-guard
用法：python3 patch_munmap_guard.py <box64源码目录>
"""
import sys
import os

SENTINEL = "BOX64-BUILD: munmap-guard"

def fail(msg):
    print(f"[patch_munmap_guard] 失败: {msg}", file=sys.stderr)
    sys.exit(1)

def apply_file(path, jobs):
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    if SENTINEL in src:
        print(f"[patch_munmap_guard] {os.path.basename(path)} 已应用过，跳过")
        return
    for anchor, repl in jobs:
        n = src.count(anchor)
        if n != 1:
            fail(f"{os.path.basename(path)} 锚点命中 {n} 次（期望 1）:\n{anchor[:80]}")
        src = src.replace(anchor, repl, 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print(f"[patch_munmap_guard] {os.path.basename(path)} 应用 {len(jobs)} 处")

def main():
    if len(sys.argv) != 2:
        fail("用法: patch_munmap_guard.py <box64目录>")
    root = sys.argv[1]

    # ---- custommem.c：辅助函数插在 freeProtection 定义前 ----
    cmem = os.path.join(root, "src", "custommem.c")
    if not os.path.isfile(cmem):
        fail(f"文件不存在: {cmem}")
    helper = (
        "// BOX64-BUILD: munmap-guard 返回 addr 的 mapallmem 标记，0 表示禁止 munmap 拆除（B-12 修复）\n"
        "uintptr_t box_guest_mapping_flag(uintptr_t addr)\n"
        "{\n"
        "    if(!mapallmem) return 1;   // 未启用全内存追踪时保持原行为（允许）\n"
        "    uintptr_t flag = (uintptr_t)rb_get(mapallmem, addr);\n"
        "    // 无标记（musl 捡回页/已释放）与 box64 内部保留页均拒绝；其余（ALLOCATED/MMAP/STACK/ELF/EXTERNAL）允许\n"
        "    if(flag == MEM_UNUSED || flag == MEM_RESERVED || flag == MEM_BOX)\n"
        "        return 0;\n"
        "    return flag;\n"
        "}\n"
        "\n"
    )
    anchor_a = "void freeProtection(uintptr_t addr, size_t size)\n{\n"
    apply_file(cmem, [(anchor_a, helper + anchor_a)])

    # ---- custommmap.c：extern 声明 + munmap 入口守卫 ----
    mmapf = os.path.join(root, "src", "custommmap.c")
    if not os.path.isfile(mmapf):
        fail(f"文件不存在: {mmapf}")
    anchor_b = "int InternalMunmap(void* addr, unsigned long length);\n"
    decl_b = anchor_b + "uintptr_t box_guest_mapping_flag(uintptr_t addr);   // BOX64-BUILD: munmap-guard\n"
    anchor_c = (
        "EXPORT int munmap(void* addr, unsigned long length)\n"
        "{\n"
        "    int ret = InternalMunmap(addr, length);\n"
    )
    repl_c = (
        "EXPORT int munmap(void* addr, unsigned long length)\n"
        "{\n"
        "    // BOX64-BUILD: munmap-guard 拆前校验标记，拒拆 box64 内部/无标记页（B-12：guest munmap 拆 p_blocks）\n"
        "    if(!box_guest_mapping_flag((uintptr_t)addr)) {\n"
        "        fprintf(stderr, \"P12GUARD skip munmap %p len=%lu\\n\", addr, length);\n"
        "        return 0;\n"
        "    }\n"
        "    int ret = InternalMunmap(addr, length);\n"
    )
    # 还需 <stdio.h>（fprintf）
    anchor_d = "#include <stdint.h>\n"
    decl_d = "#include <stdio.h>   // BOX64-BUILD: munmap-guard\n" + anchor_d
    apply_file(mmapf, [(anchor_b, decl_b), (anchor_c, repl_c), (anchor_d, decl_d)])
    return 0

if __name__ == "__main__":
    sys.exit(main())
