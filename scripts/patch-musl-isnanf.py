#!/usr/bin/env python3
"""给 box64 源码打补丁，使其可用 musl 交叉编译：

1. glibc 专属接口替换（musl 无）：
   - isnanf()/isinff() → C99 isnan()/isinf()（按 sizeof 自适应，语义等价）
   - fseeko64()/ftello64() → fseeko()/ftello()（musl 的 off_t 恒为 64 位）
   - ino64_t/off64_t → ino_t/off_t（glibc 专属别名，musl 本就 64 位）
   - os_linux.c 的 mallopt() 调优块 → 注释掉（musl 无 glibc mallopt）
2. fts 支持（musl 的 libc 没有 fts）：
   - 下载 void-linux/musl-fts 的 fts.c/fts.h 注入 box64 源码
   - 修改 CMakeLists.txt 把 fts.c 加入编译源
   - 生成 stub 头 sys/param.h（musl 无此头）、config.h（musl-fts 特性宏）
"""

import os
import re
import sys
import urllib.request
import subprocess

REPL = {  # 优先匹配更长的 f 变体
    "isnanf(": "isnan(",
    "isinff(": "isinf(",
    # musl 的 off_t 恒为 64 位，fseeko/ftello 本身就是 64 位，等价替换
    "fseeko64(": "fseeko(",
    "ftello64(": "ftello(",
    # glibc 专属的 64 位类型别名，musl 用 ino_t/off_t（本就 64 位），语义等价
    "ino64_t": "ino_t",
    "off64_t": "off_t",
    # glibc 内部类型别名，musl 用同名公共类型（布局一致）
    "__sigset_t": "sigset_t",
    # glibc 的 _NP 初始化宏 → 无后缀占位（musl 无 ERRORCHECK/RECURSIVE 静态初始化宏，
    # 实际值由 build 脚本 CFLAGS 注入，见 PTHREAD_MUTEX_INITIALIZER_* 定义）
    "PTHREAD_ERRORCHECK_MUTEX_INITIALIZER_NP": "PTHREAD_ERRORCHECK_MUTEX_INITIALIZER",
    "PTHREAD_RECURSIVE_MUTEX_INITIALIZER_NP": "PTHREAD_RECURSIVE_MUTEX_INITIALIZER",
}

# musl 无 glibc 的 mallopt/M_ARENA_*，从 os_linux.c 注释该调优块
MALLOPT_MARKER = "// setting 32bits malloc options"

# mysignal.h：非 Windows 分支缺 __sigset_t typedef（musl 中它只是结构体标签）
SIGSET_TYPEDEF_ANCHOR = "#include <signal.h>"
SIGSET_TYPEDEF_PATCH = """#include <signal.h>
typedef sigset_t __sigset_t;"""

# musl 无 glibc 的 __uid_t/__gid_t/__pid_t/__sighandler_t 内部别名
UNDERSCORE_TYPES = {
    "__uid_t": "uid_t",
    "__gid_t": "gid_t",
    "__pid_t": "pid_t",
    "__sighandler_t": "sighandler_t",
    # glibc 专属 64 位 statfs 类型（musl 的 fsblkcnt_t/fsfilcnt_t 本就 64 位）
    "__fsblkcnt64_t": "fsblkcnt_t",
    "__fsfilcnt64_t": "fsfilcnt_t",
}

# musl-fts 上游（void-linux 维护的 FreeBSD 派生 fts，纯 POSIX）
FTS_URL = "https://raw.githubusercontent.com/void-linux/musl-fts/master/{}"

# fts.c 里需要的 libtools 源文件列表插入锚点（CMakeLists.txt 第 467 行 auxval.c）
CMAKE_FTS_ANCHOR = '"${BOX64_ROOT}/src/libtools/auxval.c"'


def patch_text(s: str):
    """按 dict 键长降序替换（正则整词边界），避免 isnanf 被 isnan 前缀干扰。"""
    all_repl = dict(REPL)
    all_repl.update(UNDERSCORE_TYPES)
    pattern = re.compile(r"\b(" + "|".join(re.escape(k) for k in all_repl) + r")")
    return pattern.sub(lambda m: all_repl[m.group(1)], s)


def patch_mysignal_h(s: str):
    """mysignal.h：非 Windows 分支补 __sigset_t typedef（musl 中它只是结构体标签）。"""
    if SIGSET_TYPEDEF_PATCH in s:
        return s, 0
    if SIGSET_TYPEDEF_ANCHOR in s:
        s = s.replace(SIGSET_TYPEDEF_ANCHOR, SIGSET_TYPEDEF_PATCH, 1)
        return s, 1
    return s, 0


# ---- threads.c 专项：musl 缺失的 glibc 线程接口 ----

