#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批次2 B2 性能 patch：maps 重读粒度优化。
背景：AddNeededLibInternal（librarian.c）每加载一个依赖库就 box64_mapclean=0，
下次 loadProtectionFromMap（elfloader.c AllocLoadElfMemory 入口）必全量重读
/proc/self/maps —— proot 下几十 ms/次，程序加载几十个 .so 累计秒级开销；
且「program break not found」告警随之反复出现（B-08）。
改法：
  1) librarian.c 不再清 box64_mapclean：新 .so 由 AllocLoadElfMemory 内部
     mmap 且 box64 自行标记 mapallmem，无需全量重读；
     初始全量读保留（core.c/init_custommem 启动路径 mapclean=0 时自然执行）。
  2) custommem.c mmap 失败重试三处（注释写 reload 但被 mapclean 快路径跳过）
     前强制 box64_mapclean=0，补上正确性兜底。
  3) pbrk 告警只报一次，消除 B-08 反复噪声。
哨兵：BOX64-BUILD: b2-maps
用法：python3 patch_b2maps.py <box64源码目录>
"""
import sys
import os

SENTINEL = "BOX64-BUILD: b2-maps"

def fail(msg):
    print(f"[patch_b2maps] 失败: {msg}", file=sys.stderr)
    sys.exit(1)

def apply_file(path, jobs):
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    if SENTINEL in src:
        print(f"[patch_b2maps] {os.path.basename(path)} 已应用过，跳过")
        return
    for anchor, repl in jobs:
        n = src.count(anchor)
        if n != 1:
            fail(f"{os.path.basename(path)} 锚点命中 {n} 次（期望 1）:\n{anchor[:80]}")
        src = src.replace(anchor, repl, 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print(f"[patch_b2maps] {os.path.basename(path)} 应用 {len(jobs)} 处")

def main():
    if len(sys.argv) != 2:
        fail("用法: patch_b2maps.py <box64目录>")
    root = sys.argv[1]

    # ---- librarian.c：库加载不再清 mapclean ----
    librarian = os.path.join(root, "src", "librarian", "librarian.c")
    if not os.path.isfile(librarian):
        fail(f"文件不存在: {librarian}")
    apply_file(librarian, [(
        "    if(!needed) // no needed libs, no problems\n"
        "        return 0;\n"
        "    box64_mapclean = 0;\n"
        "    int ret = 0;\n",
        "    if(!needed) // no needed libs, no problems\n"
        "        return 0;\n"
        "    // BOX64-BUILD: b2-maps 不再清 box64_mapclean —— 新 .so 经 AllocLoadElfMemory\n"
        "    // 自行 mmap 并标记 mapallmem，免去每依赖库全量重读 /proc/self/maps（proot 高开销）\n"
        "    int ret = 0;\n",
    )])

    # ---- custommem.c：三处失败重试强制重读 + pbrk 告警只报一次 ----
    cmem = os.path.join(root, "src", "custommem.c")
    if not os.path.isfile(cmem):
        fail(f"文件不存在: {cmem}")
    jobs = [
        # 1) 32bit 重试路径（下一行 find31bitBlockNearHint 区分）
        (
            "        loadProtectionFromMap();    // reload map, because something went wrong previously\n"
            "        addr = find31bitBlockNearHint(old_addr, length, 0); // is this the best way?\n",
            "        box64_mapclean = 0;    // BOX64-BUILD: b2-maps 失败重试前强制全量重读\n"
            "        loadProtectionFromMap();    // reload map, because something went wrong previously\n"
            "        addr = find31bitBlockNearHint(old_addr, length, 0); // is this the best way?\n",
        ),
        # 2) 47bit 重试路径（该 load 行单空格注释格式，唯一样本）
        (
            "        loadProtectionFromMap(); // reload map, because something went wrong previously\n"
            "        addr = find47bitBlock(length);\n",
            "        box64_mapclean = 0; // BOX64-BUILD: b2-maps 失败重试前强制全量重读\n"
            "        loadProtectionFromMap(); // reload map, because something went wrong previously\n"
            "        addr = find47bitBlock(length);\n",
        ),
        # 3) 47bit hint 重试路径（下一行 find47bitBlockNearHint 区分）
        (
            "        loadProtectionFromMap();    // reload map, because something went wrong previously\n"
            "        addr = find47bitBlockNearHint(old_addr, length, 0); // is this the best way?\n",
            "        box64_mapclean = 0;    // BOX64-BUILD: b2-maps 失败重试前强制全量重读\n"
            "        loadProtectionFromMap();    // reload map, because something went wrong previously\n"
            "        addr = find47bitBlockNearHint(old_addr, length, 0); // is this the best way?\n",
        ),
        # 4) pbrk 告警去重（B-08 反复噪声）
        (
            "    if(!pbrk) {\n"
            "        if (!box64_unittest_mode)\n"
            "            printf_log(LOG_INFO, \"Warning, program break not found\\n\");\n"
            "        if(cur_brk) pbrk = *cur_brk;    // approximate is better than nothing\n"
            "    }\n",
            "    if(!pbrk) {\n"
            "        if (!box64_unittest_mode) {\n"
            "            static int shown_brk = 0;    // BOX64-BUILD: b2-maps program break 告警只报一次\n"
            "            if(!shown_brk) {\n"
            "                shown_brk = 1;\n"
            "                printf_log(LOG_INFO, \"Warning, program break not found\\n\");\n"
            "            }\n"
            "        }\n"
            "        if(cur_brk) pbrk = *cur_brk;    // approximate is better than nothing\n"
            "    }\n",
        ),
    ]
    apply_file(cmem, jobs)
    print("[patch_b2maps] 完成")

if __name__ == "__main__":
    main()
