#!/usr/bin/env python3
"""P0 确定性 bug 修复（subagent 静态审查结论，2026-09）：

1. arm64_lock.S arm64_atomic_storeifref_d：casal 不更新 NZCV，bne 排在 cmp 之前
   读到残留 flags → 返回值不可靠。交换 cmp/bne 顺序（对照 64 位正确版）。
   附带 P0-3b：LSE storeifref 与 storeifref_d 失败路径返回期望值(x3/w3)而非
   casal 实际 old → 调用者 (ret==ref) 误判成功；改返回 casal 结果(x2/w2)。
   （注：远端 Cortex-A53 无 LSE 走非 LSE 路径不受影响，此为通用正确性修复）
2. arm64_lock.S arm64_lock_storeb/store/store_dd：`str; dmb` 屏障在 store 之后，
   缺前向发布语义；读侧 GET_PROT 是 ldaxrb(acquire) 配对 → 改 release store
   （前导 dmb ish + stlrb/stlr，语义为原实现超集）。
3. arm64_lock.S arm64_lock_write_dq：stlxp 无配对 ldxp（无独占监视器）且失败不
   重试 → 静默丢 128 位写。补 ldxp + cbnz 重试环。
4. wrappedlibc.c my_epoll_wait/my_epoll_pwait/my_epoll_pwait2：guest maxevents
   直接开 VLA（<=0 UB、超大爆栈）→ 入口加界检查返回 EINVAL。
5. wrappedlibc.c my_epoll_pwait2 回退路径：`tmp>1<<31` 中 1<<31 是 int UB=
   INT_MIN → 条件恒真 → tout=INT_MIN 负值 → 内核当无限阻塞 → 改显式饱和
   0x7fffffff 并对负值钳 0。

用法: patch_p0fixes.py <box64源码目录>
幂等: 检测哨兵 BOX64-BUILD: p0fixes 则跳过。
"""
import os
import sys

SENTINEL = "BOX64-BUILD: p0fixes"

