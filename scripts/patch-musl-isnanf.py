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

# 2) mmap64/MAP_32BIT：musl 无（mmap 本就是 64 位）
THREADS_MMAP = "mmap64(NULL, stacksize, PROT_READ|PROT_WRITE, MAP_PRIVATE|MAP_ANONYMOUS|MAP_32BIT, -1, 0)"
THREADS_MMAP_PATCH = "mmap(NULL, stacksize, PROT_READ|PROT_WRITE, MAP_PRIVATE|MAP_ANONYMOUS, -1, 0)"

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
        (THREADS_MMAP, THREADS_MMAP_PATCH),
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


def fetch(url: str) -> str:
    print(f"下载 {url}")
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read().decode("utf-8")


def inject_fts(root: str, include_dir: str = None):
    """注入 musl-fts：下载 fts.c/fts.h → src/libtools/，改 CMakeLists 编译 fts.c。
    fts.h 也复制到 include_dir（CMake 不搜索 src/libtools，#include <fts.h> 用尖括号）。"""
    libtools = os.path.join(root, "src", "libtools")
    if include_dir:
        os.makedirs(include_dir, exist_ok=True)
    for name in ("fts.c", "fts.h"):
        dst = os.path.join(libtools, name)
        if os.path.exists(dst):
            print(f"跳过 {name}（已存在）")
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


root = sys.argv[1]
include_dir = sys.argv[2] if len(sys.argv) > 2 else None

inject_fts(root, include_dir)
if include_dir:
    write_stub_headers(include_dir)

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
        if fn == "threads.c":
            new, m = patch_threads_c(new)
            n += m
        if n:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new)
            total += n
            print(f"{path}: {n} 处")
print(f"共替换 {total} 处")
if not total:
    print("警告: 未找到需要替换的 glibc 浮点宏")