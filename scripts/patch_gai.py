#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
getaddrinfo/dn_comp 兼容 patch（bionic/musl 宿主适配，通用 GNU 程序适配）。

背景：x86 guest（glibc 语义）与宿主 libc（bionic/musl）的两处差异：
1. getaddrinfo：iputils 等 USE_IDN 程序按编译头传 AI_CANONNAME(0x2)|
   AI_IDN(0x40)|AI_CANONIDN(0x80)（ping 反汇编 hints.ai_flags=0xC2 证实，
   glibc/bionic netdb.h 同值），bionic 对未识别 flag 位返回 EAI_BADFLAGS，
   guest 收到 gai_strerror(EAI_BADFLAGS)="Invalid flags" 直接退出。
   修法：GOM 包一层，拷贝 hints 剥离 glibc 专有位后转发宿主实现。
2. dn_comp：glibc 2.34 起并入 libc.so.6 导出（optver GLIBC_2.34），
   box64 wrappedlibc 符号表缺失 → relocation 报
   "Symbol dn_comp not found, cannot apply R_X86_64_JUMP_SLOT"
   （JUMP_SLOT 填 0，真调用即崩）。musl 1.2.5 自带 dn_comp（5 参，
   iFppipp 与 glibc 一致），GO(dn_comp) 自动生成转发即可。

哨兵：BOX64-BUILD: gai-fix
用法: patch_gai.py <box64源码目录>
"""
import os
import sys

SENTINEL = "BOX64-BUILD: gai-fix"


def fail(msg):
    print(f"[patch_gai] 失败: {msg}", file=sys.stderr)
    sys.exit(1)


def apply_file(path, jobs, must_exist=True):
    if not os.path.isfile(path):
        if must_exist:
            fail(f"文件不存在: {path}")
        print(f"[patch_gai] 跳过缺失文件: {path}")
        return
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    if SENTINEL in src:
        print(f"[patch_gai] {os.path.basename(path)} 已应用过，跳过")
        return
    for anchor, repl in jobs:
        n = src.count(anchor)
        if n != 1:
            fail(f"{os.path.basename(path)} 锚点命中 {n} 次（期望 1）:\n{anchor[:160]}")
        src = src.replace(anchor, repl, 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print(f"[patch_gai] {os.path.basename(path)} 应用 {len(jobs)} 处")


MY_GETADDRINFO = """
// {sentinel} glibc 专有 AI_IDN/AI_CANONIDN 剥离（bionic 会 EAI_BADFLAGS）
EXPORT int my_getaddrinfo(x64emu_t* emu, const char* node, const char* service,
    const struct addrinfo* hints, struct addrinfo** res)
{{
    (void)emu;
    struct addrinfo h;
    if(hints) {{
        h = *hints;
        // 头真值 AI_IDN=0x0040 AI_CANONIDN=0x0080（反汇编 0xC2 证实），
        // 另剥 IDN 扩展位；AI_CANONNAME(0x2)/NUMERICSERV(0x400) 等基础位保留
        h.ai_flags &= ~(0x0040 | 0x0080 | 0x10000000 | 0x00200000);
        hints = &h;
    }}
    return getaddrinfo(node, service, hints, res);
}}
"""


def main():
    if len(sys.argv) != 2:
        fail("用法: patch_gai.py <box64目录>")
    root = sys.argv[1]

    # ---- wrappedlibc_private.h：getaddrinfo/getnameinfo 转 GOM + dn_comp 符号 ----
    apply_file(os.path.join(root, "src", "wrapped", "wrappedlibc_private.h"), [
        ("GO(getaddrinfo, iFpppp)\n",
         "// BOX64-BUILD: gai-fix GOM 包一层剥离 glibc 专有 AI flag\nGOM(getaddrinfo, iFEpppp)\n"),
        ("GO(getnameinfo, iFpupupui)\n",
         "// BOX64-BUILD: gai-fix GOM 包一层剥离 glibc 专有 NI_IDN(0x20)（iFpupupui 为预生成类型）\n"
         "GOM(getnameinfo, iFpupupui)\n"),
        ("GOM(dprintf, iFEipV)\n",
         "GO(dn_comp, iFppipp)  // BOX64-BUILD: gai-fix glibc2.34 归 libc，musl 自带转发\n"
         "GOM(dprintf, iFEipV)\n"),
    ])

    # ---- wrappedlibc.c：netdb.h include + my_getaddrinfo 实现 ----
    apply_file(os.path.join(root, "src", "wrapped", "wrappedlibc.c"), [
        ("#include <sys/socket.h>\n",
         "#include <sys/socket.h>\n#include <netdb.h>\n"),
        ("""EXPORT int my_getopt_long_only(int argc, char* const argv[], const char* optstring, const struct option *longopts, int *longindex)
{
    my_updateGlobalOpt();
    int ret = getopt_long_only(argc, argv, optstring, longopts, longindex);
    my_checkGlobalOpt();
    return ret;
}
""",
         """EXPORT int my_getopt_long_only(int argc, char* const argv[], const char* optstring, const struct option *longopts, int *longindex)
{
    my_updateGlobalOpt();
    int ret = getopt_long_only(argc, argv, optstring, longopts, longindex);
    my_checkGlobalOpt();
    return ret;
}
""" + MY_GETADDRINFO.format(sentinel=SENTINEL)),
        ("""    return getaddrinfo(node, service, hints, res);
}
""",
         """    return getaddrinfo(node, service, hints, res);
}

// """ + SENTINEL + """ glibc 专有 NI_IDN(0x20) 剥离（bionic 会 EAI_BADFLAGS →
// gai_strerror 打 "Invalid flags"）；基础位 NUMHOST/NUMSERV/NOFQDN/
// NAMEREQD/DGRAM 均 <=0x1F，保留之。
EXPORT int my_getnameinfo(const struct sockaddr* sa, uint32_t salen,
    char* host, uint32_t hostlen, char* serv, uint32_t servlen, int flags)
{
    return getnameinfo(sa, salen, host, hostlen, serv, servlen, flags & 0x1F);
}
"""),
    ])

    print("[patch_gai] 完成")


if __name__ == "__main__":
    main()
