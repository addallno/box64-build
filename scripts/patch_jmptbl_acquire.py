#!/usr/bin/env python3
"""patch_jmptbl_acquire.py — 跳转表读侧 acquire 屏障（B-12 内存序修复）

来源：subagent 审查报告1 P0 级嫌疑 + 本地代码核对（2026-09-27）。

问题（ARM 弱序，x86 TSO 假设不成立）：
- 写侧发布链（FillBlock64 → addJumpTableIfDefault64 / native_lock_storeifref2）
  是 release 语义（dmb ish + stlxr/casal）✓；
- 读侧 getDBBlock / getJumpAddress64 / getDBSize 用普通 load，
  JIT 跨块跳转（jump_to_next / indirect_lookup / callret 出块三处）
  用 LDRx_U12 / LDRx_REG_LSL3 普通 load —— 读到新 entry 后
  仍可能观察到旧 code / 旧 db 指针 / 半初始化子表 → 跳进未同步代码。
- 修复：
  * C 慢路径：entry 读改 __atomic_load_n(..., __ATOMIC_ACQUIRE)
    （编译器生成 ldar）；
  * JIT emit：读表 load 后插 DMB_ISHLD()（emitter 无 LDAR 宏，
    且目标机 Cortex-A53 无 RCpc，不能用 LDAPR；DMB_ISHLD 与
    STRONGMEM 的 SMREAD 同款，编码已验证）。

哨兵：BOX64-BUILD: jmptbl-acquire（幂等）
用法：python3 patch_jmptbl_acquire.py <box64源码目录>
退出码：0=成功/已幂等，1=锚点缺失或不唯一
"""
import sys
import os

SENTINEL = "BOX64-BUILD: jmptbl-acquire"

