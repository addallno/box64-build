#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批次2-B5: ARM64 readFreq 的 cntfrq_el0 在部分容器/内核下读 0 →
box64 放弃硬件计数器，每次 guest RDTSC 走 CLOCK_MONOTONIC_COARSE（ms 级+syscall 开销）。
cntfrq==0 时改用 cntvct + 50ms 睡眠校准出近似频率，保住硬件计数路径。
幂等: BOX64-BUILD: b5tsc
"""
import sys

SENTINEL = "BOX64-BUILD: b5tsc"

OLD = '''static inline uint64_t readFreq()
{
    uint64_t val;
    asm volatile("mrs %0, cntfrq_el0"
                 : "=r"(val));
    return val;
}
'''

NEW = '''static inline uint64_t readFreq()
{
    uint64_t val;
    asm volatile("mrs %0, cntfrq_el0"
                 : "=r"(val));
    if(val)
        return val;
    // BOX64-BUILD: b5tsc cntfrq 读 0（EL0 trap/未实现）时用 cntvct+50ms 校准兜底
    struct timespec ts;
    ts.tv_sec = 0;
    ts.tv_nsec = 50000000;
    uint64_t cycles = readCycleCounter();
    nanosleep(&ts, NULL);
    cycles = readCycleCounter() - cycles;
    val = cycles * 20;
    return (val + 500000) / 1000000 * 1000000;
}
'''


def fail(msg):
    print("[ERROR] " + msg, file=sys.stderr)
    sys.exit(1)


def main():
    path = sys.argv[1] + "/src/os/freq_linux.c"
    with open(path, encoding="utf-8") as f:
        src = f.read()
    if SENTINEL in src:
        print("b5tsc 已打过，跳过")
        return
    n = src.count(OLD)
    if n != 1:
        fail("freq_linux.c ARM64 readFreq 锚点出现 %d 次(预期1)" % n)
    src = src.replace(OLD, NEW, 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print("b5tsc: freq_linux.c readFreq cntfrq==0 校准兜底已应用")


if __name__ == "__main__":
    main()
