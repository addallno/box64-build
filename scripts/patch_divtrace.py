#!/usr/bin/env python3
"""诊断用打点 patch：在解释器 x64run.c 的 DIV/IDIV case 打印参数与写回结果。
仅用于定位 divq hi!=0 不写回的根因，定位后应从 patch 链移除。
用法: patch_divtrace.py <box64源码目录>
"""
import sys
from pathlib import Path

MARK = "[DIVT]"

ANCHOR = """                    case 6:                 /* DIV Ed */
                        #ifndef TEST_INTERPRETER
                        if(!ED->q[0])
                            EmitDiv0(emu, (void*)R_RIP, 1);
                        #endif
                        div64(emu, ED->q[0]);
                        break;
"""

REPL = """                    case 6:                 /* DIV Ed */
                        #ifndef TEST_INTERPRETER
                        if(!ED->q[0])
                            EmitDiv0(emu, (void*)R_RIP, 1);
                        #endif
                        fprintf(stderr, "[DIVT] nextop=%02x rex_w=%d s=%llx rax=%llx rdx=%llx rip=%llx\\n",
                            nextop, rex.w, (unsigned long long)ED->q[0],
                            (unsigned long long)R_RAX, (unsigned long long)R_RDX,
                            (unsigned long long)R_RIP);
                        div64(emu, ED->q[0]);
                        fprintf(stderr, "[DIVT]-> rax=%llx rdx=%llx err=%d\\n",
                            (unsigned long long)R_RAX, (unsigned long long)R_RDX, emu->error);
                        break;
"""

def main() -> int:
    if len(sys.argv) != 2:
        print(f"用法: {sys.argv[0]} <box64源码目录>", file=sys.stderr)
        return 2
    target = Path(sys.argv[1]) / "src/emu/x64run.c"
    if not target.exists():
        print(f"错误: {target} 不存在", file=sys.stderr)
        return 1
    text = target.read_text(encoding="utf-8")
    if MARK in text:
        print(f"patch_divtrace: {target} 已包含打点，跳过（幂等）")
        return 0
    if ANCHOR not in text:
        print(f"错误: 锚点未找到于 {target}，patch 失败", file=sys.stderr)
        return 1
    target.write_text(text.replace(ANCHOR, REPL, 1), encoding="utf-8")
    print(f"patch_divtrace: 已在 {target} 插入 {MARK} 打点")
    return 0

if __name__ == "__main__":
    sys.exit(main())