# (相对路径, 锚文本, 替换文本, 期望次数)
# 每个锚在文件中必须恰好出现 expected 次
JOBS = [
    # ---- 1) custommem.c getDBBlock：entry 读改 acquire ----
    (
        "src/custommem.c",
        """    #ifdef JMPTABL_SHIFT4
    uintptr_t ret = (uintptr_t)box64_jmptbl3[idx3][idx2][idx1][idx0];
    #else
    uintptr_t ret = (uintptr_t)box64_jmptbl2[idx2][idx1][idx0];
    #endif
""",
        """    #ifdef JMPTABL_SHIFT4
    // BOX64-BUILD: jmptbl-acquire 读跳转表 entry 用 acquire，配对写侧 release CAS
    uintptr_t ret = (uintptr_t)__atomic_load_n(&box64_jmptbl3[idx3][idx2][idx1][idx0], __ATOMIC_ACQUIRE);
    #else
    uintptr_t ret = (uintptr_t)__atomic_load_n(&box64_jmptbl2[idx2][idx1][idx0], __ATOMIC_ACQUIRE);
    #endif
""",
        1,
    ),
    # ---- 2) custommem.c getJumpAddress64：同上 ----
    (
        "src/custommem.c",
        """    #ifdef JMPTABL_SHIFT4
    return (uintptr_t)box64_jmptbl3[idx3][idx2][idx1][idx0];
    #else
    return (uintptr_t)box64_jmptbl2[idx2][idx1][idx0];
    #endif
}
""",
        """    #ifdef JMPTABL_SHIFT4
    // BOX64-BUILD: jmptbl-acquire
    return (uintptr_t)__atomic_load_n(&box64_jmptbl3[idx3][idx2][idx1][idx0], __ATOMIC_ACQUIRE);
    #else
    return (uintptr_t)__atomic_load_n(&box64_jmptbl2[idx2][idx1][idx0], __ATOMIC_ACQUIRE);
    #endif
}
""",
        1,
    ),
    # ---- 3) custommem.c getDBSize：entry 读改 acquire（后读 db 指针） ----
    (
        "src/custommem.c",
        """    #ifdef JMPTABL_START4
    *db = *(dynablock_t**)(box64_jmptbl3[idx3][idx2][idx1][idx0]- sizeof(void*));
    #else
    *db = *(dynablock_t**)(box64_jmptbl2[idx2][idx1][idx0]- sizeof(void*));
    #endif
""",
        """    #ifdef JMPTABL_START4
    // BOX64-BUILD: jmptbl-acquire entry acquire 后再取 -8 处的 db 指针
    {
        uintptr_t entry = (uintptr_t)__atomic_load_n(&box64_jmptbl3[idx3][idx2][idx1][idx0], __ATOMIC_ACQUIRE);
        *db = *(dynablock_t**)(entry - sizeof(void*));
    }
    #else
    {
        uintptr_t entry = (uintptr_t)__atomic_load_n(&box64_jmptbl2[idx2][idx1][idx0], __ATOMIC_ACQUIRE);
        *db = *(dynablock_t**)(entry - sizeof(void*));
    }
    #endif
""",
        1,
    ),
    # ---- 4) helper.c jump_to_next 直接表跳转：LDR 后 DMB_ISHLD ----
    (
        "src/dynarec/arm64/dynarec_arm64_helper.c",
        """        TABLE64_(x3, p);
        GETIP_(ip);
        LDRx_U12(x2, x3, 0);
        dest = x2;
""",
        """        TABLE64_(x3, p);
        GETIP_(ip);
        LDRx_U12(x2, x3, 0);
        DMB_ISHLD(); // BOX64-BUILD: jmptbl-acquire 读表后 acquire，再 BR/BLR
        dest = x2;
""",
        1,
    ),
    # ---- 5) helper.c indirect_lookup：每级 LDR 后 DMB_ISHLD（整函数替换） ----
    (
        "src/dynarec/arm64/dynarec_arm64_helper.c",
        """static int indirect_lookup(dynarec_arm_t* dyn, int ninst, int is32bits, int s1, int s2)
{
    MAYUSE(dyn);
    if (!is32bits) {
        // check higher 48bits
        LSRx_IMM(s1, xRIP, 48);
        intptr_t j64 = (intptr_t)dyn->jmp_next - (intptr_t)dyn->block;
        CBNZw(s1, j64);
        // load table
        if(dyn->need_reloc) {
            TABLE64C(s2, const_jmptbl48);
        } else {
            MOV64x(s2, getConst(const_jmptbl48));    // this is a static value, so will be a low address
        }
        #ifdef JMPTABL_SHIFT4
        UBFXx(s1, xRIP, JMPTABL_START3, JMPTABL_SHIFT3);
        LDRx_REG_LSL3(s2, s2, s1);
        #endif
        UBFXx(s1, xRIP, JMPTABL_START2, JMPTABL_SHIFT2);
        LDRx_REG_LSL3(s2, s2, s1);
    } else {
        // check higher 32bits disabled
        // LSRx_IMM(s1, xRIP, 32);
        // intptr_t j64 = (intptr_t)dyn->jmp_next - (intptr_t)dyn->block;
        // CBNZw(s1, j64);
        // load table
        TABLE64C(s2, const_jmptbl32);
        #ifdef JMPTABL_SHIFT4
        UBFXx(s1, xRIP, JMPTABL_START2, JMPTABL_SHIFT2);
        LDRx_REG_LSL3(s2, s2, s1);
        #endif
    }
    UBFXx(s1, xRIP, JMPTABL_START1, JMPTABL_SHIFT1);
    LDRx_REG_LSL3(s2, s2, s1);
    UBFXx(s1, xRIP, JMPTABL_START0, JMPTABL_SHIFT0);
    LDRx_REG_LSL3(s1, s2, s1);
    return s1;
}
""",
        """static int indirect_lookup(dynarec_arm_t* dyn, int ninst, int is32bits, int s1, int s2)
{
    MAYUSE(dyn);
    if (!is32bits) {
        // check higher 48bits
        LSRx_IMM(s1, xRIP, 48);
        intptr_t j64 = (intptr_t)dyn->jmp_next - (intptr_t)dyn->block;
        CBNZw(s1, j64);
        // load table
        if(dyn->need_reloc) {
            TABLE64C(s2, const_jmptbl48);
        } else {
            MOV64x(s2, getConst(const_jmptbl48));    // this is a static value, so will be a low address
        }
        #ifdef JMPTABL_SHIFT4
        UBFXx(s1, xRIP, JMPTABL_START3, JMPTABL_SHIFT3);
        LDRx_REG_LSL3(s2, s2, s1);
        DMB_ISHLD(); // BOX64-BUILD: jmptbl-acquire 层级表读取同样需要 acquire
        #endif
        UBFXx(s1, xRIP, JMPTABL_START2, JMPTABL_SHIFT2);
        LDRx_REG_LSL3(s2, s2, s1);
        DMB_ISHLD(); // BOX64-BUILD: jmptbl-acquire
    } else {
        // check higher 32bits disabled
        // LSRx_IMM(s1, xRIP, 32);
        // intptr_t j64 = (intptr_t)dyn->jmp_next - (intptr_t)dyn->block;
        // CBNZw(s1, j64);
        // load table
        TABLE64C(s2, const_jmptbl32);
        #ifdef JMPTABL_SHIFT4
        UBFXx(s1, xRIP, JMPTABL_START2, JMPTABL_SHIFT2);
        LDRx_REG_LSL3(s2, s2, s1);
        DMB_ISHLD(); // BOX64-BUILD: jmptbl-acquire
        #endif
    }
    UBFXx(s1, xRIP, JMPTABL_START1, JMPTABL_SHIFT1);
    LDRx_REG_LSL3(s2, s2, s1);
    DMB_ISHLD(); // BOX64-BUILD: jmptbl-acquire
    UBFXx(s1, xRIP, JMPTABL_START0, JMPTABL_SHIFT0);
    LDRx_REG_LSL3(s1, s2, s1);
    DMB_ISHLD(); // BOX64-BUILD: jmptbl-acquire 末级 entry 读取，随后 BR/BLR
    return s1;
}
""",
        1,
    ),
    # ---- 6) 00.c callret 出块（RELOC+MOV64 分支）：LDR 后 DMB ----
    (
        "src/dynarec/arm64/dynarec_arm64_00.c",
        """                        } else {
                            MOV64x(x4, j64);
                        }
                        LDRx_U12(x4, x4, 0);
                        BR(x4);
""",
        """                        } else {
                            MOV64x(x4, j64);
                        }
                        LDRx_U12(x4, x4, 0);
                        DMB_ISHLD(); // BOX64-BUILD: jmptbl-acquire
                        BR(x4);
""",
        1,
    ),
    # ---- 7) 00.c callret 出块（RetEndBlock 24 空格缩进） ----
    (
        "src/dynarec/arm64/dynarec_arm64_00.c",
                        """                        if(dyn->need_reloc) AddRelocTable64RetEndBlock(dyn, ninst, addr, STEP);
                        TABLE64_(x4, j64);
                        LDRx_U12(x4, x4, 0);
                        BR(x4);
""",
                        """                        if(dyn->need_reloc) AddRelocTable64RetEndBlock(dyn, ninst, addr, STEP);
                        TABLE64_(x4, j64);
                        LDRx_U12(x4, x4, 0);
                        DMB_ISHLD(); // BOX64-BUILD: jmptbl-acquire
                        BR(x4);
""",
        1,
    ),
    # ---- 8) 00.c callret 出块（RetEndBlock 28 空格缩进） ----
    (
        "src/dynarec/arm64/dynarec_arm64_00.c",
                        """                            if(dyn->need_reloc) AddRelocTable64RetEndBlock(dyn, ninst, addr, STEP);
                            TABLE64_(x4, j64);
                            LDRx_U12(x4, x4, 0);
                            BR(x4);
""",
                        """                            if(dyn->need_reloc) AddRelocTable64RetEndBlock(dyn, ninst, addr, STEP);
                            TABLE64_(x4, j64);
                            LDRx_U12(x4, x4, 0);
                            DMB_ISHLD(); // BOX64-BUILD: jmptbl-acquire
                            BR(x4);
""",
        1,
    ),
]


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    root = sys.argv[1]
    if SENTINEL in open(os.path.join(root, "src/custommem.c"), encoding="utf-8").read():
        print("已应用过（幂等），跳过")
        return 0

    applied = 0
    failures = []
    for rel, old, new, expect in JOBS:
        path = os.path.join(root, rel)
        with open(path, encoding="utf-8") as f:
            src = f.read()
        cnt = src.count(old)
        if cnt != expect:
            failures.append(f"{rel}: 锚点出现 {cnt} 次（期望 {expect}）")
            continue
        src = src.replace(old, new)
        with open(path, "w", encoding="utf-8") as f:
            f.write(src)
        applied += 1
        print(f"[应用] {rel}: {len(old.splitlines())} 行块")

    if failures:
        for m in failures:
            print(f"[失败] {m}", file=sys.stderr)
        return 1
    print(f"完成：{applied}/{len(JOBS)} 处补丁")
    return 0


if __name__ == "__main__":
    sys.exit(main())