# 1) _pthread_cleanup_push/pop 声明：musl 头已声明（struct __ptcb* 签名），注释掉 box64 的 void* 重复声明
THREADS_CLEANUP_DECL = """void _pthread_cleanup_push(void* buffer, void* routine, void* arg);	// declare hidden functions
void _pthread_cleanup_pop(void* buffer, int exec);"""
THREADS_CLEANUP_PATCH = """// musl 的 <pthread.h> 已声明 _pthread_cleanup_push/pop（struct __ptcb* 签名），
// box64 用 void* 声明与之冲突，注释掉（musl 下由头文件提供）"""

# 2) mmap64：box64 自己在 custommmap.c 定义了 mmap64（mmap 是其 alias），
#    只需 musl 无的声明。threads.c 无需改（box64 的 mmap64 签名已够用）
# 3) iFli_t 用 unsigned long 接收 pthread_t 指针 → musl 下 pthread_t 是指针，改 void*
THREADS_IFLI = "typedef int (*iFli_t)(long unsigned int, int);"
THREADS_IFLI_PATCH = "typedef int (*iFli_t)(void*, int);"

# 4) dlvsym（glibc 专属，musl 无）→ 直接用当前 pthread_kill
THREADS_DLVSYM_BLOCK = """	// search for older symbol for pthread_kill
	{
		char buff[50];
		for(int i=0; i<34 && !real_phtread_kill_old; ++i) {
			snprintf(buff, 50, "GLIBC_2.%d", i);
			real_phtread_kill_old = (iFli_t)dlvsym(NULL, "pthread_kill", buff);
		}
	}
	if(!real_phtread_kill_old)
		real_phtread_kill_old = (iFli_t)dlvsym(NULL, "pthread_kill", "GLIBC_2.2.5");
	if(!real_phtread_kill_old) {
		printf_log(LOG_INFO, "Warning, older then 2.34 pthread_kill not found, using current one\\n");
		real_phtread_kill_old = (iFli_t)pthread_kill;
	}"""
THREADS_DLVSYM_PATCH = """	// musl 无版本符号机制，直接使用当前 pthread_kill
	real_phtread_kill_old = (iFli_t)pthread_kill;"""

# 5) pthread_attr_*affinity_np：musl 无属性级版本，注入 stub 定义到 threads.c 顶部
THREADS_STUB_ANCHOR = "typedef struct threadstack_s {"
THREADS_STUB_PATCH = """// musl 无 pthread_attr_*affinity_np（glibc 专属），stub 返回 ENOSYS
int pthread_attr_getaffinity_np(const pthread_attr_t* attr, size_t cpusize, void* cpuset)
{ (void)attr; (void)cpusize; (void)cpuset; errno = ENOSYS; return -1; }
int pthread_attr_setaffinity_np(pthread_attr_t* attr, size_t cpusize, void* cpuset)
{ (void)attr; (void)cpusize; (void)cpuset; errno = ENOSYS; return -1; }

typedef struct threadstack_s {"""


def patch_threads_c(s: str):
    """threads.c 的 musl 适配：cleanup 声明/mmap64/类型/dlvsym/attr affinity stub。"""
    count = 0
    for old, new in (
        (THREADS_CLEANUP_DECL, THREADS_CLEANUP_PATCH),
        (THREADS_IFLI, THREADS_IFLI_PATCH),
        (THREADS_DLVSYM_BLOCK, THREADS_DLVSYM_PATCH),
    ):
        if old in s:
            s = s.replace(old, new, 1)
            count += 1
        else:
            print(f"警告: threads.c 未找到片段: {old.splitlines()[0][:60]}")
    if THREADS_STUB_PATCH not in s and THREADS_STUB_ANCHOR in s:
        s = s.replace(THREADS_STUB_ANCHOR, THREADS_STUB_PATCH, 1)
        count += 1
    return s, count


def remove_mallopt_block(s: str):
    """仅注释 os_linux.c 的 mallopt 相关行（musl 无此 glibc 接口），不触碰 #ifndef/#endif。"""
    lines = s.split("\n")
    out = []
    removed = 0
    for line in lines:
        if MALLOPT_MARKER in line:
            removed += 1
            out.append("    // " + line + " (musl 无此 glibc 接口, patch)")
            continue
        if "mallopt(" in line:
            removed += 1
            out.append("    // " + line)
            continue
        out.append(line)
    return "\n".join(out), removed


# ---- signal32.c 专项：glibc 专属 siginfo_t 字段名（musl 用 __si_fields）----
# glibc:  union _sifields;  __SI_SIGFAULT_ADDL 宏（aarch64 为空）
# musl:   union __si_fields;  无 __SI_SIGFAULT_ADDL 宏
# signal32.c 自定义镜像结构 my_siginfo32_s 内的 _sifields 成员保留，
# 只有访问宿主 siginfo_t 的成员名需要改成 musl 的名字。
SIGNAL32_GLIBC = "    __SI_SIGFAULT_ADDL\n"
SIGNAL32_FIELD_SRC = "src->_sifields"
SIGNAL32_FIELD_OFFSETOF = "offsetof(siginfo_t, _sifields)"

