#!/usr/bin/env python3
"""pthread_join 失败日志插桩（steam Unloading 卡死排查，2026-09）：

现象：steamcmd 登录成功后 Unloading 阶段无限重试 join CJobMgr 工作线程，
反复打印 "Work thread ... is marked exited, but we could not immediately
join"，进程永不退出（timeout 才被杀）。反汇编 steamclient.so 确认其 timed
join 即 PLT pthread_join（失败/超时重试），故需要在 box64 侧记录真实的
join 错误码（EINVAL=detached / ESRCH=无效线程等）以定位根因。

改动：
1. wrappedlibpthread_private.h: GO(pthread_join) -> GOM（挂接 my_pthread_join，
   签名用已生成的 iFEpp，pthread_t 与 void* 同宽直接转换，避免新增类型 token）
2. static_threads.h: 声明 my_pthread_join（wrappedlibpthread.c 可见）
3. threads.c: 在 my_pthread_create 之后插入 my_pthread_join，
   失败时 printf_log(LOG_INFO) 打印 thread 指针与 strerror。

用法: patch_joinlog.py <box64源码目录>
幂等: 检测哨兵 BOX64-BUILD: joinlog 则跳过。
"""
import os
import sys

SENTINEL = "BOX64-BUILD: joinlog"

JOBS = [
    ("src/libtools/threads.c", [
        # strerror 需要 string.h（原文件未直接包含）
        (
            "#include <stdio.h>\n#include <stdlib.h>\n",
            "#include <stdio.h>\n#include <stdlib.h>\n#include <string.h>\n",
        ),
    ]),
    ("src/wrapped/wrappedlibpthread_private.h", [
        (
            "GO(pthread_join, iFLp)\n",
            "GOM(pthread_join, iFEpp)\n",
        ),
    ]),
    ("src/libtools/static_threads.h", [
        (
            "int my_pthread_create(x64emu_t *emu, void* t, void* attr, void* start_routine, void* arg);\n",
            "int my_pthread_create(x64emu_t *emu, void* t, void* attr, void* start_routine, void* arg);\n"
            "int my_pthread_join(x64emu_t* emu, void* thread, void** retval);\n",
        ),
    ]),
    ("src/libtools/threads.c", [
        (
            "\treturn pthread_create((pthread_t*)t, PTHREAD_ATTR(attr),\n"
            "\t\tpthread_routine, et);\n"
            "\t// no need too unalign for attr, it's const\n"
            "}\n",
            "\treturn pthread_create((pthread_t*)t, PTHREAD_ATTR(attr),\n"
            "\t\tpthread_routine, et);\n"
            "\t// no need too unalign for attr, it's const\n"
            "}\n"
            "\n"
            "// " + SENTINEL + ": 诊断插桩，记录 guest pthread_join 失败错误码\n"
            "EXPORT int my_pthread_join(x64emu_t* emu, void* thread, void** retval)\n"
            "{\n"
            "\t(void)emu;\n"
            "\tint ret = pthread_join((pthread_t)thread, retval);\n"
            "\tif(ret)\n"
            "\t\tprintf_log(LOG_INFO, \"my_pthread_join(%p) failed, ret=%d (%s)\\n\",\n"
            "\t\t\tthread, ret, strerror(ret));\n"
            "\treturn ret;\n"
            "}\n",
        ),
    ]),
]


def patch(srcdir: str) -> int:
    # 幂等：threads.c 已含哨兵则整体跳过
    tc = os.path.join(srcdir, "src/libtools/threads.c")
    with open(tc, "r") as f:
        if SENTINEL in f.read():
            print("patch_joinlog: 已应用，跳过")
            return 0
    for rel, changes in JOBS:
        path = os.path.join(srcdir, rel)
        with open(path, "r") as f:
            data = f.read()
        for old, new in changes:
            c = data.count(old)
            if c != 1:
                print(f"patch_joinlog: {rel} 锚点出现 {c} 次（需为1）: {old[:70]!r}",
                      file=sys.stderr)
                return 2
            data = data.replace(old, new, 1)
        with open(path, "w") as f:
            f.write(data)
        print(f"patch_joinlog: {rel} OK")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("用法: patch_joinlog.py <box64源码目录>", file=sys.stderr)
        sys.exit(1)
    sys.exit(patch(sys.argv[1]))