# (相对路径, [ (old, new), ... ])——每个锚点 count 必须为 1
JOBS = [
    ("src/dynarec/arm64/arm64_lock.S", [
        # 1. storeifref_d: casal 后先 cmp 再 bne
        (
            "    mov     w3, w2\n"
            "    casal   w2, w1, [x0]\n"
            "    bne     2f\n"
            "    cmp     w2, w3\n",
            "    mov     w3, w2\n"
            "    casal   w2, w1, [x0]\n"
            "    cmp     w2, w3      // " + SENTINEL + ": casal 不更新 flags，bne 必须在 cmp 后\n"
            "    bne     2f\n",
        ),
        # 2. storeb: release store
        (
            "arm64_lock_storeb:\n"
            "    strb    w1, [x0]\n"
            "    dmb     ish\n"
            "    ret\n",
            "arm64_lock_storeb:\n"
            "    dmb     ish\n"
            "    stlrb   w1, [x0]    // " + SENTINEL + ": release（配对读侧 acquire）\n"
            "    ret\n",
        ),
        # 3. store: release store
        (
            "arm64_lock_store:\n"
            "    str     w1, [x0]\n"
            "    dmb     ish\n"
            "    ret\n",
            "arm64_lock_store:\n"
            "    dmb     ish\n"
            "    stlr    w1, [x0]    // " + SENTINEL + ": release\n"
            "    ret\n",
        ),
        # 4. store_dd: release store
        (
            "arm64_lock_store_dd:\n"
            "    str     x1, [x0]\n"
            "    dmb     ish\n"
            "    ret\n",
            "arm64_lock_store_dd:\n"
            "    dmb     ish\n"
            "    stlr    x1, [x0]    // " + SENTINEL + ": release\n"
            "    ret\n",
        ),
        # 5. write_dq: 补配对 ldxp + stlxp 失败重试
        (
            "    // r0 needs to be aligned\n"
            "    stlxp   w3, x0, x1, [x2]\n"
            "    mov     w0, w3\n"
            "    dmb     ish\n"
            "    ret\n",
            "    // r0 needs to be aligned\n"
            "    dmb     ish\n"
            "1:\n"
            "    ldxp    x4, x5, [x2]\n"
            "    stlxp   w3, x0, x1, [x2]\n"
            "    cbnz    w3, 1b      // " + SENTINEL + ": stlxp 需配对 ldxp，失败重试\n"
            "    mov     w0, w3\n"
            "    dmb     ish\n"
            "    ret\n",
        ),
        # 6. P0-3b: LSE storeifref_d 失败路径返回期望值 w3 而非 casal 实际值 w2
        #    → 调用者 (ret==ref) 误判成功（非 LSE 版返回的是 ldaxr 实际 old）。
        #    锚点含修复1后的 cmp/bne 文本，保证与非 LSE 版区分。
        (
            "    cmp     w2, w3      // " + SENTINEL + ": casal 不更新 flags，bne 必须在 cmp 后\n"
            "    bne     2f\n"
            "    mov     w0, w1\n"
            "    ret\n"
            "2:\n"
            "    mov     w0, w3\n"
            "    ret\n",
            "    cmp     w2, w3      // " + SENTINEL + ": casal 不更新 flags，bne 必须在 cmp 后\n"
            "    bne     2f\n"
            "    mov     w0, w1\n"
            "    ret\n"
            "2:\n"
            "    mov     w0, w2      // " + SENTINEL + ": 返回 casal 实际 old，勿用期望值 w3\n"
            "    ret\n",
        ),
        # 7. P0-3b: LSE storeifref(64位) 同款失败路径返回 x3(期望) → 应返回 x2(实际 old)
        (
            "    mov     x3, x2\n"
            "    casal   x2, x1, [x0]\n"
            "    cmp     x2, x3\n"
            "    bne     2f\n"
            "    mov     x0, x1\n"
            "    ret\n"
            "2:\n"
            "    mov     x0, x3\n"
            "    ret\n",
            "    mov     x3, x2\n"
            "    casal   x2, x1, [x0]\n"
            "    cmp     x2, x3\n"
            "    bne     2f\n"
            "    mov     x0, x1\n"
            "    ret\n"
            "2:\n"
            "    mov     x0, x2      // " + SENTINEL + ": 返回 casal 实际 old，勿用期望值 x3\n"
            "    ret\n",
        ),
    ]),
    ("src/wrapped/wrappedlibc.c", [
        # 4. my_epoll_wait VLA 界检查
        (
            "EXPORT int32_t my_epoll_wait(x64emu_t* emu, int32_t epfd, void* events, int32_t maxevents, int32_t timeout)\n"
            "{\n"
            "    struct epoll_event _events[maxevents];\n",
            "EXPORT int32_t my_epoll_wait(x64emu_t* emu, int32_t epfd, void* events, int32_t maxevents, int32_t timeout)\n"
            "{\n"
            "    if(maxevents<=0 || maxevents>4096) { errno = EINVAL; return -1; } // " + SENTINEL + ": VLA 界检查\n"
            "    struct epoll_event _events[maxevents];\n",
        ),
        # 4. my_epoll_pwait VLA 界检查
        (
            "EXPORT int32_t my_epoll_pwait(x64emu_t* emu, int32_t epfd, void* events, int32_t maxevents, int32_t timeout, const sigset_t *sigmask)\n"
            "{\n"
            "    struct epoll_event _events[maxevents];\n",
            "EXPORT int32_t my_epoll_pwait(x64emu_t* emu, int32_t epfd, void* events, int32_t maxevents, int32_t timeout, const sigset_t *sigmask)\n"
            "{\n"
            "    if(maxevents<=0 || maxevents>4096) { errno = EINVAL; return -1; } // " + SENTINEL + ": VLA 界检查\n"
            "    struct epoll_event _events[maxevents];\n",
        ),
        # 4. my_epoll_pwait2 VLA 界检查
        (
            "EXPORT int32_t my_epoll_pwait2(x64emu_t* emu, int epfd, void* events, int maxevents, struct timespec *timeout, sigset_t * sigmask)\n"
            "{\n"
            "    struct epoll_event _events[maxevents];\n",
            "EXPORT int32_t my_epoll_pwait2(x64emu_t* emu, int epfd, void* events, int maxevents, struct timespec *timeout, sigset_t * sigmask)\n"
            "{\n"
            "    if(maxevents<=0 || maxevents>4096) { errno = EINVAL; return -1; } // " + SENTINEL + ": VLA 界检查\n"
            "    struct epoll_event _events[maxevents];\n",
        ),
        # 5. epoll_pwait2 超时溢出（1<<31 UB=INT_MIN → 恒真 → 永久阻塞）
        (
            "            int64_t tmp = (timeout->tv_nsec + timeout->tv_sec*1000000000LL)/1000000LL;\n"
            "            if(tmp>1<<31) tmp = 1<<31;\n",
            "            int64_t tmp = (timeout->tv_nsec + timeout->tv_sec*1000000000LL)/1000000LL;\n"
            "            if(tmp > 0x7fffffffLL) tmp = 0x7fffffffLL; else if(tmp < 0) tmp = 0; // " + SENTINEL + "\n",
        ),
    ]),
]


def patch(srcdir: str) -> int:
    for rel, repls in JOBS:
        path = os.path.join(srcdir, rel)
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        if SENTINEL in src:
            print(f"patch_p0fixes: 已应用过，跳过 ({rel})")
            continue
        for old, new in repls:
            c = src.count(old)
            if c != 1:
                print(f"patch_p0fixes: {rel} 锚点出现 {c} 次（需为1）: {old[:70]!r}",
                      file=sys.stderr)
                return 1
            src = src.replace(old, new, 1)
        with open(path, "w", encoding="utf-8") as f:
            f.write(src)
        print(f"patch_p0fixes: 已应用 {len(repls)} 处替换 -> {rel}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"用法: {sys.argv[0]} <box64源码目录>")
        sys.exit(2)
    sys.exit(patch(sys.argv[1]))