# ---- threads32.c 专项：与 threads.c 相同思路，但缩进为空格、类型名 iFLi_t ----

# 1) cleanup 声明冲突（musl pthread.h 已用 struct __ptcb* 声明，注释掉；空格版）
THREADS32_CLEANUP_DECL = """void _pthread_cleanup_push(void* buffer, void* routine, void* arg); // declare hidden functions
void _pthread_cleanup_pop(void* buffer, int exec);"""
THREADS32_CLEANUP_PATCH = """// musl 的 <pthread.h> 已声明 _pthread_cleanup_push/pop（struct __ptcb* 签名），
// box64 用 void* 声明与之冲突，注释掉（musl 下由头文件提供）"""
# 2) iFLi_t：musl pthread_t 是指针，unsigned long 改为 void*
THREADS32_IFLI = "typedef int  (*iFLi_t)(unsigned long, int);"
THREADS32_IFLI_PATCH = "typedef int  (*iFLi_t)(void*, int);"

# 3) dlvsym（glibc 专属，musl 无）→ 直接 pthread_kill（空格缩进版）
THREADS32_DLVSYM_BLOCK = """    // search for older symbol for pthread_kill
    {
        char buff[50];
        for(int i=0; i<34 && !real_phtread_kill_old; ++i) {
            snprintf(buff, 50, "GLIBC_2.%d", i);
            real_phtread_kill_old = (iFLi_t)dlvsym(NULL, "pthread_kill", buff);
        }
    }"""
THREADS32_DLVSYM_PATCH = """    // musl 无版本符号机制，直接使用当前 pthread_kill
    real_phtread_kill_old = (iFLi_t)pthread_kill;"""

# 4) pthread_getattr_np：musl 声明签名 (pthread_t, pthread_attr_t*)，th 是 uintptr_t 需 cast
THREADS32_GETATTR = "    int ret = pthread_getattr_np(th, get_attr(attr));"
THREADS32_GETATTR_PATCH = "    int ret = pthread_getattr_np((pthread_t)th, get_attr(attr));"

# 5) struct __pthread_mutex_s（glibc 专属类型）→ 读第一个 int（glibc 的 __kind 与
#    musl 的 type 都是首个 int，等价）
THREADS32_MUTEX = 'fake->i386__kind = ((struct __pthread_mutex_s*)from_ptrv(fake->real_mutex))->__kind;'
THREADS32_MUTEX_PATCH = 'fake->i386__kind = *((int*)from_ptrv(fake->real_mutex));'


def patch_threads32_c(s: str):
    """threads32.c 的 musl 适配（与 threads.c 同一套问题，缩进/类型名不同）。"""
    count = 0
    for old, new in (
        (THREADS32_CLEANUP_DECL, THREADS32_CLEANUP_PATCH),
        (THREADS32_IFLI, THREADS32_IFLI_PATCH),
        (THREADS32_DLVSYM_BLOCK, THREADS32_DLVSYM_PATCH),
        (THREADS32_GETATTR, THREADS32_GETATTR_PATCH),
        (THREADS32_MUTEX, THREADS32_MUTEX_PATCH),
    ):
        if old in s:
            s = s.replace(old, new, 1)
            count += 1
        else:
            print(f"警告: threads32.c 未找到片段: {old.splitlines()[0][:60]}")
    if THREADS_STUB_PATCH not in s and THREADS_STUB_ANCHOR in s:
        s = s.replace(THREADS_STUB_ANCHOR, THREADS_STUB_PATCH, 1)
        count += 1
    return s, count


def patch_signal32_c(s: str):
    """signal32.c：适配 musl 的 siginfo_t（__si_fields union，无 __SI_SIGFAULT_ADDL 宏）。"""
    count = 0
    if SIGNAL32_GLIBC in s:
        s = s.replace(SIGNAL32_GLIBC, "", 1)
        count += 1
    if SIGNAL32_FIELD_SRC in s:
        s = s.replace(SIGNAL32_FIELD_SRC, "src->__si_fields", 1)
        count += 1
    if SIGNAL32_FIELD_OFFSETOF in s:
        s = s.replace(SIGNAL32_FIELD_OFFSETOF, "offsetof(siginfo_t, __si_fields)", 1)
        count += 1
    return s, count


