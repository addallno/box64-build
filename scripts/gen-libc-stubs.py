#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen-libc-stubs.py — 为 box64 STATICBUILD + musl 静态链接生成 glibc 缺失符号的 weak stub

背景
----
box64 在 STATICBUILD 模式下，wrappedlibc.c 通过 wrappedlib_init.h 的宏
`GO(N,W) {#N, W, 0, &N},` 展开 wrappedlibc_private.h 里的全部条目，
生成 libc_symbolmap / datamap 等静态符号表。表项里的 `&N` / `(void*)&N`
要求这些符号在编译/链接时真实存在。

这些符号是 glibc 的 ABI。musl 的 libc.a 不提供其中的几百个（glibc 专有函数、
glibc 内部 `__xxx` 符号、`_IO_*` 流对象、LFS64 系列等）。本脚本：
  1) 提取 musl 的全部可用全局符号集（dynamic.list ∪ weak/strong_alias ∪
     源码顶层非 static/hidden 的函数/数据定义；或直接读取 `nm` 输出，更精确）
  2) 解析 wrappedlibc_private.h，收集 box64 引用的符号
  3) 差集得到“musl 中不存在”的符号
  4) 为这些符号生成一个独立的 C 文件（weak 定义 stub），编译进 box64，
     使 `&N` 有合法地址，链接不再报 undefined。

关键设计
--------
* 只用 weak 定义：与 musl 真实符号重名时，musl 的强/弱定义优先，绝不覆盖；
  只在 musl 确实没有时才成为最终定义（此时它是唯一定义）。
* 输出为独立 .c（不是 -include 头）：避免与 wrappedlibc.c 内 static_libc.h
  的 `extern` 声明（同类型签名）在同一翻译单元冲突。
* 对已知原型（从 box64 的 static_libc.h extern 声明提取）生成签名精确的 stub，
  返回 0/NULL；其余用 `intptr_t name(void)` 兜底。
* 数学判定符号（isnan/isinf/isfinite/signbit 及 __ 变体）用 GCC builtin 实现，
  保证运行时语义正确（NaN/Inf 判断）。
