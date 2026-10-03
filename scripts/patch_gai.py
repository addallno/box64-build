#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
getaddrinfo/dn_comp 兼容 patch（bionic/musl 宿主适配，通用 GNU 程序适配）。

背景：x86 guest（glibc 语义）与宿主 libc（bionic/musl）的三处差异：
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
3. musl 静态宿主（box64-bin aarch64 musl）读不到 /etc/resolv.conf
   （Android /etc 只读且无此文件）→ native getaddrinfo 5s EAI_AGAIN，
   steamcmd 等全部域名解析失败。修法：native 返回 EAI_AGAIN 时置
   broken 标记改走 nslookup 子进程 fallback（Termux nslookup 走
   bionic netd 解析栈），结果组装标准 addrinfo 链，与 native
   freeaddrinfo（musl free）配对；AF_UNSPEC 只查 A（v6 无出站）。

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


# DNS fallback 辅助（musl 无 resolv.conf 时经 nslookup 解析；花括号原样，不走 format）
MY_GETADDRINFO = r"""
#define BOX64_DNS_MAX 16
#define BOX64_DNS_BUF 4096

static int box64_dns_port(const char* svc)
{
    static const struct { const char* n; int p; } tab[] = {
        {"http", 80}, {"https", 443}, {"ftp", 21}, {"ssh", 22},
        {"domain", 53}, {"smtp", 25}, {"telnet", 23}, {"pop3", 110},
        {"imap", 143}, {"nntp", 119}, {"irc", 6667}, {NULL, 0}
    };
    if (!svc || !*svc)
        return 0;
    char* end = NULL;
    long v = strtol(svc, &end, 10);
    if (end && *end == '\0' && v >= 0 && v <= 65535)
        return (int)v;
    for (int i = 0; tab[i].n; ++i)
        if (!strcasecmp(svc, tab[i].n))
            return tab[i].p;
    return -1;
}

// 跑 nslookup 收集 Address 行；返回条数(>=0) 或 EAI_*（负值）
static int box64_dns_lookup(const char* host, int want_v6, char out[][64], int maxn)
{
    int pfd[2];
    if (pipe(pfd))
        return EAI_AGAIN;
    pid_t pid = fork();
    if (pid < 0) {
        close(pfd[0]); close(pfd[1]);
        return EAI_AGAIN;
    }
    if (pid == 0) {
        // 子进程：stdout+stderr 合并入管道；5s 硬超时（alarm 穿透 exec）
        close(pfd[0]);
        dup2(pfd[1], 1);
        dup2(pfd[1], 2);
        close(pfd[1]);
        alarm(5);
        const char* q = want_v6 ? "-query=AAAA" : "-query=A";
        const char* bin = getenv("BOX64_NSLOOKUP");
        if (bin)
            execl(bin, "nslookup", q, host, (char*)NULL);
        execlp("nslookup", "nslookup", q, host, (char*)NULL);
        execl("/data/data/com.termux/files/usr/bin/nslookup",
              "nslookup", q, host, (char*)NULL);
        _exit(127);
    }
    close(pfd[1]);
    char buf[BOX64_DNS_BUF];
    size_t tot = 0;
    for (;;) {
        ssize_t n = read(pfd[0], buf + tot, sizeof(buf) - 1 - tot);
        if (n <= 0)
            break;
        tot += (size_t)n;
        if (tot >= sizeof(buf) - 1)
            break;
    }
    buf[tot] = '\0';
    close(pfd[0]);
    int st = 0;
    waitpid(pid, &st, 0);
    if (WIFSIGNALED(st))
        return EAI_AGAIN;
    if (WIFEXITED(st) && WEXITSTATUS(st) == 127)
        return EAI_AGAIN;
    int cnt = 0;
    int nxd = (strstr(buf, "NXDOMAIN") || strstr(buf, "non-existent")) ? 1 : 0;
    int saw_name = 0;
    char* line = buf;
    while (line && *line) {
        char* nl = strchr(line, '\n');
        if (nl)
            *nl = '\0';
        if (strstr(line, "Name:"))
            saw_name = 1;
        // 仅 Name: 之后的 Address 行是结果；跳过服务器行（#53）
        char* a = saw_name ? strstr(line, "Address:") : NULL;
        if (a && !strstr(a, "#53")) {
            a += 8;
            while (*a == ' ' || *a == '\t')
                ++a;
            int ok = 0;
            if (want_v6) {
                ok = strchr(a, ':') != NULL;
            } else {
                int dots = 0, bad = 0;
                for (char* p = a; *p; ++p) {
                    if (*p == '.')
                        ++dots;
                    else if (!isdigit((unsigned char)*p)) {
                        bad = 1;
                        break;
                    }
                }
                ok = (!bad && dots == 3) ? 1 : 0;
            }
            if (ok && cnt < maxn) {
                strncpy(out[cnt], a, 63);
                out[cnt][63] = '\0';
                ++cnt;
            }
        }
        line = nl ? nl + 1 : NULL;
    }
    if (cnt)
        return cnt;
    return nxd ? EAI_NONAME : EAI_AGAIN;
}

// native 不可用时的解析组链（calloc 组装，与 native freeaddrinfo 的 musl free 配对）
static int box64_dns_fallback(const char* node, const char* service,
    const struct addrinfo* hints, struct addrinfo** res)
{
    if (!res)
        return EAI_FAIL;
    *res = NULL;
    if (!node && !service)
        return EAI_NONAME;
    int family = hints ? hints->ai_family : AF_UNSPEC;
    if (family != AF_UNSPEC && family != AF_INET && family != AF_INET6)
        return EAI_FAMILY;
    int want_v6 = (family == AF_INET6);
    int port = 0;
    if (service) {
        int p = box64_dns_port(service);
        if (p < 0)
            return EAI_SERVICE;
        port = p;
    }
    char addrs[BOX64_DNS_MAX][64];
    int cnt = 0;
    if (!node) {
        // 通配绑定（node==NULL 时 AI_PASSIVE 与否语义均绑通配地址）
        strncpy(addrs[0], want_v6 ? "::" : "0.0.0.0", 64);
        addrs[0][63] = '\0';
        cnt = 1;
    } else {
        struct in_addr t4;
        struct in6_addr t6;
        if ((want_v6 && inet_pton(AF_INET6, node, &t6) == 1)
            || (!want_v6 && inet_pton(AF_INET, node, &t4) == 1)) {
            strncpy(addrs[0], node, 64);
            addrs[0][63] = '\0';
            cnt = 1;
        } else {
            int r = box64_dns_lookup(node, want_v6, addrs, BOX64_DNS_MAX);
            if (r < 0)
                return r;
            cnt = r;
        }
    }
    if (cnt <= 0)
        return EAI_NONAME;
    int stype = hints ? hints->ai_socktype : 0;
    int proto = hints ? hints->ai_protocol : 0;
    int oflags = hints ? hints->ai_flags : 0;
    int want_canon = (oflags & AI_CANONNAME) ? 1 : 0;
    struct addrinfo* head = NULL;
    struct addrinfo** tail = &head;
    int made = 0;
    for (int i = 0; i < cnt; ++i) {
        struct addrinfo* ai = (struct addrinfo*)calloc(1, sizeof(struct addrinfo));
        if (!ai)
            goto oom;
        ai->ai_family = want_v6 ? AF_INET6 : AF_INET;
        ai->ai_socktype = stype;
        ai->ai_protocol = proto;
        ai->ai_flags = oflags;
        if (want_v6) {
            struct sockaddr_in6* sa =
                (struct sockaddr_in6*)calloc(1, sizeof(struct sockaddr_in6));
            if (!sa) { free(ai); goto oom; }
            sa->sin6_family = AF_INET6;
            sa->sin6_port = (uint16_t)htons(port);
            if (inet_pton(AF_INET6, addrs[i], &sa->sin6_addr) != 1) {
                free(sa); free(ai);
                continue;
            }
            ai->ai_addr = (struct sockaddr*)sa;
            ai->ai_addrlen = sizeof(struct sockaddr_in6);
        } else {
            struct sockaddr_in* sa =
                (struct sockaddr_in*)calloc(1, sizeof(struct sockaddr_in));
            if (!sa) { free(ai); goto oom; }
            sa->sin_family = AF_INET;
            sa->sin_port = (uint16_t)htons(port);
            if (inet_pton(AF_INET, addrs[i], &sa->sin_addr) != 1) {
                free(sa); free(ai);
                continue;
            }
            ai->ai_addr = (struct sockaddr*)sa;
            ai->ai_addrlen = sizeof(struct sockaddr_in);
        }
        if (want_canon && !made) {
            ai->ai_canonname = box_strdup(node);
            want_canon = 0;
        }
        *tail = ai;
        tail = &ai->ai_next;
        ++made;
    }
    if (!made)
        return EAI_NONAME;
    *res = head;
    return 0;
oom:
    if (head)
        freeaddrinfo(head);
    return EAI_MEMORY;
}

static int box64_gai_native_broken = 0;

// {sentinel} glibc 专有 AI_IDN/AI_CANONIDN 剥离（bionic 会 EAI_BADFLAGS）；
// native 返回 EAI_AGAIN（musl 无 resolv.conf）后改走 nslookup fallback。
EXPORT int my_getaddrinfo(x64emu_t* emu, const char* node, const char* service,
    const struct addrinfo* hints, struct addrinfo** res)
{
    (void)emu;
    struct addrinfo h;
    if (hints) {
        h = *hints;
        // 头真值 AI_IDN=0x0040 AI_CANONIDN=0x0080（反汇编 0xC2 证实），
        // 另剥 IDN 扩展位；AI_CANONNAME(0x2)/NUMERICSERV(0x400) 等基础位保留
        h.ai_flags &= ~(0x0040 | 0x0080 | 0x10000000 | 0x00200000);
        hints = &h;
    }
    if (!box64_gai_native_broken) {
        int r = getaddrinfo(node, service, hints, res);
        if (r == 0)
            return 0;
        if (r != EAI_AGAIN)
            return r;
        box64_gai_native_broken = 1;  // 宿主无可用解析器，后续直接 fallback
    }
    return box64_dns_fallback(node, service, hints, res);
}
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
         "#include <sys/socket.h>\n#include <netdb.h>\n#include <strings.h>\n"),
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
""" + MY_GETADDRINFO.replace("{sentinel}", SENTINEL)),
        ("""    return box64_dns_fallback(node, service, hints, res);
}
""",
         """    return box64_dns_fallback(node, service, hints, res);
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