# libc_net32.c：musl 无 getprotobyname_r/getprotobynumber_r，宿主 __res_state 用 qhook/rhook
NET32_GETPROTONAME_R = "    int r = getprotobyname_r(name, &ret_l, buff, buflen, &result_l);"
NET32_GETPROTONAME_R_PATCH = (
    "    int r = -1;\n"
    "    struct protoent* tmp_pe = getprotobyname(name);\n"
    "    if(tmp_pe) { ret_l = *tmp_pe; result_l = &ret_l; r = 0; }"
)
NET32_GETPROTONUMBER_R = "    int r = getprotobynumber_r(proto, &ret_l, buff, buflen, &result_l);"
NET32_GETPROTONUMBER_R_PATCH = (
    "    int r = -1;\n"
    "    struct protoent* tmp_pe = getprotobynumber(proto);\n"
    "    if(tmp_pe) { ret_l = *tmp_pe; result_l = &ret_l; r = 0; }"
)
NET32_QHOOK_32 = "to_ptrv(src->__glibc_unused_qhook)"  # 573: dst=镜像, src=宿主 → src 改 qhook
NET32_RHOOK_32 = "to_ptrv(src->__glibc_unused_rhook)"  # 574
NET32_RHOOK_64 = "dst->__glibc_unused_rhook = from_ptrv(src->__glibc_unused_rhook);"  # 593 整行唯一
NET32_QHOOK_64 = "dst->__glibc_unused_qhook = from_ptrv(src->__glibc_unused_qhook);"  # 594 整行唯一


def patch_libc_net32_c(s: str):
    """libc_net32.c：musl 无 getprotobyname_r/getprotobynumber_r（改非 _r 模拟）；
    宿主 struct __res_state 无 __glibc_unused_qhook/rhook（musl 用 qhook/rhook）。
    注意：convert_res_state_to_32 里 src=宿主(dst=镜像)，convert_res_state_to_64 里
    dst=宿主(src=镜像)，两者各只改宿主侧字段，镜像字段名 __glibc_unused_* 保留。"""
    count = 0
    if NET32_GETPROTONAME_R in s:
        s = s.replace(NET32_GETPROTONAME_R, NET32_GETPROTONAME_R_PATCH, 1)
        count += 1
    if NET32_GETPROTONUMBER_R in s:
        s = s.replace(NET32_GETPROTONUMBER_R, NET32_GETPROTONUMBER_R_PATCH, 1)
        count += 1
    for src_frag, dst_frag in (
        (NET32_QHOOK_32, "to_ptrv(src->qhook)"),
        (NET32_RHOOK_32, "to_ptrv(src->rhook)"),
        (NET32_RHOOK_64, "dst->rhook = from_ptrv(src->__glibc_unused_rhook);"),
        (NET32_QHOOK_64, "dst->qhook = from_ptrv(src->__glibc_unused_qhook);"),
    ):
        if src_frag in s:
            assert s.count(src_frag) == 1, f"libc_net32.c 替换片段不唯一: {src_frag!r}"
            s = s.replace(src_frag, dst_frag, 1)
            count += 1
    return s, count


def fetch(url: str) -> str:
    print(f"下载 {url}")
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read().decode("utf-8")


def inject_fts(root: str, include_dir: str = None):
    """注入 musl-fts：下载 fts.c/fts.h → src/libtools/，改 CMakeLists 编译 fts.c。
    fts.h 也复制到 include_dir（CMake 不搜索 src/libtools，#include <fts.h> 用尖括号）。
    设置环境变量 SKIP_FTS=1 可跳过网络下载（本地无网验证时用）。"""
    libtools = os.path.join(root, "src", "libtools")
    if include_dir:
        os.makedirs(include_dir, exist_ok=True)
    for name in ("fts.c", "fts.h"):
        dst = os.path.join(libtools, name)
        if os.path.exists(dst):
            print(f"跳过 {name}（已存在）")
        elif os.environ.get("SKIP_FTS"):
            print(f"跳过 {name}（SKIP_FTS=1，本地无网）")
            continue
        else:
            content = fetch(FTS_URL.format(name))
            with open(dst, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"写入 {dst}")
        if name == "fts.h" and include_dir:
            extra = os.path.join(include_dir, "fts.h")
            if not os.path.exists(extra):
                with open(dst, "r", encoding="utf-8") as f:
                    content = f.read()
                with open(extra, "w", encoding="utf-8") as f:
                    f.write(content)
                print(f"写入 {extra}")

    cmake = os.path.join(root, "CMakeLists.txt")
    with open(cmake, "r", encoding="utf-8") as f:
        s = f.read()
    if "${BOX64_ROOT}/src/libtools/fts.c" not in s:
        anchor = CMAKE_FTS_ANCHOR
        assert anchor in s, f"CMakeLists.txt 找不到锚点 {anchor}"
        s = s.replace(anchor, anchor + '\n    "${BOX64_ROOT}/src/libtools/fts.c"', 1)
        with open(cmake, "w", encoding="utf-8") as f:
            f.write(s)
        print("CMakeLists.txt: 已加入 fts.c 编译源")
    else:
        print("CMakeLists.txt: fts.c 已在编译源列表")