"""

import argparse
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

# ---------------------------------------------------------------- musl 符号集

# C 关键字 / 类型词，函数/数据解析时过滤误报
_KWD = {
    'if', 'while', 'for', 'switch', 'return', 'case', 'do', 'else', 'goto',
    'typedef', 'sizeof', 'typeof', '_Static_assert', 'void', 'int', 'char',
    'long', 'short', 'float', 'double', 'unsigned', 'signed', 'const',
    'volatile', 'static', 'extern', 'inline', 'struct', 'union', 'enum',
    'noreturn', 'hidden', 'restrict', 'auto', 'register', 'bool', '_Bool',
    'wchar_t', 'size_t', 'ssize_t', 'int32_t', 'uint32_t', 'int64_t',
    'uint64_t', 'intptr_t', 'uintptr_t', 'off_t', 'pid_t', 'time_t', 'FILE',
    'va_list',
}


def _strip_conditionals(s: str) -> str:
    """删除 `#if 0` 等禁用代码块（含其中误解析的函数/数据定义）。"""
    out = []
    skip = 0
    for line in s.splitlines(True):
        if re.match(r"\s*#\s*if\s+(0|1L|1==0|0L)\b", line):
            skip += 1
            continue
        if skip and re.match(r"\s*#\s*endif\b", line):
            skip -= 1
            continue
        if not skip:
            out.append(line)
    return "".join(out)


def _strip_comments(s: str) -> str:
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    s = re.sub(r"//[^\n]*", "", s)
    return s


def _strip_preproc(s: str) -> str:
    return re.sub(r"(?m)^[ \t]*#[^\n]*(\\\n[^\n]*)*\n?", "", s)


_FN_RE = re.compile(r"(?m)([A-Za-z_]\w*)\s*\([^;{}]*?\)\s*(?:\{)")


def _strip_init(s: str) -> str:
    """去掉 '=' 之后的初始化表达式（括号平衡，停在顶层 , 或 ;）"""
    out = []
    i, n = 0, len(s)
    while i < n:
        if s[i] == '=':
            j = i + 1
            depth = 0
            while j < n:
                c = s[j]
                if c in '([{':
                    depth += 1
                elif c in ')]}':
                    depth -= 1
                elif (c == ',' or c == ';') and depth <= 0:
                    break
                j += 1
            i = j
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


_NAME_RE = re.compile(r"([A-Za-z_]\w*)(?=\s*(?:\[[^\]]*\])?\s*(?:[,;]|$))")
_ALIAS_RE = re.compile(
    r"\b(?:weak|strong)_alias\s*\(\s*([A-Za-z_]\w*)\s*,\s*([A-Za-z_]\w*)\s*\)")


def _parse_musl_file(path: str, syms: set) -> None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            s = f.read()
    except OSError:
        return
    s = _strip_preproc(_strip_comments(_strip_conditionals(s)))

    for m in _ALIAS_RE.finditer(s):
        syms.add(m.group(1))
        syms.add(m.group(2))

    for m in _FN_RE.finditer(s):
        nm = m.group(1)
        if nm in _KWD:
            continue
        # 取名字所在行（连同上一行）检查 static/hidden 修饰
        line_start = s.rfind("\n", 0, m.start()) + 1
        if line_start > 0:
            line_start = s.rfind("\n", 0, line_start - 1) + 1
        pre = s[line_start:m.start()]
        if re.search(r"\b(?:static|hidden)\b", pre):
            continue
        syms.add(nm)

    for line in s.splitlines():
        ls = line.strip()
        if not ls:
            continue
        if re.match(r"^(static|hidden|extern)\b", ls):
            continue
        if not re.search(r"[=;,]$", ls):
            continue
        if re.match(r"^(typedef|struct|union|enum)\b", ls) or \
           re.match(r"^(struct|union|enum)\s+\w+\s*\{", ls):
            continue
        if re.search(r"\b(?:if|while|for|switch|return|goto|case)\b", ls):
            continue
        for nm in _NAME_RE.findall(_strip_init(ls)):
            if nm not in _KWD:
                syms.add(nm)


def parse_musl_src(musl_root: str, verbose: bool = False) -> set:
    """从 musl 源码提取全局符号集。宁多勿漏。"""
    syms = set()
    dyn = os.path.join(musl_root, "dynamic.list")
    if os.path.exists(dyn):
        with open(dyn, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"\s*([A-Za-z_]\w*)\s*;", line)
                if m:
                    syms.add(m.group(1))
    for root in ("src", "ldso"):
        base = os.path.join(musl_root, root)
        if not os.path.isdir(base):
            continue
        for dirpath, _dirnames, filenames in os.walk(base):
            for fn in filenames:
                if fn.endswith(".c"):
                    _parse_musl_file(os.path.join(dirpath, fn), syms)
    if verbose:
        print(f"[musl] 源码解析符号集大小: {len(syms)}")
    return syms


def parse_nm_syms(path: str) -> set:
    """读取 `nm -g --defined-only libc.a` 输出或纯符号名列表（每行一个）。"""
    syms = set()
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # nm 输出: 0000000000000000 T strlen  （或 "T strlen" 等）
            m = re.match(r"^[0-9a-fA-F]+\s+[A-Za-z]\s+(\S+)$", line)
            if m:
                syms.add(m.group(1))
            else:
                toks = line.split()
                if len(toks) == 2 and re.match(r"^[A-Za-z]$", toks[0]):
                    syms.add(toks[1])
                else:
                    syms.add(line)
    return syms


MUSL_TARBALL_URLS = [
    "https://github.com/ifduyue/musl/archive/refs/tags/v1.2.5.tar.gz",
    "https://musl.libc.org/releases/musl-1.2.5.tar.gz",
]


def download_musl(cache_dir: str, url: str = None) -> str:
    """下载并解压 musl 源码到 cache_dir，返回源码根目录。"""
    os.makedirs(cache_dir, exist_ok=True)
    tar_path = os.path.join(cache_dir, "musl-1.2.5.tar.gz")
    if not os.path.exists(tar_path):
        urls = [url] if url else MUSL_TARBALL_URLS
        last_err = None
        for u in urls:
            try:
                print(f"[musl] 下载 {u}")
                urllib.request.urlretrieve(u, tar_path)
                last_err = None
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                print(f"[musl] 下载失败: {e}")
        if last_err:
            sys.exit(f"无法下载 musl 源码: {last_err}")
    root = os.path.join(cache_dir, "musl-1.2.5")
    if not os.path.isdir(root):
        with tarfile.open(tar_path) as t:
            t.extractall(cache_dir)
    return root


# ----------------------------------------------------------- box64 引用符号

_PRIV_MACRO_RE = re.compile(
    r"^(GOD|GOWD|GO|GOW)\(([A-Za-z_]\w*),")
_PRIV_MACRO2_RE = re.compile(
    r"^(GO2|GOW2|GOD|GOWD)\(([A-Za-z_]\w*),([^,]+),\s*([A-Za-z_]\w*)\)")
_PRIV_DATA_RE = re.compile(r"^(DATA|DATAB|DATAV)\(([A-Za-z_]\w*),\s*(\d+)\)")


def parse_private_refs(priv_path: str) -> tuple:
    """
    解析 wrappedlibc_private.h。
    返回 (func_refs, data_refs)：
      func_refs: {sym: 来源宏};  GOM/GOWM/DATAM 的 my_ 符号不返回。
      data_refs: {sym: (size, 来源宏)}
    """
    func_refs = {}
    data_refs = {}
    for line in open(priv_path, encoding="utf-8"):
        t = line.strip()
        if not t or t.startswith("//"):
            continue
        m = _PRIV_DATA_RE.match(t)
        if m:
            data_refs[m.group(2)] = (int(m.group(3)), m.group(1))
            continue
        m = _PRIV_MACRO2_RE.match(t)
        if m:
            o = m.group(4)
            if not o.startswith("my_"):
                func_refs[o] = m.group(1)
            continue
        m = _PRIV_MACRO_RE.match(t)
        if m:
            func_refs[m.group(2)] = m.group(1)
    return func_refs, data_refs


def parse_static_libc_signatures(h_path: str) -> dict:
    """解析 box64 的 src/libtools/static_libc.h 中的 extern 声明，得到签名。

    返回 {sym: (return_type, params_text)}；只取单行、以 `;` 结尾的 extern 声明。
    """
    sig = {}
    re_ext = re.compile(
        r"^\s*extern\s+(?P<rest>[^;]+?)\s*;\s*(?://.*)?$")
    re_fn = re.compile(
        r"^(?P<ret>.+?)\s+(?P<name>[A-Za-z_]\w*)\s*\((?P<params>[^()]*)\)\s*$")
    for line in open(h_path, encoding="utf-8"):
        m = re_ext.match(line)
        if not m:
            continue
        fn = re_fn.match(m.group("rest").strip())
        if fn and not fn.group("name").startswith("my_"):
            sig[fn.group("name")] = (fn.group("ret"), fn.group("params"))
    return sig


# ------------------------------------------------------------- 智能数学 stub

# 数学判定符号：glibc 导出函数，musl 用宏实现（无符号）。
# 用 GCC builtin 生成，保证 NaN/Inf/符号位判定在运行时语义正确。
SMART_MATH = {
    "__isnan":   "int __isnan(double x) { return __builtin_isnan(x); }",
    "__isnanf":  "int __isnanf(float x) { return __builtin_isnan(x); }",
    "__isnanl":  "int __isnanl(long double x) { return __builtin_isnan(x); }",
    "isnan":     "int isnan(double x) { return __builtin_isnan(x); }",
    "isnanf":    "int isnanf(float x) { return __builtin_isnan(x); }",
    "isnanl":    "int isnanl(long double x) { return __builtin_isnan(x); }",
    "__isinf":   "int __isinf(double x) { return __builtin_isinf(x); }",
    "__isinff":  "int __isinff(float x) { return __builtin_isinf(x); }",
    "__isinfl":  "int __isinfl(long double x) { return __builtin_isinf(x); }",
    "isinf":     "int isinf(double x) { return __builtin_isinf(x); }",
    "isinff":    "int isinff(float x) { return __builtin_isinf(x); }",
    "isinfl":    "int isinfl(long double x) { return __builtin_isinf(x); }",
    "__finite":  "int __finite(double x) { return __builtin_isfinite(x); }",
    "finite":    "int finite(double x) { return __builtin_isfinite(x); }",
    "__finitef": "int __finitef(float x) { return __builtin_isfinite(x); }",
    "finitef":   "int finitef(float x) { return __builtin_isfinite(x); }",
    "__finitel": "int __finitel(long double x) { return __builtin_isfinite(x); }",
    "finitel":   "int finitel(long double x) { return __builtin_isfinite(x); }",
    "__signbit": "int __signbit(double x) { return __builtin_signbit(x); }",
    "__signbitf": "int __signbitf(float x) { return __builtin_signbit(x); }",
    "__signbitl": "int __signbitl(long double x) { return __builtin_signbit(x); }",
}

# musl 中不存在但 box64 引用、且签名已知的符号（static_libc.h 之外的补充）。
# 格式: sym -> 完整函数定义文本
# 重要：musl 的 off_t 恒为 64 位，因此所有 LFS64 接口可直接转发到无后缀版本。
SMART_EXTRA = {
    # glibc 内部 errno 访问器（等价 __errno_location）
    "__errno": "void* __errno(void) { return (void*)&errno; }",
    # glibc 向 C99 的转发（返回 int，strfrom*）
    "strfromd":   "int strfromd(char* buf, size_t n, const char* fmt, double x) { return 0; }",
    "strfromf":   "int strfromf(char* buf, size_t n, const char* fmt, float x) { return 0; }",
    "strfromf32": "int strfromf32(char* buf, size_t n, const char* fmt, float x) { return 0; }",
    "strfromf64": "int strfromf64(char* buf, size_t n, const char* fmt, double x) { return 0; }",
    "strfroml":   "int strfroml(char* buf, size_t n, const char* fmt, long double x) { return 0; }",
    # glibc _l 系列（musl 的 newlocale/strtod_l 等已提供，但 *_l 公共名缺失时转发）
    "strtol_l":   "long strtol_l(const char* nptr, char** endptr, int base, void* loc) { (void)loc; return strtol(nptr, endptr, base); }",
    "strtoll_l":  "long long strtoll_l(const char* nptr, char** endptr, int base, void* loc) { (void)loc; return strtoll(nptr, endptr, base); }",
    "strtoul_l":  "unsigned long strtoul_l(const char* nptr, char** endptr, int base, void* loc) { (void)loc; return strtoul(nptr, endptr, base); }",
    "strtoull_l": "unsigned long long strtoull_l(const char* nptr, char** endptr, int base, void* loc) { (void)loc; return strtoull(nptr, endptr, base); }",
    "wcstol_l":   "long wcstol_l(const wchar_t* nptr, wchar_t** endptr, int base, void* loc) { (void)loc; return wcstol(nptr, endptr, base); }",
    "wcstoll_l":  "long long wcstoll_l(const wchar_t* nptr, wchar_t** endptr, int base, void* loc) { (void)loc; return wcstoll(nptr, endptr, base); }",
    "wcstoul_l":  "unsigned long wcstoul_l(const wchar_t* nptr, wchar_t** endptr, int base, void* loc) { (void)loc; return wcstoul(nptr, endptr, base); }",
    "wcstoull_l": "unsigned long long wcstoull_l(const wchar_t* nptr, wchar_t** endptr, int base, void* loc) { (void)loc; return wcstoull(nptr, endptr, base); }",
    "wcstof_l":   "float wcstof_l(const wchar_t* nptr, wchar_t** endptr, void* loc) { (void)loc; return wcstof(nptr, endptr); }",
    "wcstod_l":   "double wcstod_l(const wchar_t* nptr, wchar_t** endptr, void* loc) { (void)loc; return wcstod(nptr, endptr); }",
    "wcstold_l":  "long double wcstold_l(const wchar_t* nptr, wchar_t** endptr, void* loc) { (void)loc; return wcstold(nptr, endptr); }",
    # ---- LFS64 接口：musl 全是宏/无符号，转发到 64 位等价实现 ----
    "getdents64":   "long getdents64(int fd, void* dirp, size_t count) { return syscall(SYS_getdents64, fd, dirp, count); }",
    "getdirentries64": "ssize_t getdirentries64(int fd, void* buf, size_t n, off_t* basep) { (void)basep; return syscall(SYS_getdents64, fd, buf, n); }",
    "readdir64":    "void* readdir64(void* dirp) { return readdir(dirp); }",
    "readdir64_r":  "int readdir64_r(void* dirp, void* entry, void** result) { return readdir_r(dirp, entry, result); }",
    "creat64":      "int creat64(const char* path, mode_t mode) { return creat(path, mode); }",
    "freopen64":    "FILE* freopen64(const char* path, const char* mode, FILE* f) { return freopen(path, mode, f); }",
    "fseeko64":     "int fseeko64(FILE* f, off_t off, int whence) { return fseeko(f, off, whence); }",
    "ftello64":     "off_t ftello64(FILE* f) { return ftello(f); }",
    "tmpfile64":    "FILE* tmpfile64(void) { return tmpfile(); }",
    "lseek64":      "off_t lseek64(int fd, off_t off, int whence) { return lseek(fd, off, whence); }",
    "pread64":      "ssize_t pread64(int fd, void* buf, size_t n, off_t off) { return pread(fd, buf, n, off); }",
    "pwrite64":     "ssize_t pwrite64(int fd, void* buf, size_t n, off_t off) { return pwrite(fd, buf, n, off); }",
    "truncate64":   "int truncate64(const char* path, off_t len) { return truncate(path, len); }",
    "ftruncate64":  "int ftruncate64(int fd, off_t len) { return ftruncate(fd, len); }",
    "lockf64":      "int lockf64(int fd, int cmd, off_t len) { return lockf(fd, cmd, len); }",
    "statfs64":     "int statfs64(const char* path, void* buf) { return statfs(path, buf); }",
    "fstatfs64":    "int fstatfs64(int fd, void* buf) { return fstatfs(fd, buf); }",
    "statvfs64":    "int statvfs64(const char* path, void* buf) { return statvfs(path, buf); }",
    "fstatvfs64":   "int fstatvfs64(int fd, void* buf) { return fstatvfs(fd, buf); }",
    "mkstemp64":    "int mkstemp64(char* t) { return mkstemp(t); }",
    "mkostemp64":   "int mkostemp64(char* t, int flags) { return mkostemp(t, flags); }",
    "mkstemps64":   "int mkstemps64(char* t, int slen) { return mkstemps(t, slen); }",
    "mkostemps64":  "int mkostemps64(char* t, int slen, int flags) { return mkostemps(t, slen, flags); }",
    "sendfile64":   "ssize_t sendfile64(int out, int in, off_t* off, size_t n) { return sendfile(out, in, off, n); }",
    "posix_fallocate64": "int posix_fallocate64(int fd, off_t off, off_t len) { return posix_fallocate(fd, off, len); }",
    "posix_fadvise64":   "int posix_fadvise64(int fd, off_t off, off_t len, int advice) { return posix_fadvise(fd, off, len, advice); }",
}


def build_smart_map(missing: set) -> dict:
    """返回 {sym: 完整定义文本}，只含缺失且已知实现的符号。"""
    out = {}
    for sym, impl in SMART_MATH.items():
        if sym in missing:
            out[sym] = impl
    for sym, impl in SMART_EXTRA.items():
        if sym in missing:
            out[sym] = impl
    return out


# ---------------------------------------------------------------- 生成 stub

_HEADER = """\
/* 自动生成：box64 STATICBUILD + musl 静态链接下的 glibc 缺失符号 weak stub
 * 生成器: scripts/gen-libc-stubs.py（请勿手工编辑）
 *
 * 说明:
 *  - 所有 stub 均为 weak 定义。musl libc.a 中真实存在的符号（强/弱）优先，
 *    本文件只兜底 musl 不存在的符号，绝不覆盖真实实现。
 *  - 函数 stub 返回 0/NULL（intptr_t 兜底），保证 &N 有合法地址，
 *    x86 程序真正调用到这些 glibc 专有接口时行为退化为“空实现”。
 *  - 数学判定符号用 GCC builtin 实现，NaN/Inf 语义正确。
 */
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <poll.h>
#include <signal.h>
#include <time.h>
#include <locale.h>
#include <regex.h>
#include <netinet/in.h>
#include <limits.h>
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>
#include <dirent.h>
#include <sys/statfs.h>
#include <sys/statvfs.h>
#include <sys/sendfile.h>
#include <sys/syscall.h>
"""


def _render_func_stub(name: str, sig=None):
    """返回一条函数 stub 文本。sig 为 (ret, params) 或 None。"""
    if sig:
        ret, params = sig
        if ret.strip() == "void":
            body = "{}"
        else:
            body = "{ return 0; }"
        return f"__attribute__((weak)) {ret} {name}({params}) {body}"
    return f"__attribute__((weak)) intptr_t {name}(void) {{ return 0; }}"


def _render_data_stub(name: str, size: int):
    return (f"__attribute__((weak)) unsigned char {name}[{size}]"
            f" __attribute__((aligned(16)));")


def generate_stubs(missing_funcs, missing_datas, sigs, smart, out_path):
    lines = [_HEADER]
    lines.append("/* ================= 函数 stub ================= */")
    for name in sorted(missing_funcs):
        if name in smart:
            lines.append(smart[name])
        elif name in sigs:
            lines.append(_render_func_stub(name, sigs[name]))
        else:
            lines.append(_render_func_stub(name))
    lines.append("")
    lines.append("/* ================= 数据 stub ================= */")
    for name in sorted(missing_datas):
        lines.append(_render_data_stub(name, missing_datas[name]))
    lines.append("")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return len(lines)


# -------------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--box64-src", required=True, help="box64 源码根目录")
    ap.add_argument("--private", default=None,
                    help="wrappedlibc_private.h 路径（默认 box64-src/src/wrapped/wrappedlibc_private.h）")
    ap.add_argument("--static-libc-h", default=None,
                    help="static_libc.h 路径（默认 box64-src/src/libtools/static_libc.h）")
    ap.add_argument("--output", required=True, help="输出 stub .c 文件路径")
    ap.add_argument("--musl-src", default=None,
                    help="musl 源码目录（提供则跳过下载）")
    ap.add_argument("--musl-url", default=None, help="musl tarball 下载 URL（可选）")
    ap.add_argument("--musl-syms", default=None,
                    help="nm 提取的符号文件（优先；最精确）")
    ap.add_argument("--cache-dir", default=None,
                    help="musl tarball 缓存目录（默认 <box64-src 同级目录>/.cache）")
    ap.add_argument("--force-stub", action="append", default=[],
                    help="强制对某符号生成 stub（即使符号集认为 musl 存在）")
    ap.add_argument("--no-stub", action="append", default=[],
                    help="强制跳过某符号（即使缺失）")
    ap.add_argument("--check", action="store_true",
                    help="若检测到交叉 gcc，对生成的 stub.c 做 -fsyntax-only 校验")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    priv = args.private or os.path.join(args.box64_src, "src", "wrapped",
                                        "wrappedlibc_private.h")
    slh = args.static_libc_h or os.path.join(args.box64_src, "src", "libtools",
                                             "static_libc.h")
    for p in (priv, slh):
        if not os.path.isfile(p):
            sys.exit(f"找不到文件: {p}")

    # 1. musl 符号集
    if args.musl_syms:
        musl_syms = parse_nm_syms(args.musl_syms)
        print(f"[musl] 使用 nm 符号文件 {args.musl_syms}: {len(musl_syms)} 个")
    else:
        musl_root = args.musl_src
        if musl_root is None:
            cache = args.cache_dir or os.path.join(
                os.path.dirname(os.path.dirname(args.box64_src)), ".cache")
            musl_root = download_musl(cache, args.musl_url)
        musl_syms = parse_musl_src(musl_root, args.verbose)
        print(f"[musl] 源码解析符号集: {len(musl_syms)} 个")

    # 2. box64 引用符号
    func_refs, data_refs = parse_private_refs(priv)
    sigs = parse_static_libc_signatures(slh)
    if args.verbose:
        print(f"[box64] 引用函数 {len(func_refs)}、数据 {len(data_refs)}、"
              f"static_libc.h 签名 {len(sigs)}")

    # 3. 差集
    missing_funcs = {s for s in func_refs if s not in musl_syms}
    missing_datas = {s: sz for s, (sz, _m) in data_refs.items() if s not in musl_syms}

    for s in args.no_stub:
        missing_funcs.discard(s)
        missing_datas.pop(s, None)
    for s in args.force_stub:
        missing_funcs.add(s)

    smart = build_smart_map(missing_funcs)

    # 4. 生成
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    nlines = generate_stubs(missing_funcs, missing_datas, sigs, smart, args.output)

    print(f"[生成] 输出 {args.output}（{nlines} 行）")
    print(f"[统计] 缺失函数 stub: {len(missing_funcs)}，"
          f"其中智能实现(数学/转发): {len(smart)}")
    print(f"[统计] 缺失数据 stub: {len(missing_datas)}")
    # 风险提示
    risk = [s for s in missing_funcs if s.startswith("__errno")]
    risk += [s for s in ("_IO_2_1_stdin_", "_IO_2_1_stdout_", "_IO_2_1_stderr_",
                         "getdents64", "getdirentries64") if s in missing_funcs]
    if risk:
        print("[风险] 下列 stub 仅返回 0/NULL，相关功能可能失效:", " ".join(risk))

    # 5. 可选语法校验
    if args.check:
        cc = os.environ.get("MUSL_CC")
        if not cc:
            for c in ("aarch64-unknown-linux-musl-gcc", "musl-gcc"):
                if subprocess.call(["which", c],
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL) == 0:
                    cc = c
                    break
        if cc:
            r = subprocess.run([cc, "-fsyntax-only", args.output],
                               capture_output=True, text=True)
            if r.returncode == 0:
                print(f"[check] {cc} -fsyntax-only 通过")
            else:
                print(f"[check] {cc} -fsyntax-only 失败:")
                print(r.stderr)
                sys.exit(1)
        else:
            print("[check] 未找到交叉 gcc（设置环境变量 MUSL_CC），跳过语法校验")


if __name__ == "__main__":
    main()