#!/usr/bin/env python3
# 诊断用：给 x64Syscall_linux 的 clone(case 56) 加 [CLONE] 打点，
# 定位 guest pthread_create 走 clone(带新栈) 时 host 侧 EPERM 的来源。
# 幂等：源码已含 [CLONE] 标记则跳过。用法: patch_clonetrace.py <box64源码目录>
import sys
from pathlib import Path

MARK = "[CLONE]"

def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__ or "usage: patch_clonetrace.py <box64-src-dir>")
        return 2
    path = Path(sys.argv[1]) / "src/emu/x64syscall.c"
    src = path.read_text(encoding="utf-8")
    if MARK in src:
        print(f"[patch_clonetrace] 已存在 {MARK} 标记，跳过")
        return 0

    # 锚点1: x64Syscall_linux case56 入口（R_RDI 注释版，全文唯一）
    anchor1 = "            // so flags=R_RDI, stack=R_RSI, parent_tid=R_RDX, child_tid=R_R10, tls=R_R8\n"
    ins1 = (
        anchor1
        + '            fprintf(stderr, "[CLONE] in flags=0x%llx stack=0x%llx ptid=0x%llx ctid=0x%llx tls=0x%llx rip=0x%llx\\n",\n'
        + "                    (unsigned long long)R_RDI, (unsigned long long)R_RSI,\n"
        + "                    (unsigned long long)R_RDX, (unsigned long long)R_R10,\n"
        + "                    (unsigned long long)R_R8, (unsigned long long)R_RIP);\n"
    )
    if src.count(anchor1) != 1:
        print(f"[patch_clonetrace] 锚点1 匹配 {src.count(anchor1)} 次，放弃")
        return 1
    src = src.replace(anchor1, ins1, 1)

    # 锚点2: SETTLS 高地址 EPERM 分支
    anchor2 = (
        '                // Refer to https://github.com/torvalds/linux/blob/v7.2/arch/x86/kernel/process_64.c#L904\n'
        "                S_RAX = -EPERM;\n"
        "                break;\n"
        "            }\n"
        "            if((R_EDI&~0xff)==0x4100) {\n"
    )
    if src.count(anchor2) != 1:
        print(f"[patch_clonetrace] 锚点2 匹配 {src.count(anchor2)} 次，放弃")
        return 1
    src = src.replace(
        anchor2,
        anchor2.replace(
            "                S_RAX = -EPERM;\n",
            '                fprintf(stderr, "[CLONE] settls_high tls=0x%llx -> EPERM\\n", (unsigned long long)R_R8);\n'
            "                S_RAX = -EPERM;\n",
        ),
        1,
    )

    # 锚点3: clone(clone_fn_syscall...) 新栈分支返回后
    anchor3 = "                    int64_t ret = clone(clone_fn_syscall, (void*)((uintptr_t)mystack+1024*1024), flags, args, R_RDX, NULL, R_R10);\n"
    if src.count(anchor3) != 1:
        print(f"[patch_clonetrace] 锚点3 匹配 {src.count(anchor3)} 次，放弃")
        return 1
    src = src.replace(
        anchor3,
        anchor3
        + '                    fprintf(stderr, "[CLONE]->newstk ret=%lld errno=%d(%s) flags=0x%llx mystack=%p\\n",\n'
        + "                            (long long)ret, errno, strerror(errno), (unsigned long long)flags, mystack);\n",
        1,
    )

    # 锚点4: stack=0 透传分支
    anchor4 = "                    S_RAX = syscall(__NR_clone, R_RDI, R_RSI, R_RDX, R_R8, R_R10);    // invert R_R8/R_R10 on Aarch64 and most other\n"
    if src.count(anchor4) != 1:
        print(f"[patch_clonetrace] 锚点4 匹配 {src.count(anchor4)} 次，放弃")
        return 1
    src = src.replace(
        anchor4,
        anchor4
        + '                    fprintf(stderr, "[CLONE]->passthru ret=%lld\\n", (long long)S_RAX);\n',
        1,
    )

    path.write_text(src, encoding="utf-8")
    print("[patch_clonetrace] 已注入 4 处 [CLONE] 打点")
    return 0

if __name__ == "__main__":
    sys.exit(main())