def write_stub_headers(include_dir: str):
    """生成 stub 头：sys/param.h（musl 无此头）、config.h（musl-fts 特性宏）。"""
    os.makedirs(os.path.join(include_dir, "sys"), exist_ok=True)
    param_h = os.path.join(include_dir, "sys", "param.h")
    if not os.path.exists(param_h):
        with open(param_h, "w", encoding="utf-8") as f:
            f.write(
                "#ifndef _SYS_PARAM_H_\n"
                "#define _SYS_PARAM_H_\n"
                "/* musl 无 sys/param.h，stub 提供 fts.c 所需的少量宏 */\n"
                "#include <limits.h>\n"
                "#include <sys/types.h>\n"
                "#ifndef MAXPATHLEN\n"
                "#define MAXPATHLEN PATH_MAX\n"
                "#endif\n"
                "#ifndef MIN\n"
                "#define MIN(a,b) ((a)<(b)?(a):(b))\n"
                "#endif\n"
                "#ifndef MAX\n"
                "#define MAX(a,b) ((a)>(b)?(a):(b))\n"
                "#endif\n"
                "#endif /* _SYS_PARAM_H_ */\n"
            )
        print(f"写入 {param_h}")

    config_h = os.path.join(include_dir, "config.h")
    if not os.path.exists(config_h):
        with open(config_h, "w", encoding="utf-8") as f:
            f.write(
                "/* musl-fts 特性宏：musl 有 dirfd()，无 d_namlen */\n"
                "#define HAVE_DIRFD 1\n"
                "/* 其余 HAVE_* 均不定义（musl 无对应 glibc 特性） */\n"
            )
        print(f"写入 {config_h}")

    # musl 的 <sys/mman.h> 无 mmap64 声明，但 box64 在 custommmap.c 定义了 mmap64
    # （mmap 是其 alias）。musl 下调用处无法看到声明 → 补声明。
    mmap_h = os.path.join(include_dir, "mmap64.h")
    if not os.path.exists(mmap_h):
        with open(mmap_h, "w", encoding="utf-8") as f:
            f.write(
                "#ifndef _MMAP64_H_\n"
                "#define _MMAP64_H_\n"
                "/* musl 无 mmap64 声明（box64 在 custommmap.c 定义，mmap 是其 alias） */\n"
                "#include <sys/mman.h>\n"
                "#include <sys/types.h>\n"
                "#include <dirent.h>\n"
                "void* mmap64(void* addr, unsigned long length, int prot, int flags, int fd, ssize_t offset);\n"
                "/* musl 无 glibc 的 __compar_d_fn_t（qsort_r 回调类型），补齐以便 wrappedlibc.c 编译 */\n"
                "typedef int (*__compar_d_fn_t)(const void*, const void*, void*);\n"
                "/* musl 不 export __ctype_*_loc 到 <ctype.h>，但 libc 有符号，补声明 */\n"
                "const unsigned short** __ctype_b_loc(void);\n"
                "const int** __ctype_toupper_loc(void);\n"
                "const int** __ctype_tolower_loc(void);\n"
                "/* musl 无 glibc 的 struct mallinfo（box64 只用它 memset，不需字段对齐语义） */\n"
                "struct mallinfo {\n"
                "  int arena; int ordblks; int smblks; int hblks; int hblkhd;\n"
                "  int usmblks; int fsmblks; int uordblks; int fordblks; int keepcost;\n"
                "};\n"
                "/* musl 无 scandirat（box64 的 my_scandirat 需要），补声明（实现注入 scandirat.c） */\n"
                "int scandirat(int dirfd, const char *path, struct dirent ***res,\n"
                "              int (*sel)(const struct dirent *),\n"
                "              int (*cmp)(const struct dirent **, const struct dirent **));\n"
                "#endif /* _MMAP64_H_ */\n"
            )
        print(f"写入 {mmap_h}")


# ---- obstack 移植（glibc 专属，musl 无）----

# 本文件同目录的 obstack/ 下放着移植好的 obstack.h 与 obstack_glibc.c
OBSTACK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "obstack")
ERROR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "error")
SCANDIRAT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scandirat")


