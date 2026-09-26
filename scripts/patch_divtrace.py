#!/usr/bin/env python3
"""诊断用打点 patch：
1) 解释器 x64run.c 的 DIV case：打印调用参数与调用后寄存器；
2) x64primop.c 的 div64 函数内部：打印 s/dvd/商/溢出分支/写回前后。
仅用于定位 divq hi!=0 不写回的根因，定位后应从 patch 链移除。
用法: patch_divtrace.py <box64源码目录>
"""
import sys
from pathlib import Path

MARK = "[DIVT]"

# ---------- 1) x64run.c 解释器 DIV case ----------
RUN_ANCHOR = """                    case 6:                 /* DIV Ed */
                        #ifndef TEST_INTERPRETER
                        if(!ED->q[0])
                            EmitDiv0(emu, (void*)R_RIP, 1);
                        #endif
                        div64(emu, ED->q[0]);
                        break;
"""

RUN_REPL = """                    case 6:                 /* DIV Ed */
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

# ---------- 2) x64primop.c div64 函数内部 ----------
PRIMOP_INC_ANCHOR = "#include <stdlib.h>\n"
PRIMOP_INC_REPL = "#include <stdlib.h>\n#include <stdio.h>\n"

DIV64_ANCHOR = (
    "\tdvd = (((__int128)R_RDX) << 64) | R_RAX;\n"
    "\tif (s == 0) {\n"
    "\t\tINTR_RAISE_DIV0(emu);\n"
    "\t\treturn;\n"
    "\t}\n"
    "\tdiv = dvd / (unsigned __int128)s;\n"
    "\tmod = dvd % (unsigned __int128)s;\n"
    "\tif (div > 0xffffffffffffffffL) {\n"
    "\t\tINTR_RAISE_DIV0(emu);\n"
    "\t\treturn;\n"
    "\t}\n"
    "\n"
    "\tR_RAX = (uint64_t)div;\n"
    "\tR_RDX = (uint64_t)mod;\n"
    "}"
)

DIV64_REPL = (
    "\tdvd = (((__int128)R_RDX) << 64) | R_RAX;\n"
    "\tfprintf(stderr, \"[DIV64] s=%llx dvd_hi=%llx dvd_lo=%llx\\n\",\n"
    "\t\t(unsigned long long)s, (unsigned long long)((unsigned __int128)dvd >> 64),\n"
    "\t\t(unsigned long long)(unsigned __int128)dvd);\n"
    "\tif (s == 0) {\n"
    "\t\tfprintf(stderr, \"[DIV64] DIV0_s_zero\\n\");\n"
    "\t\tINTR_RAISE_DIV0(emu);\n"
    "\t\treturn;\n"
    "\t}\n"
    "\tdiv = dvd / (unsigned __int128)s;\n"
    "\tmod = dvd % (unsigned __int128)s;\n"
    "\tfprintf(stderr, \"[DIV64] quot_hi=%llx quot_lo=%llx mod=%llx\\n\",\n"
    "\t\t(unsigned long long)((unsigned __int128)div >> 64),\n"
    "\t\t(unsigned long long)(unsigned __int128)div,\n"
    "\t\t(unsigned long long)(unsigned __int128)mod);\n"
    "\tif (div > 0xffffffffffffffffL) {\n"
    "\t\tfprintf(stderr, \"[DIV64] DIV0_overflow div_hi=%llx\\n\",\n"
    "\t\t\t(unsigned long long)((unsigned __int128)div >> 64));\n"
    "\t\tINTR_RAISE_DIV0(emu);\n"
    "\t\treturn;\n"
    "\t}\n"
    "\tfprintf(stderr, \"[DIV64] OUT_pre rax=%llx rdx=%llx\\n\",\n"
    "\t\t(unsigned long long)R_RAX, (unsigned long long)R_RDX);\n"
    "\tR_RAX = (uint64_t)div;\n"
    "\tR_RDX = (uint64_t)mod;\n"
    "\tfprintf(stderr, \"[DIV64] OUT_post rax=%llx rdx=%llx\\n\",\n"
    "\t\t(unsigned long long)R_RAX, (unsigned long long)R_RDX);\n"
    "}"
)


def patch_file(path: Path, anchor: str, repl: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    if MARK in text and label == "run":
        print(f"patch_divtrace: {path} 已包含 {label} 打点，跳过（幂等）")
        return
    if "[DIV64]" in text and label == "primop":
        print(f"patch_divtrace: {path} 已包含 {label} 打点，跳过（幂等）")
        return
    if anchor not in text:
        print(f"错误: {label} 锚点未找到于 {path}", file=sys.stderr)
        sys.exit(1)
    path.write_text(text.replace(anchor, repl, 1), encoding="utf-8")
    print(f"patch_divtrace: 已在 {path} 插入 {label} 打点")


def main() -> int:
    if len(sys.argv) != 2:
        print(f"用法: {sys.argv[0]} <box64源码目录>", file=sys.stderr)
        return 2
    root = Path(sys.argv[1])
    run_c = root / "src/emu/x64run.c"
    primop_c = root / "src/emu/x64primop.c"
    for p in (run_c, primop_c):
        if not p.exists():
            print(f"错误: {p} 不存在", file=sys.stderr)
            return 1
    patch_file(run_c, RUN_ANCHOR, RUN_REPL, "run")
    patch_file(primop_c, DIV64_ANCHOR, DIV64_REPL, "primop")
    # primop 需要 stdio；放在最后统一判断（若 [DIV64] 已插入说明未打过）
    t = primop_c.read_text(encoding="utf-8")
    if "[DIV64]" in t and "#include <stdio.h>" not in t:
        if PRIMOP_INC_ANCHOR not in t:
            print("错误: stdio 锚点未找到", file=sys.stderr)
            return 1
        primop_c.write_text(t.replace(PRIMOP_INC_ANCHOR, PRIMOP_INC_REPL, 1), encoding="utf-8")
        print(f"patch_divtrace: 已在 {primop_c} 补 #include <stdio.h>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