def patch_wrappedlibc_c(s: str):
    """wrappedlibc.c 的 musl 适配：
    - musl 无 struct stat64 / stat64 等（off_t 恒 64 位，struct stat 即 64 位布局），
      glibc 的 __xstat/__fxstat 族内部用 stat64 获取 64 位信息再 Unalign 到 x86 布局。
      musl 下直接用 struct stat / stat / fstat / lstat / fstatat（等价）。
    - qsort_r 的 __compar_d_fn_t 已由 mmap64.h（-include）提供 typedef。
    只改函数体内部，保留 EXPORT my___*stat64(...) 的 alias 声明。
    """
    pairs = [
        ("    struct stat64 st;\n", "    struct stat st;\n"),
        ("    struct  stat64 st;\n", "    struct stat st;\n"),
        ("    int r = fstat64(fd, buf?&st:buf);\n", "    int r = fstat(fd, buf?&st:buf);\n"),
        ("    int r = stat64((const char*)path, buf?&st:buf);\n", "    int r = stat((const char*)path, buf?&st:buf);\n"),
        ("    int r = lstat64((const char*)name, buf?&st:buf);\n", "    int r = lstat((const char*)name, buf?&st:buf);\n"),
        ("    int r = fstatat64(d, path, &st, flags);\n", "    int r = fstatat(d, path, &st, flags);\n"),
    ]
    count = 0
    for old, new in pairs:
        if old in s:
            # struct stat64 st 声明出现多次（各 *_stat 函数体），替换目标相同→全部替换
            # 函数调用行必须唯一（防串改到 alias 声明）
            if old.startswith("    struct"):
                s = s.replace(old, new)
                count += 1
            else:
                assert s.count(old) == 1, f"wrappedlibc.c 替换片段不唯一: {old!r}"
                s = s.replace(old, new, 1)
                count += 1

    # musl 无 *64 变体（off_t 恒 64 位），纯去掉 64 后缀即可。
    # 调用点在 my_open64/my_fopen64/my_glob64/my_scandir64 等函数体内，可多处出现→全部替换。
    for old, new in (
        ("return open64(", "return open("),
        ("return fopen64(", "return fopen("),
        ("return glob64(", "return glob("),
        ("return scandir64(", "return scandir("),
        ("return scandirat64(", "return scandirat("),
        ("return ftw64(", "return ftw("),
        ("return nftw64(", "return nftw("),
    ):
        if old in s:
            s = s.replace(old, new)
            count += 1

    # musl 无 getrlimit64（getrlimit 即 64 位版本），struct rlimit64 → struct rlimit
    if "struct rlimit64* rlim" in s:
        assert s.count("struct rlimit64* rlim") == 1
        s = s.replace("struct rlimit64* rlim", "struct rlimit* rlim", 1)
        count += 1
    if "getrlimit64(resource, rlim)" in s:
        assert s.count("getrlimit64(resource, rlim)") == 1
        s = s.replace("getrlimit64(resource, rlim)", "getrlimit(resource, rlim)", 1)
        count += 1

    # musl 的 pthread_mutex_t 无 __data.__owner（glibc 专属）；
    # getGlibcCachedTid 里改用 GetTID()（musl 无 glibc tid 缓存机制，直接取真实 tid，
    # 使 updateGlibcTidCache 中 cached==real 恒成立而跳过写缓存）。
    if "pid_t tid = lock.__data.__owner;" in s:
        assert s.count("pid_t tid = lock.__data.__owner;") == 1
        s = s.replace("pid_t tid = lock.__data.__owner;", "pid_t tid = GetTID();", 1)
        count += 1

    return s, count


def patch_wrapped32_libc_c(s: str):
    """wrapped32/wrappedlibc.c 的 musl 适配：
    x86 32 位程序的 stat 系统调用。宿主侧用 struct stat（musl 等价于 glibc struct stat64），
    经 FillStatFromStat64 转 i386_stat 布局。字段名不变。
    """
    pairs = [
        ("const struct stat64 *st64", "const struct stat *st64"),
        ("    struct stat64 s = {0};\n", "    struct stat s = {0};\n"),
        ("    struct stat64 st;\n", "    struct stat st;\n"),
        ("    int ret = fstatat64(fd, name, buff?&s:NULL, flags);\n",
         "    int ret = fstatat(fd, name, buff?&s:NULL, flags);\n"),
        ("    int ret = stat64(f, r?&s:NULL);\n", "    int ret = stat(f, r?&s:NULL);\n"),
        ("    int ret = lstat64(f, r?&s:NULL);\n", "    int ret = lstat(f, r?&s:NULL);\n"),
        ("    int ret = fstat64(fd, r?&s:NULL);\n", "    int ret = fstat(fd, r?&s:NULL);\n"),
        ("    int r = stat64(path, &st);\n", "    int r = stat(path, &st);\n"),
        ("    int r = fstat64(fd, &st);\n", "    int r = fstat(fd, &st);\n"),
        ("    int r = lstat64(path, &st);\n", "    int r = lstat(path, &st);\n"),
        ("    int r = stat64((const char*)path, &st);\n", "    int r = stat((const char*)path, &st);\n"),
        ("    int r = lstat64((const char*)name, &st);\n", "    int r = lstat((const char*)name, &st);\n"),
        ("    int r = fstatat64(d, path, &st, flags);\n", "    int r = fstatat(d, path, &st, flags);\n"),
    ]
    count = 0
    for old, new in pairs:
        if old in s:
            s = s.replace(old, new)
            count += 1
    return s, count


def inject_obstack(root: str, include_dir: str = None):
    """注入 glibc obstack 移植实现：
    1. obstack.h → include_dir（供 #include <obstack.h>）
    2. obstack_glibc.c → src/libtools/（不覆盖 box64 自带的 wrapper obstack.c，
       该 wrapper 内部调用我们提供的 _obstack_* 符号）
    3. CMakeLists 追加 obstack_glibc.c 编译源
    """
    if include_dir:
        os.makedirs(include_dir, exist_ok=True)
        dst_h = os.path.join(include_dir, "obstack.h")
        src_h = os.path.join(OBSTACK_DIR, "obstack.h")
        if not os.path.exists(dst_h):
            with open(src_h, "r", encoding="utf-8") as f:
                content = f.read()
            with open(dst_h, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"写入 {dst_h}")
        else:
            print(f"跳过 obstack.h（已存在）")

    libtools = os.path.join(root, "src", "libtools")
    dst_c = os.path.join(libtools, "obstack_glibc.c")
    src_c = os.path.join(OBSTACK_DIR, "obstack_glibc.c")
    if not os.path.exists(dst_c):
        with open(src_c, "r", encoding="utf-8") as f:
            content = f.read()
        with open(dst_c, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"写入 {dst_c}")
    else:
        print(f"跳过 obstack_glibc.c（已存在）")

    cmake = os.path.join(root, "CMakeLists.txt")
    with open(cmake, "r", encoding="utf-8") as f:
        s = f.read()
    src_entry = '"${BOX64_ROOT}/src/libtools/obstack_glibc.c"'
    if src_entry not in s:
        # 锚点：主 ELFLOADER_SRC 无条件列表（auxval.c 后），
        # 这样 Android 与 Linux 都能编译，为 myalign32.c（BOX32 必编）提供 _obstack_* 符号
        anchor = '"${BOX64_ROOT}/src/libtools/auxval.c"'
        assert anchor in s, f"CMakeLists.txt 找不到锚点 {anchor}"
        s = s.replace(anchor, anchor + "\n    " + src_entry, 1)
        with open(cmake, "w", encoding="utf-8") as f:
            f.write(s)
        print("CMakeLists.txt: 已把 obstack_glibc.c 加入无条件 ELFLOADER_SRC")
    else:
        print("CMakeLists.txt: obstack_glibc.c 已在编译源列表")


def inject_glibc_headers(include_dir: str):
    """把 scripts/glibc-hdr/ 下 glibc 专属 stub 头复制到 include_dir，
    使 static_libc.h 的 #include 编译通过（box64 不实际使用这些头里的类型）。"""
    if not include_dir:
        return
    hdr_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "glibc-hdr")
    if not os.path.isdir(hdr_dir):
        print(f"跳过 glibc-hdr（不存在: {hdr_dir}）")
        return
    for dirpath, _dirs, files in os.walk(hdr_dir):
        rel = os.path.relpath(dirpath, hdr_dir)
        dst_dir = os.path.join(include_dir, rel) if rel != "." else include_dir
        os.makedirs(dst_dir, exist_ok=True)
        for f in files:
            src = os.path.join(dirpath, f)
            dst = os.path.join(dst_dir, f)
            if not os.path.exists(dst):
                with open(src, "r", encoding="utf-8") as fi:
                    with open(dst, "w", encoding="utf-8") as fo:
                        fo.write(fi.read())
                print(f"写入 stub 头 {dst}")
            else:
                print(f"跳过 {dst}（已存在）")


def inject_error(root: str, include_dir: str = None):
    """注入 glibc error()/error_at_line() 移植实现：
    1. error.h → include_dir（供 #include <error.h>）
    2. error.c → src/libtools/
    3. CMakeLists 追加 error.c 编译源
    """
    if include_dir:
        os.makedirs(include_dir, exist_ok=True)
        dst_h = os.path.join(include_dir, "error.h")
        src_h = os.path.join(ERROR_DIR, "error.h")
        if not os.path.exists(dst_h):
            with open(src_h, "r", encoding="utf-8") as f:
                content = f.read()
            with open(dst_h, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"写入 {dst_h}")
        else:
            print("跳过 error.h（已存在）")

    libtools = os.path.join(root, "src", "libtools")
    dst_c = os.path.join(libtools, "error_glibc.c")
    src_c = os.path.join(ERROR_DIR, "error.c")
    if not os.path.exists(dst_c):
        with open(src_c, "r", encoding="utf-8") as f:
            content = f.read()
        with open(dst_c, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"写入 {dst_c}")
    else:
        print("跳过 error_glibc.c（已存在）")

    cmake = os.path.join(root, "CMakeLists.txt")
    with open(cmake, "r", encoding="utf-8") as f:
        s = f.read()
    src_entry = '"${BOX64_ROOT}/src/libtools/error_glibc.c"'
    if src_entry not in s:
        anchor = '"${BOX64_ROOT}/src/libtools/obstack_glibc.c"'
        assert anchor in s, f"CMakeLists.txt 找不到锚点 {anchor}"
        s = s.replace(anchor, anchor + "\n    " + src_entry, 1)
        with open(cmake, "w", encoding="utf-8") as f:
            f.write(s)
        print("CMakeLists.txt: 已把 error_glibc.c 加入无条件 ELFLOADER_SRC")


def inject_scandirat(root: str):
    """注入 scandirat 实现（musl 无此函数）：
    1. scandirat.c → src/libtools/
    2. CMakeLists 追加 scandirat_glibc.c 编译源
    """
    libtools = os.path.join(root, "src", "libtools")
    dst_c = os.path.join(libtools, "scandirat_glibc.c")
    src_c = os.path.join(SCANDIRAT_DIR, "scandirat.c")
    if not os.path.exists(dst_c):
        with open(src_c, "r", encoding="utf-8") as f:
            content = f.read()
        with open(dst_c, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"写入 {dst_c}")
    else:
        print("跳过 scandirat_glibc.c（已存在）")

    cmake = os.path.join(root, "CMakeLists.txt")
    with open(cmake, "r", encoding="utf-8") as f:
        s = f.read()
    src_entry = '"${BOX64_ROOT}/src/libtools/scandirat_glibc.c"'
    if src_entry not in s:
        anchor = '"${BOX64_ROOT}/src/libtools/error_glibc.c"'
        assert anchor in s, f"CMakeLists.txt 找不到锚点 {anchor}"
        s = s.replace(anchor, anchor + "\n    " + src_entry, 1)
        with open(cmake, "w", encoding="utf-8") as f:
            f.write(s)
        print("CMakeLists.txt: 已把 scandirat_glibc.c 加入无条件 ELFLOADER_SRC")


def inject_missing_symbols(root: str):
    """为 STATICBUILD 下 musl 缺失的 glibc 符号生成 weak stub：
    解析 src/wrapped/wrappedlibc_private.h 的全部宏条目，收集需 & 地址的符号
    （GO/GOW/GOD/GOWD/GO2/GOW2 的 N/O 与 DATA/DATAV/DATAB 的 N），
    生成 src/libtools/glibc_missing_symbols.c（__attribute__((weak)) 定义）。
    weak 定义在静态链接时：musl libc 有同名强符号则被覆盖（安全），
    缺失的则提供地址，保证 symbolmap 的 &N/&O 编译链接通过。
    """
    priv = os.path.join(root, "src", "wrapped", "wrappedlibc_private.h")
    assert os.path.exists(priv), f"找不到 {priv}"
    libtools = os.path.join(root, "src", "libtools")
    dst_c = os.path.join(libtools, "glibc_missing_symbols.c")
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gen_missing_symbols.py")
    subprocess.run([sys.executable, script, priv, dst_c], check=True)
    print(f"写入 {dst_c}")

    cmake = os.path.join(root, "CMakeLists.txt")
    with open(cmake, "r", encoding="utf-8") as f:
        s = f.read()
    src_entry = '"${BOX64_ROOT}/src/libtools/glibc_missing_symbols.c"'
    if src_entry not in s:
        anchor = '"${BOX64_ROOT}/src/libtools/scandirat_glibc.c"'
        assert anchor in s, f"CMakeLists.txt 找不到锚点 {anchor}"
        s = s.replace(anchor, anchor + "\n    " + src_entry, 1)
        with open(cmake, "w", encoding="utf-8") as f:
            f.write(s)
        print("CMakeLists.txt: 已把 glibc_missing_symbols.c 加入无条件 ELFLOADER_SRC")


root = sys.argv[1]
include_dir = sys.argv[2] if len(sys.argv) > 2 else None

inject_fts(root, include_dir)
inject_obstack(root, include_dir)
inject_error(root, include_dir)
inject_scandirat(root)
inject_missing_symbols(root)
if include_dir:
    write_stub_headers(include_dir)
    inject_glibc_headers(include_dir)

total = 0
for dirpath, _dirs, files in os.walk(os.path.join(root, "src")):
    for fn in files:
        if not fn.endswith(".c") and not fn.endswith(".h"):
            continue
        path = os.path.join(dirpath, fn)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            s = f.read()
        new = patch_text(s)
        n = sum(s.count(k) for k in REPL)
        n += sum(s.count(k) for k in UNDERSCORE_TYPES)
        if fn == "os_linux.c":
            new, m = remove_mallopt_block(new)
            n += m
        if fn == "mysignal.h":
            new, m = patch_mysignal_h(new)
            n += m
        if fn == "signal32.c":
            new, m = patch_signal32_c(new)
            n += m
        if fn == "threads.c":
            new, m = patch_threads_c(new)
            n += m
        if fn == "threads32.c":
            new, m = patch_threads32_c(new)
            n += m
        if fn == "libc_net32.c":
            new, m = patch_libc_net32_c(new)
            n += m
        if fn == "wrappedlibc.c":
            if "wrapped32" in path:
                new, m = patch_wrapped32_libc_c(new)
            else:
                new, m = patch_wrappedlibc_c(new)
            n += m
        if n:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new)
            total += n
            print(f"{path}: {n} 处")
print(f"共替换 {total} 处")
if not total:
    print("警告: 未找到需要替换的 glibc 浮点宏")