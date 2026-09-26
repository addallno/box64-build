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
    r"^(GOD|GOWD|GO|GOW|GOS|GOWS)\(([A-Za-z_]\w*),")
# 允许 name@VERSION（如 pthread_cond_broadcast@GLIBC_2.0）——版本后缀不是 C 标识符，
# 注册前须 strip；目标 O（my32_*）才是需声明的符号。
_PRIV_MACRO2_RE = re.compile(
    r"^(GO2|GOW2|GOD|GOWD)\(([A-Za-z_]\w*(?:@[A-Za-z0-9_.]+)?),([^,]+),\s*([A-Za-z_]\w*)\)")
_PRIV_GOM_RE = re.compile(r"^(GOM|GOWM)\(([A-Za-z_]\w*)")
_PRIV_GOS_RE = re.compile(r"^(GOS|GOWS)\(([A-Za-z_]\w*)")
_PRIV_DATAM_RE = re.compile(r"^(DATAM)\(([A-Za-z_]\w*),\s*([^)]+)\)")
_PRIV_DATA_RE = re.compile(r"^(DATA|DATAB|DATAV)\(([A-Za-z_]\w*),\s*([^)]+)\)")


def parse_private_refs(priv_path: str) -> tuple:
    """
    解析 wrappedlibc_private.h。
    返回 (func_refs, data_refs)：
      func_refs: {sym: 来源宏};  包含 my32_ 前缀符号（STATICBUILD 需要声明）。
      data_refs: {sym: (size, 来源宏)}
    """
    func_refs = {}
    data_refs = {}
    for line in open(priv_path, encoding="utf-8"):
        t = line.strip()
        if not t or t.startswith("//"):
            continue
        # DATA/DATAB/DATAV/DATAM: 数据符号（DATAM 同时登记 my32_ 映射名）
        m = _PRIV_DATA_RE.match(t) or _PRIV_DATAM_RE.match(t)
        if m:
            try:
                sz = int(m.group(3))
            except ValueError:
                sz = 256  # sizeof(...) 等非数字大小，默认 256
            data_refs[m.group(2)] = (sz, m.group(1))
            if m.group(1) == "DATAM":
                data_refs["my32_" + m.group(2)] = (sz, "DATAM")
            continue
        # GOM/GOWM: 函数符号，收集原始名称和 my32_ 映射名称
        m = _PRIV_GOM_RE.match(t)
        if m:
            n = m.group(2)
            func_refs[n] = m.group(1)
            func_refs["my32_" + n] = m.group(1)
            continue
        # GOS/GOWS: 结构返回，STATICBUILD → &my32_N（与 GOM 同样映射）
        m = _PRIV_GOS_RE.match(t)
        if m:
            n = m.group(2)
            func_refs[n] = m.group(1)
            func_refs["my32_" + n] = m.group(1)
            continue
        # GO2/GOW2: 收集原始名称和映射目标名称（含 my32_）
        m = _PRIV_MACRO2_RE.match(t)
        if m:
            n = m.group(2)  # 原始名称，如 "execl" 或 "pthread_kill@GLIBC_2.0"
            o = m.group(4)  # 映射目标，如 "my32_execv"
            # 版本后缀（@GLIBC_x.y）不是合法 C 标识符；strip 后再注册（常已被 GOM 收录）
            if "@" in n:
                n = n.split("@", 1)[0]
            if n:
                func_refs[n] = m.group(1)
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


def parse_static_libc_symbols(h_path: str) -> set:
    """解析 static_libc.h 中所有已声明或定义的函数名（包括 extern 声明、static inline 和普通定义）。

    返回函数名集合；generate_header() 中跳过这些符号，避免与 static_libc.h 冲突。
    """
    syms = set()
    # extern 声明: extern TYPE NAME(...)
    re_ext = re.compile(
        r"^\s*extern\s+\S+\s+(\w+)\s*\(")
    # 普通函数定义: TYPE NAME(...) {
    re_def = re.compile(
        r"^\s*\S+\s+(\w+)\s*\([^)]*\)\s*\{")
    # static inline 函数: static inline TYPE NAME(...) {
    re_static = re.compile(
        r"^\s*static\s+inline\s+\S+\s+(\w+)\s*\(")
    for line in open(h_path, encoding="utf-8"):
        m = re_static.match(line)
        if m:
            syms.add(m.group(1))
            continue
        m = re_ext.match(line)
        if m:
            syms.add(m.group(1))
            continue
        m = re_def.match(line)
        if m:
            syms.add(m.group(1))
    return syms


def parse_box32_sigs(*scan_dirs) -> dict:
    """扫描目录列表 *.c 中 my32_* 函数定义，返回 {name: (ret, params)}。

    支持多行签名与嵌套括号（如 int *(main)(int, char**, char**)）。
    供 generate_header 为已有本地定义的 my32_ 生成兼容签名，避免 conflicting types。
    """
    sigs = {}
    # 头部: [static|EXPORT] ret [EXPORT] my32_name (  —— ret 多词(unsigned long等)+紧邻/分离 *
    head_re = re.compile(
        r"(?:^|\n)(?:static\s+|EXPORT\s+)*"
        r"((?:const\s+)?(?:[A-Za-z_][\w]*\s+)*?[A-Za-z_][\w]*(?:\s*\*+)*)"
        r"\s*(?:EXPORT\s+)?"
        r"(my32_[A-Za-z0-9_]+)\s*\(")
    for wrapped32_dir in scan_dirs:
        if not wrapped32_dir or not os.path.isdir(wrapped32_dir):
            continue
        for fn in sorted(os.listdir(wrapped32_dir)):
            if not fn.endswith(".c"):
                continue
            path = os.path.join(wrapped32_dir, fn)
            try:
                text = open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            for m in head_re.finditer(text):
                ret = re.sub(r"\bEXPORT\b", "", m.group(1)).strip()
                name = m.group(2)
                # 从 ( 起平衡括号提取参数
                i = m.end() - 1
                depth = 0
                j = i
                while j < len(text):
                    c = text[j]
                    if c == "(":
                        depth += 1
                    elif c == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                if j >= len(text):
                    continue
                params = text[i + 1:j]
                params = re.sub(r"\bEXPORT\b", "", params)
                params = re.sub(r"\s+", " ", params).strip()
                after = text[j + 1:j + 40]
                # 只取定义（{）或 EXPORT 声明（; 或 __attribute__ 后接 {）
                if "{" not in after.split(";")[0] and "EXPORT" not in m.group(0):
                    if ";" not in after and "{" not in after:
                        continue
                if ret in ("", "static") or "##" in name or "##" in params:
                    continue
                # 优先真实定义（{ 前无 ;）；否则首个匹配；勿盲目取更长 params
                # （避免同名声明覆盖定义，如 makecontext void*→int32_t*）
                is_def = "{" in after.split(";")[0]
                if name not in sigs:
                    sigs[name] = [ret, params, is_def, False]
                else:
                    cur = sigs[name]
                    # 同名 def/decl 参数不一致 → 标记冲突（共享头须用无参声明）
                    if params != cur[1] and is_def != cur[2]:
                        cur[3] = True
                    if is_def and not cur[2]:
                        cur[0], cur[1], cur[2] = ret, params, is_def
                    elif is_def == cur[2] and len(params) > len(cur[1]):
                        cur[0], cur[1] = ret, params
            # 展开本文件 FINITE 系列宏（F1F/F1D/F2F/F2D）：
            # EXPORT R my32___##N##_finite P 因含 ## 被 head_re 跳过，
            # 若不补录签名，path4 会生成 extern void(name)(void) 与真实定义冲突
            _FINITE_MACROS = {
                "F1F": ("float", "float a"),
                "F1D": ("double", "double a"),
                "F2F": ("float", "float a, float b"),
                "F2D": ("double", "double a, double b"),
            }
            finite_call_re = re.compile(
                r"^(F1F|F1D|F2F|F2D)\(([A-Za-z0-9_]+)\)\s*$", re.M)
            for m in finite_call_re.finditer(text):
                kind, base = m.group(1), m.group(2)
                name = f"my32___{base}_finite"
                if name not in sigs:
                    ret, params = _FINITE_MACROS[kind]
                    sigs[name] = [ret, params, True, False]
    # 规范为 {name: (ret, params, conflict)} 三元组
    return {n: (v[0], v[1], v[3]) for n, v in sigs.items()}


def scan_go2_my_targets(box64_src: str) -> set:
    """扫描所有 wrapped*_private.h 中 GO2/GOW2 的 my_* 映射目标。"""
    targets = set()
    re_go2 = re.compile(
        r"GO(?:W)?2\([^,]+,\s*[^,]+,\s*(my_[A-Za-z0-9_]+)\s*\)")
    for sub in ("wrapped", "wrapped32"):
        d = os.path.join(box64_src, "src", sub)
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if not fn.endswith("_private.h"):
                continue
            try:
                text = open(os.path.join(d, fn),
                            encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            targets.update(re_go2.findall(text))
    return targets


def parse_my64_sigs(box64_src: str) -> dict:
    """扫描 src/wrapped/*.c 中 my_* 函数定义签名（供 init32 注入）。"""
    sigs = {}
    wrapped_dir = os.path.join(box64_src, "src", "wrapped")
    if not os.path.isdir(wrapped_dir):
        return sigs
    pat = re.compile(
        r"^(?:static\s+|EXPORT\s+)?"
        r"((?:const\s+)?[A-Za-z_][\w\s\*]*?)\s+"
        r"(my_[A-Za-z0-9_]+)\s*\(([^)]*)\)")
    for fn in sorted(os.listdir(wrapped_dir)):
        if not fn.endswith(".c"):
            continue
        path = os.path.join(wrapped_dir, fn)
        try:
            for line in open(path, encoding="utf-8", errors="replace"):
                if line.startswith("#"):
                    continue
                m = pat.match(line.rstrip("\n"))
                if not m:
                    continue
                if "{" not in line and "EXPORT" not in line:
                    continue
                ret = m.group(1).replace("EXPORT", "").strip()
                name = m.group(2)
                params = re.sub(r"\bEXPORT\b", "", m.group(3)).strip()
                if ret in ("", "static") or "##" in name:
                    continue
                if name not in sigs or len(params) > len(sigs[name][1]):
                    sigs[name] = (ret, params)
        except OSError:
            continue
    return sigs


def inject_my64_decls_into_init32(box64_src: str) -> int:
    """在 wrappedlib_init32.h 中注入 GO2 目标 my_* 的 extern 声明。
    注入到 #include "glibc_missing_symbols.h" 之后（类型头已可见）。
    my_* 定义在64位 wrapped/*.c，不进共享 glibc_missing_symbols.h（避免64位冲突）。
    """
    init32 = os.path.join(box64_src, "src", "wrapped32", "wrappedlib_init32.h")
    if not os.path.isfile(init32):
        print(f"[init32] 跳过（不存在）: {init32}")
        return 0
    text = open(init32, encoding="utf-8").read()
    marker = '#include "glibc_missing_symbols.h"'
    if "my64_decls_begin" in text:
        print("[init32] my_* 声明已注入")
        return 0
    if marker not in text:
        print("[init32] 警告: 未找到 glibc_missing_symbols.h include，改用 debug.h 锚点")
        marker = '#include "debug.h"'
        if marker not in text:
            print("[init32] 警告: 无可用锚点，跳过 my_* 注入")
            return 0

    targets = scan_go2_my_targets(box64_src)
    if not targets:
        print("[init32] 未找到 GO2 my_* 目标")
        return 0
    my64 = parse_my64_sigs(box64_src)
    # static_*.h / signals.h 已有真实签名的 my_*：跳过注入（void 会 conflicting types）
    declared_elsewhere = set()
    for hpath in (
        os.path.join(box64_src, "src", "libtools", "static_threads.h"),
        os.path.join(box64_src, "src", "libtools", "static_libc.h"),
        os.path.join(box64_src, "src", "include", "signals.h"),
    ):
        if not os.path.isfile(hpath):
            continue
        for hline in open(hpath, encoding="utf-8", errors="replace"):
            hm = re.match(r"^\s*(?:extern\s+)?\S+\s+(my_\w+)\s*\(", hline)
            if hm:
                declared_elsewhere.add(hm.group(1))
    lines = [
        "/* my64_decls_begin: GO2 目标 my_* 声明（定义在64位 wrapped/*.c） */",
    ]
    # 无已知定义的目标：仍声明 void(void)（仅取地址；weak stub 或强定义在链接期解析）
    fallback = {
        "my_signal": ("sighandler_t", "int, sighandler_t"),
        "my___sysv_signal": ("sighandler_t", "int, sighandler_t"),
        "my_on_exit": ("int", "void*, int, void*"),
    }
    skipped = 0
    for name in sorted(targets):
        if name in declared_elsewhere:
            skipped += 1
            continue
        if name in my64:
            ret, params = my64[name]
            # 不安全类型（x64_va_list_t 等）降级 void，避免 unknown type
            if not _is_safe_shared_sig(ret, params):
                ret, params = "void", ""
        elif name in fallback:
            ret, params = fallback[name]
        else:
            ret, params = "void", ""
        if not params or params.strip() == "void":
            lines.append(f"extern {ret} {name}(void);")
        else:
            lines.append(f"extern {ret} {name}({params});")
    lines.append("/* my64_decls_end */")
    block = "\n".join(lines) + "\n\n"
    text = text.replace(marker, block + marker, 1)
    open(init32, "w", encoding="utf-8").write(text)
    n = len(targets) - skipped
    print(f"[init32] 已注入 {n} 个 my_* 声明（跳过 static 已声明 {skipped}）")
    return n


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
    # glibc memcmp 等价物（musl 不提供）：恒 0 stub = 恒“相等”，会静默破坏
    # guest 字符串/内容比较，必须转发 memcmp（签名 iFppL 与之完全一致）
    "__memcmpeq": "int __memcmpeq(const void* a, const void* b, size_t n) { return memcmp(a, b, n); }",
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
    # alphasort64：steamcmd 等 x86 程序经 GLOB_DAT 引用，必须保留符号名
    "alphasort64":  "int alphasort64(const struct dirent **a, const struct dirent **b) { return alphasort(a, b); }",
    "getdents64":   "long getdents64(int fd, void* dirp, size_t count) { return syscall(SYS_getdents64, fd, dirp, count); }",
    "getdirentries64": "ssize_t getdirentries64(int fd, void* buf, size_t n, off_t* basep) { (void)basep; return syscall(SYS_getdents64, fd, buf, n); }",
    "readdir64":    "void* readdir64(void* dirp) { return readdir((DIR*)dirp); }",
    "readdir64_r":  "int readdir64_r(void* dirp, void* entry, void** result) { return readdir_r((DIR*)dirp, (struct dirent*)entry, (struct dirent**)result); }",
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
    # glibc gamma/roundeven（musl 用宏定义 gamma→lgamma，roundeven 可能不可见）
    "gamma":     "double gamma(double x) { return lgamma(x); }",
    "gammaf":    "float gammaf(float x) { return lgammaf(x); }",
    "roundeven": "double roundeven(double x) { return x; }",
    "roundevenf": "float roundevenf(float x) { return x; }",
     # ---- FORTIFY _chk 系列：musl 无 glibc fortify，必须转发到非 chk 版本 ----
     # stub{return 0} 会让 guest 拿到 NULL/0 并继续使用（如 strcmp(NULL,...) 直接 SIGSEGV）
     # 签名必须与 box64 static_libc.h 的 extern 声明完全一致，否则 glibc_missing_symbols.h
     # （先 include static_libc.h 再声明 smart）会因 conflicting types 编译失败。
     "__memcpy_chk":   "void* __memcpy_chk(void* d, void* s, size_t n, size_t dn) { (void)dn; return memcpy(d, s, n); }",
     "__memmove_chk":  "void* __memmove_chk(void* d, void* s, size_t n, size_t dn) { (void)dn; return memmove(d, s, n); }",
     "__mempcpy_chk":  "void* __mempcpy_chk(void* d, void* s, size_t n, size_t dn) { (void)dn; return (char*)memcpy(d, s, n) + n; }",
     "__memset_chk":   "void* __memset_chk(void* d, int c, size_t n, size_t dn) { (void)dn; return memset(d, c, n); }",
     "__strcpy_chk":   "void* __strcpy_chk(char* d, const char* s, size_t dn) { (void)dn; return strcpy(d, s); }",
     "__strncpy_chk":  "void* __strncpy_chk(char* d, const char* s, size_t n, size_t dn) { (void)dn; return strncpy(d, s, n); }",
     "__strcat_chk":   "char* __strcat_chk(char* d, const char* s, size_t dn) { (void)dn; return strcat(d, s); }",
     "__strncat_chk":  "void* __strncat_chk(char* d, const char* s, size_t n, size_t dn) { (void)dn; return strncat(d, s, n); }",
     "__stpcpy_chk":   "char* __stpcpy_chk(char* d, const char* s, size_t dn) { (void)dn; size_t l = strlen(s); memcpy(d, s, l + 1); return d + l; }",
     "__stpncpy_chk":  "char* __stpncpy_chk(char* d, const char* s, size_t n, size_t dn) { (void)dn; char* r = stpncpy(d, s, n); return r; }",
     "__wmemcpy_chk":  "wchar_t* __wmemcpy_chk(wchar_t* d, const wchar_t* s, size_t n, size_t dn) { (void)dn; return wmemcpy(d, s, n); }",
     "__wmemmove_chk": "wchar_t* __wmemmove_chk(wchar_t* d, const wchar_t* s, size_t n, size_t dn) { (void)dn; return wmemmove(d, s, n); }",
     "__wmemset_chk":  "wchar_t* __wmemset_chk(wchar_t* d, wchar_t c, size_t n, size_t dn) { (void)dn; return wmemset(d, c, n); }",
     "__wcscpy_chk":   "wchar_t* __wcscpy_chk(wchar_t* d, const wchar_t* s, size_t dn) { (void)dn; return wcscpy(d, s); }",
     "__wcsncpy_chk":  "wchar_t* __wcsncpy_chk(wchar_t* d, const wchar_t* s, size_t n, size_t dn) { (void)dn; return wcsncpy(d, s, n); }",
     "__wcscat_chk":   "wchar_t* __wcscat_chk(wchar_t* d, const wchar_t* s, size_t dn) { (void)dn; return wcscat(d, s); }",
     "__wcsncat_chk":  "wchar_t* __wcsncat_chk(wchar_t* d, const wchar_t* s, size_t n, size_t dn) { (void)dn; return wcsncat(d, s, n); }",
     "__getcwd_chk":   "char* __getcwd_chk(char* b, size_t bn, size_t n) { (void)bn; return getcwd(b, n); }",
     # static_libc.h 为 4 参（无 buflen）；guest 经 box64 GO(pFpLip) 也只传 4 参
     "__fgets_chk":    "char* __fgets_chk(char* s, size_t n, int c, FILE* f) { (void)n; return fgets(s, c, f); }",
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


def _render_undefs(smart: dict) -> str:
    """为 SMART_MATH 中所有符号生成 #undef，防止 musl <math.h> 等头文件中的宏展开冲突。
    必须覆盖所有符号（不只是 missing 中的），因为 musl 可能定义 A→B 宏，
    导致一个 stub 的符号名被预处理器替换成另一个。"""
    undefs = []
    for sym in SMART_MATH:
        undefs.append(f"#ifdef {sym}")
        undefs.append(f"#undef {sym}")
        undefs.append("#endif")
    if undefs:
        return "\n".join(undefs) + "\n"
    return ""


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
/* glibc 专有类型别名，musl 无这些 typedef，但从 static_libc.h 签名引用 */
typedef uid_t __uid_t;
typedef gid_t __gid_t;
typedef pid_t __pid_t;
typedef void (*__sighandler_t)(int);
#define __sigset_t sigset_t
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


def generate_stubs(missing_funcs, missing_datas, sigs, smart, out_path,
                   musl_header_decls=None, force_names=None):
    decls = musl_header_decls or set()
    forced = force_names or set()
    data_syms = set(missing_datas.keys())
    lines = [_HEADER]
    undefs = _render_undefs(smart)
    if undefs:
        lines.append("/* 屏蔽 musl <math.h> 等头文件中的宏定义，避免与 stub 定义冲突 */")
        lines.append(undefs)
    lines.append("/* ================= 函数 stub ================= */")
    skipped_decl = 0
    skipped_data = 0
    for name in sorted(missing_funcs):
        if name in smart:
            lines.append(smart[name])
        elif name in decls and name not in forced:
            skipped_decl += 1
            continue
        elif name in data_syms:
            skipped_data += 1
            continue
        elif name in sigs:
            lines.append(_render_func_stub(name, sigs[name]))
        else:
            lines.append(_render_func_stub(name))
    if skipped_decl or skipped_data:
        print(f"[stubs] 跳过函数 stub: decls={skipped_decl}, data冲突={skipped_data}")
    lines.append("")
    lines.append("/* ================= 数据 stub ================= */")
    for name in sorted(missing_datas):
        lines.append(_render_data_stub(name, missing_datas[name]))
    lines.append("")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return len(lines)


_HEADER_DECL = """\
/* 自动生成：box64 musl 静态链接下缺失 glibc 符号的 extern 声明
 * 生成器: scripts/gen-libc-stubs.py（请勿手工编辑）
 *
 * 通过 wrappedlib_init.h → #include "glibc_missing_symbols.h" 引入，
 * 使 STATICBUILD 下 GO(N,W) → {#N, W, 0, &N} 宏展开时能找到符号声明。
 * 实际 weak 定义在 glibc_missing_symbols.c 中。
 *
 * 声明策略（四路过滤，最小化冲突）：
 *   1. 在 static_libc.h 中（声明/定义）→ 跳过（static_libc.h 先于此头被 include）
 *   2. 在 musl 头文件 decls 中 → 跳过（musl 已声明）
 *   3. 在 musl 头文件 macros 中 → 仅 #undef（musl 以宏形式提供）
 *   4. 完全缺失 → extern void(void)（-Wno-implicit-function-declaration 允许取地址）
 */

#ifndef _GLIBC_MISSING_SYMBOLS_H
#define _GLIBC_MISSING_SYMBOLS_H

/* glibc 专有类型别名（musl 无这些 typedef） */
typedef uid_t __uid_t;
typedef gid_t __gid_t;
typedef pid_t __pid_t;
typedef void (*__sighandler_t)(int);
#define __sigset_t sigset_t

/* box64 static_libc.h：slc 符号（__fgets_chk/capget 等）的声明来源。
 * 头文件带 include guard，64位 wrappedlibc.c 先 include 本头时不受影响；
 * wrapped32 不直接 include static_libc.h，经此注入获得 slc 声明。
 * 路径经 CMake include_directories(${BOX64_ROOT}/src) 解析。 */
#include "libtools/static_libc.h"

/* 补充头文件：与 build-box64-musl.sh 中 all_musl_headers.c 对齐，
 * 确保 decls（从 gcc -E 提取的 musl 头文件符号）对应的头文件实际被 include。
 * 若此处缺失某个头文件，则 decls 中该头文件的符号会被跳过声明，
 * 但 wrapped32 编译时找不到对应声明 → undeclared 错误。 */

/* 标准 C 头文件 */
#include <complex.h>
#include <errno.h>
#include <fenv.h>
#include <math.h>
#include <setjmp.h>
#include <signal.h>
#include <time.h>
#include <uchar.h>
#include <wchar.h>
#include <wctype.h>

/* POSIX 头文件 */
#include <aio.h>
#include <assert.h>
#include <cpio.h>
#include <ctype.h>
#include <dirent.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <fmtmsg.h>
#include <fnmatch.h>
#include <ftw.h>
#include <glob.h>
#include <grp.h>
#include <iconv.h>
#include <ifaddrs.h>
#include <langinfo.h>
#include <libgen.h>
#include <libintl.h>
#include <limits.h>
#include <locale.h>
/* <malloc.h> 不在此处 include：patch-musl-isnanf.py 注入了自定义 struct mallinfo，
 * 与 musl <malloc.h> 中的定义冲突。malloc 相关函数由 <stdlib.h> 覆盖。 */
#include <mqueue.h>
#include <nl_types.h>
#include <poll.h>
#include <pthread.h>
#include <pty.h>
#include <pwd.h>
#include <regex.h>
#include <resolv.h>
#include <sched.h>
#include <search.h>
#include <semaphore.h>
#include <spawn.h>
#include <stdalign.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <tar.h>
#include <termios.h>
#include <unistd.h>
#include <utime.h>
#include <utmp.h>
#include <utmpx.h>
#include <wordexp.h>

/* sys/* 头文件 */
#include <sys/uio.h>
#include <sys/epoll.h>
#include <sys/file.h>
#include <sys/eventfd.h>
#include <sys/fanotify.h>
#include <sys/fsuid.h>
#include <sys/inotify.h>
#include <sys/ioctl.h>
#include <sys/ipc.h>
#include <sys/klog.h>
#include <sys/mman.h>
#include <sys/mount.h>
#include <sys/msg.h>
#include <sys/personality.h>
#include <sys/prctl.h>
#include <sys/ptrace.h>
#include <sys/quota.h>
#include <sys/random.h>
#include <sys/reboot.h>
#include <sys/resource.h>
#include <sys/sendfile.h>
#include <sys/sem.h>
#include <sys/shm.h>
#include <sys/signalfd.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/statfs.h>
#include <sys/statvfs.h>
#include <sys/syscall.h>
#include <sys/sysinfo.h>
#include <sys/timeb.h>
#include <sys/timerfd.h>
#include <sys/times.h>
#include <sys/timex.h>
#include <sys/types.h>
#include <sys/uio.h>
#include <sys/un.h>
#include <sys/utsname.h>
#include <sys/wait.h>
#include <sys/xattr.h>

/* 网络头文件 */
#include <arpa/inet.h>
#include <arpa/nameser.h>
#include <netdb.h>
#include <net/ethernet.h>
#include <net/if.h>
#include <netinet/in.h>
#include <netinet/tcp.h>

/* 其他系统头文件 */
#include <mntent.h>
#include <shadow.h>

"""


# ------------------------------------------------------------- 共享头安全签名


def _is_safe_shared_sig(ret: str, params: str) -> bool:
    """判断签名是否可安全写入共享头 glibc_missing_symbols.h。

    共享头被所有 TU include，只允许出现各处可见的通用类型。
    规则（白名单+黑名单混合）：
    - 大写开头标识符（CURLSHoption/XID/EventHandler/BDF_*）一律拒绝，
      除显式白名单（FILE）
    - 已知 TU 局部前缀/后缀（FT_/SDL_/my_/i386_/*_32/*_32_t/x86emu_t）拒绝
    - struct/union/enum 标签名同样检查
    不安全 → 跳过声明（定义所在 TU 已有定义，其他 TU 不引用该符号）。
    """
    _SAFE_UPPER = {"FILE"}

    def _idents(s: str) -> list:
        return re.findall(r"[A-Za-z_]\w*", s)

    def _ok_type_token(t: str) -> bool:
        if t in ("struct", "union", "enum", "const", "volatile",
                 "unsigned", "signed", "static", "extern", "inline",
                 "restrict", "__attribute__", "__extension__"):
            return True
        if t[0].isupper():
            return t in _SAFE_UPPER
        if re.match(r"^(FT_|SDL_|BDF_|PS_|my_|i386_|x86emu|x64_va)", t):
            return False
        if re.search(r"_32(_t)?$", t) or t.endswith("_32_t"):
            return False
        if t in ("posix_spawn_file_actions_32_t", "fcvalue_32_t",
                 "EventHandler", "CURLSHoption",
                 # threads32.c 局部 typedef，共享头不可见
                 "pthread_cond_2_0_t"):
            return False
        return True

    def _check_toks(toks: list) -> bool:
        i = 0
        while i < len(toks):
            t = toks[i]
            if t in ("struct", "union", "enum"):
                if i + 1 >= len(toks):
                    return False
                if not _ok_type_token(toks[i + 1]) and toks[i + 1][0].isupper() is False:
                    # 标签名走同一套规则（大写需白名单，前缀黑名单拒绝）
                    if not _ok_type_token(toks[i + 1]):
                        return False
                elif toks[i + 1][0].isupper() and toks[i + 1] not in _SAFE_UPPER:
                    return False
                elif not _ok_type_token(toks[i + 1]):
                    return False
                i += 2
                continue
            if not _ok_type_token(t):
                return False
            i += 1
        return True

    if not _check_toks(_idents(ret)):
        return False
    p = (params or "").strip()
    if not p or p == "void":
        return True
    for part in p.split(","):
        part = re.sub(r"\[[^\]]*\]", "", part)
        toks = _idents(part)
        if not toks:
            continue
        type_toks = toks[:-1] if len(toks) > 1 else toks
        if not _check_toks(type_toks):
            return False
    return True


def find_local_export_data(box64_src):
    """扫描 box64 源中 EXPORT 定义的数据符号（如 wrappedlibpcre.c 的 pcre_free）。
    这些符号 box64 自身有 strong 定义；glibc_missing_symbols.h 若再声明
    extern unsigned char[N]，会在同一 TU（经 wrappedlib_init.h include）
    与定义类型冲突 → 生成头文件时跳过。返回 {符号名}。"""
    found = set()
    pat = re.compile(r"^EXPORT\s+(?!.*\()(.+?)\s*([A-Za-z_]\w*)\s*(?:=|;)")
    src = os.path.join(box64_src, "src")
    if not os.path.isdir(src):
        return found
    for dirpath, _dirs, files in os.walk(src):
        for fn in files:
            if not fn.endswith(".c"):
                continue
            try:
                with open(os.path.join(dirpath, fn), encoding="utf-8",
                          errors="replace") as f:
                    for line in f:
                        m = pat.match(line)
                        if m:
                            found.add(m.group(2))
            except OSError:
                pass
    return found


def generate_header(func_refs, data_refs, sigs, smart, out_path,
                    musl_header_decls=None,
                    musl_macros=None,
                    static_libc_syms=None,
                    musl_syms=None,
                    box32_sigs=None,
                    has_static_libc=True,
                    local_data_defs=None):
    """生成 extern 声明头文件。

    五路过滤策略（最小化与 musl/box64 头文件的类型冲突）:
      1. _KNOWN_MUSL_DECLS → 跳过
      2. smart 路径 → 精确签名声明
      3. static_libc_syms → 跳过（box64 static_libc.h 已声明/定义）
      4. macros → #undef 后 fallthrough
      5. 完全缺失 → extern void(name)(void)

    关键：decls 提取不完整（只有 2660 个），但 .h 文件 include 了所有 musl
    头文件。因此额外使用 _KNOWN_MUSL_DECLS 和 _MUSL_KNOWN_DATA 手动覆盖
    已知会冲突的符号。
    """
    lines = [_HEADER_DECL]
    if not has_static_libc:
        # 老版本源码无 src/libtools/static_libc.h，剔除模板中的 include 行
        lines = [("\n".join(l for l in _HEADER_DECL.splitlines()
                            if 'libtools/static_libc.h' not in l) + "\n")]
    undefs = _render_undefs(smart)
    if undefs:
        lines.append("/* 屏蔽 musl <math.h> 宏定义 */")
        lines.append(undefs)

    decls = musl_header_decls or set()
    macros = musl_macros or set()
    slc_syms = static_libc_syms or set()
    musl_all = set(musl_syms) if musl_syms else set()

    # musl 头文件中已声明的全局数据符号（即使提取失败也要跳过）
    # 提前定义：函数段也需跳过这些符号（避免与 injected extern int / DATA 声明冲突）
    _MUSL_KNOWN_DATA = {
        "daylight", "timezone", "tzname",
        "optarg", "opterr", "optind", "optopt",
        "stdin", "stdout", "stderr",
        "environ", "errno",
        "_IO_2_1_stdin_", "_IO_2_1_stdout_", "_IO_2_1_stderr_",
        "_IO_file_jumps", "_IO_list_all",
        "__progname", "__progname_full",
        "_sys_siglist", "sys_siglist",
        "_nl_msg_cat_cntr", "__check_rhosts_file",
        "signgam",
        "__res_state",
        "__libc_enable_secure",  # wrapped32/wrappedlibc.c 定义为 int，与 unsigned char[4] 冲突
        "__stack_chk_guard",     # wrappedldlinux.c 声明为 extern void*
        "__libc_stack_end",      # wrappedldlinux.c 声明为 extern void*
        "__pointer_chk_guard",   # wrappedldlinux.c 声明为 extern void*
        "_rtld_global",          # wrapped32/wrappedldlinux.c 由 patch 注入 extern int 声明
        "_rtld_global_ro",       # wrapped32/wrappedldlinux.c 由 patch 注入 extern int 声明
        "__ctype_b",             # musl <ctype.h> 宏 → (*__ctype_b_loc())
        # __timezone 不进 _MUSL_KNOWN_DATA：musl 只有 timezone，无 __timezone；
        # private.h 的 DATAB/DATAM(__timezone) 需 data 段声明
        "_r_debug",              # musl 内部，某些头文件可能声明
    }

    # 过滤 dummy_* 假符号（wrappedlibc_private.h 中的特殊条目，非真实 C 函数）
    # 过滤 my_*（64位 wrapper，定义在 wrapped/*.c；wrapped32 经 init32.h 单独注入声明）
    # 保留 my32_*：wrapped32 GOM→&my32_N 需 header 声明 + weak stub（多数无定义）
    func_refs = {n for n in func_refs if not n.startswith("dummy_") and not n.startswith("my_")}

    box32_sigs = box32_sigs or {}

    print(f"[header] 输入: func_refs={len(func_refs)}, decls={len(decls)}, "
          f"macros={len(macros)}, static_libc={len(slc_syms)}, box32_sigs={len(box32_sigs)}")

    lines.append("/* ================= 函数声明 ================= */")
    declared = 0
    undefed = 0
    skipped_slc = 0
    # musl 头文件已声明但 gcc -E 提取可能遗漏的函数（musl 内部实现/条件声明）
    _KNOWN_MUSL_DECLS = {
        # 注意: arc4random/arc4random_buf/malloc_usable_size 不在 _KNOWN——
        # musl 若无声明（或 malloc.h 未 include）则由 path4/FORCE 提供
        # __pthread_mutexattr_*: static_libc.h → static_threads.h 已声明
        "__pthread_mutexattr_destroy", "__pthread_mutexattr_settype",
        # __pthread_getspecific/setspecific/rwlock_* 不进 _KNOWN：
        # musl pthread.h/static_threads.h 实际未声明这些 __ 符号（CI 98cf1c1 undeclared）；
        # FORCE 须用与 weak stub 一致的 intptr_t(void)，void(void) 会 conflicting（35825886967）
        # 其余 C类历史条目：musl 头未对 wrapped32 可见声明，留在集合会导致 GO() 取地址 undeclared
        "__fdelt_chk", "__xpg_basename", "dn_skipname",
        "fts_close", "fts_open", "fts_set", "fts_children", "fts_read",
        "open_tree", "move_mount", "fsmount", "fsopen", "fsconfig",
        "fsmove",
        "strerrorname_np",
        "__sigaddset", "__sigismember", "__sigdelset",
        "__mbsnrtowcs_chk", "__mbsrtowcs_chk",
        "__wcrtomb_chk", "__wcsrtombs_chk",
        # readdir64/readdir64_r: 由 patch_text 替换 readdir64( → readdir(，
        # smart 路径在 header 中声明正确签名（&readdir64 在 GO 宏中需要）
        "cfree", "tfind", "tsearch", "tdestroy", "twalk",
        "prlimit64",
        "eventfd", "eventfd_read", "eventfd_write",
        "fanotify_init", "fanotify_mark",
        "klogctl", "quotactl", "reboot",
        "flock", "_flushlbf",
        # __res_close/__res_iclose/__res_ninit/__res_nclose: musl resolv.h 不公开声明，
        # 必须由本头文件提供 extern 声明，否则 wrappedlibresolv_private.h 中 GO() 取地址报 undeclared
        "__assert_fail",
        # capget/capset/__bzero/gnu_dev_*/_IO_* 在 static_libc.h，由 slc_syms 跳过；
        # wrapped32 经 glibc_missing_symbols.h include static_libc.h 获得声明
        "fmtmsg", "ftime",
        "__progname", "__progname_full",
        "openpty",
        # musl <pthread.h> / <semaphore.h> 静态内联变体
        "__pthread_mutex_init", "__pthread_mutex_lock",
        "__pthread_mutex_unlock", "__pthread_mutex_trylock",
        "__pthread_mutex_destroy", "__pthread_mutex_timedlock",
        "__pthread_cond_init", "__pthread_cond_wait",
        "__pthread_cond_timedwait", "__pthread_cond_signal",
        "__pthread_cond_broadcast", "__pthread_cond_destroy",
        "__pthread_rwlock_init",
        "__pthread_rwlock_tryrdlock", "__pthread_rwlock_trywrlock",
        "__pthread_rwlock_destroy",
        "__pthread_key_create", "__pthread_key_delete",
        "pthread_mutexattr_getkind_np", "pthread_mutexattr_setkind_np",
        "__pthread_mutexattr_init",
        "sem_close", "sem_destroy", "sem_getvalue", "sem_init",
        "sem_open", "sem_post", "sem_timedwait", "sem_trywait",
        "sem_unlink", "sem_wait",
        # musl <resolv.h> 已声明的符号（wrappedlibresolv.c 先 #include <resolv.h>）
        # 这些符号如果 header 再声明会冲突
        "__dn_comp", "__dn_expand", "__dn_skipname",
        "dn_comp", "dn_expand", "dn_skipname",
        "__ns_get16", "__ns_get32",
        "__ns_name_ntop", "__ns_name_unpack",
        "__res_dnok", "__res_hnok", "__res_mailok", "__res_ownok", "__res_send",
        "ns_get16", "ns_get32",
        "ns_initparse", "ns_name_uncompress", "ns_parserr",
        "ns_put16", "ns_put32", "ns_skiprr",
        # musl resolv.h 也声明 res_init/res_query/res_search 等
        # res_nquery/res_nsearch 已移除：musl 未声明，wrapped32 取地址需本头 extern
        "res_init", "res_query", "res_search", "res_querydomain",
        "res_mkquery", "res_send",
        "res_nquerydomain", "res_nmkquery", "res_nsend",
        "__res_init", "__res_query", "__res_search",
        "__res_querydomain", "__res_mkquery",
        "__res_nquery", "__res_nsearch",
        # static_libc.h 冲突：返回 void* vs struct __res_state*
        "__res_state",
        # musl <mqueue.h> 已声明
        "__mq_open_2", "mq_close", "mq_getattr", "mq_open",
        "mq_receive", "mq_send", "mq_setattr",
        "mq_timedreceive", "mq_timedsend", "mq_unlink",
        # musl <aio.h> 已声明
        "aio_cancel", "aio_error", "aio_fsync", "aio_read", "aio_return",
        "aio_suspend", "aio_write", "lio_listio",
        # musl <dlfcn.h> 已声明
        "dl_iterate_phdr", "dladdr", "dlclose", "dlerror", "dlinfo",
        "dlopen", "dlsym",
        # musl <ftw.h> 已声明
        "ftw", "nftw",
        # musl <mqueue.h> 已声明（补充遗漏）
        "mq_notify",
        # musl <err.h> 已声明
        "error_at_line", "errx", "verr", "verrx", "vwarn", "vwarnx",
        "warn", "warnx",
        # musl <getopt.h> 已声明（通过 <unistd.h> 或其他头间接引入）
        "getopt_long", "getopt_long_only",
        # musl 内部实现 / __ 前缀变体
        "__strtold_internal",
        # musl <syslog.h> 已声明
        "vsyslog",
        # GCC 内建函数 / musl _l 后缀宏：extern void 声明会冲突
        "strfmon", "strfmon_l",
        # roundeven/roundevenf: smart 路径提供 stub 签名
        "__strtold_l", "__strtod_l", "__strtol_l", "__strtoll_l",
        "__strtoul_l", "__strtoull_l", "__wcstol_l", "__wcstoll_l",
        "__wcstoul_l", "__wcstoull_l", "__wcstod_l", "__wcstof_l",
        "__wcstold_l",
    }
    # 强制声明：decls 是 gcc -E 全文标识符（含字符串/未用宏展开残留），非真实声明；
    # musl 公共头与 static_libc.h 均未声明 → private.h GO() 取地址必须由本头提供 extern。
    # 仅放两边都缺失的符号；__res_iclose/__res_nclose/__res_ninit 在 static_libc.h
    # 已有正确签名（void(void*,int)/void(void*)/int(void*)），须走 slc_syms 跳过，不可强制。
    # 值必须是与64位 wrappedlibc.c（syslog.h/malloc.h/本地定义）兼容的正确签名，
    # 不能一律 void(void)——会与系统头/本地 weak 定义 conflicting types。
    _FORCE_DECLARE = {
        "__res_close": "void __res_close(void)",
        "res_nquery": "void res_nquery(void)",
        # 64位 wrappedlibc.c L5238 weak 定义 uint32_t arc4random(void)；
        # musl stdlib.h 无 arc4random → wrapped32 需本头声明
        "arc4random": "uint32_t arc4random(void)",
        "arc4random_buf": "void arc4random_buf(void* buf, size_t buflen)",
        # musl malloc.h:19 size_t(void*)；本头故意不 include malloc.h
        "malloc_usable_size": "size_t malloc_usable_size(void* ptr)",
        # musl syslog.h:60-63 签名（wrappedlibc.c L60 include 它）
        "closelog": "void closelog(void)",
        "openlog": "void openlog(const char* ident, int option, int facility)",
        "setlogmask": "int setlogmask(int maskpri)",
        "syslog": "void syslog(int priority, const char* format, ...)",
        # vsyslog 不进 FORCE：wrappedlibc.c 自带 extern int
        # 5 个 __pthread_*：musl 头未声明；签名须与 src/wrapped/wrappedlibpthread.c
        # L69-78 本地 extern 完全一致，否则 conflicting types
        "__pthread_getspecific": "void* __pthread_getspecific(size_t)",
        "__pthread_setspecific": "int __pthread_setspecific(size_t, void*)",
        "__pthread_rwlock_rdlock": "int __pthread_rwlock_rdlock(void*)",
        "__pthread_rwlock_unlock": "int __pthread_rwlock_unlock(void*)",
        "__pthread_rwlock_wrlock": "int __pthread_rwlock_wrlock(void*)",
    }
    for name in sorted(func_refs):
        # 数据符号 / 已知 DATA 符号不在函数段声明（避免 redeclared as different kind）
        if name in data_refs or name in _MUSL_KNOWN_DATA:
            continue
        # 强制声明优先于 decls/static_libc 跳过（在 _KNOWN 之后、smart 之前）
        if name in _FORCE_DECLARE:
            if name in macros:
                lines.append(f"#ifdef {name}")
                lines.append(f"#undef {name}")
                lines.append("#endif")
                undefed += 1
            lines.append(f"extern {_FORCE_DECLARE[name]};")
            declared += 1
            continue
        # 前置跳过：musl 已知声明/内联/宏（优先级最高，避免与 smart 路径冲突）
        if name in _KNOWN_MUSL_DECLS:
            continue
        # 第零路（前）：static_libc.h 已声明/定义 → 跳过。
        # 本头在函数段之前 #include static_libc.h；若 smart 再声明一次，
        # 即使语义相同、限定符/参数个数略异也会 conflicting types。
        if name in slc_syms:
            skipped_slc += 1
            continue
        # 第零路：SMART_MATH 符号（isnan/isinf/finite 等）始终声明，
        # 因为 wrappedlibm.c 等文件可能不包含 <math.h>，需要这些声明。
        # 用 #undef + 正确签名（从 smart map 推导）。
        if name in smart:
            if name in macros:
                lines.append(f"#ifdef {name}")
                lines.append(f"#undef {name}")
                lines.append("#endif")
                undefed += 1
            # 从 smart map 的实现文本提取签名
            impl = smart[name]
            # 格式: "int __finite(double x) { return __builtin_isfinite(x); }"
            sig_match = re.match(r"(\S+(?:\s+\S+)?)\s+(\w+)\s*\(([^)]*)\)", impl)
            if sig_match:
                ret_type = sig_match.group(1)
                params = sig_match.group(3)
                lines.append(f"extern {ret_type} {name}({params});")
            else:
                lines.append(f"extern void {name}(void);")
            declared += 1
            continue
        # 第二路：musl 头文件已有函数/类型声明 → 跳过
        # 注意：不因 musl_all（nm 符号）跳过——内部符号可能在 libc.a 中有
        # 但公共头未声明，private.h 取地址时需本头提供 extern 声明。
        if name in decls:
            continue
        # 第三路：musl 以宏形式提供 → #undef 后 fallthrough 到声明
        if name in macros:
            lines.append(f"#ifdef {name}")
            lines.append(f"#undef {name}")
            lines.append(f"#endif")
            undefed += 1
        # 第四路：完全缺失的符号 → extern 声明
        # 仅当符号不在 decls 中时才生成（decls 来自 musl 头文件预处理提取）
        # my32_*：仅当签名只含通用类型时用真实签名（避免专有类型在共享头中
        # unknown type）；含 FT_/SDL_/X11/*_32_t 等专有类型则跳过声明——
        # 定义所在 TU 在 include 本头前已有定义，其他 TU 不引用该符号。
        if name not in decls:
            if name.startswith("my32_") and name in box32_sigs:
                ret, params, conflict = box32_sigs[name]
                # def/decl 签名冲突（如 makecontext void* vs int32_t*）：
                # 共享头用无参声明，与任意原型兼容，避免与 wrappedlibc.c 前向声明冲突
                if conflict:
                    safe_ret = ret if _is_safe_shared_sig(ret, "") else "unsigned long"
                    lines.append(f"extern {safe_ret} {name}();")
                    declared += 1
                elif _is_safe_shared_sig(ret, params):
                    if not params or params.strip() == "void":
                        lines.append(f"extern {ret} {name}(void);")
                    else:
                        lines.append(f"extern {ret} {name}({params});")
                    declared += 1
                else:
                    # 不安全签名：无参声明（仅取地址合法，C 允许）
                    # 返回类型也不安全（如 XID/X11）时降级 unsigned long（机器字宽）
                    safe_ret = ret if _is_safe_shared_sig(ret, "") else "unsigned long"
                    lines.append(f"extern {safe_ret} {name}();")
                    declared += 1
            elif name.startswith("my32_") and name not in box32_sigs:
                # 无本地定义的 my32_*（如 GOWS→my32_imaxdiv）：
                # path4 void + weak stub
                lines.append(f"extern void {name}(void);")
                declared += 1
            else:
                lines.append(f"extern void {name}(void);")
                declared += 1
    print(f"[header] 函数: undef={undefed}, 声明={declared}, "
          f"跳过(static_libc)={skipped_slc}, "
          f"跳过(decls)={len(func_refs)-undefed-declared-skipped_slc}")

    lines.append("")
    lines.append("/* ================= 数据声明 ================= */")
    # DATAM 的 my32_ 映射与 .c 本地定义类型须一致：
    #   in6addr*: libc_net32.c 定义 struct in6_addr
    #   ___libc_stack_end: wrapped32/wrappedldlinux.c 定义 ptr_t（BOX32 下 unsigned int）
    #   __r_debug: wrapped32/wrappedldlinux.c 定义 void*[5]
    # value = (类型, 可选数组维度)
    _EXPLICIT_DATA_C = {
        "my32_in6addr_any": ("struct in6_addr", ""),
        "my32_in6addr_loopback": ("struct in6_addr", ""),
        "my32___libc_stack_end": ("unsigned int", ""),
        "my32__r_debug": ("void*", "[5]"),
    }
    for name in sorted(data_refs):
        # musl 头文件已声明的数据 → 跳过
        if name in decls or name in macros:
            continue
        # 已知的 musl 声明数据符号 → 跳过
        if name in _MUSL_KNOWN_DATA:
            continue
        if name in _EXPLICIT_DATA_C:
            typ, dims = _EXPLICIT_DATA_C[name]
            lines.append(f"extern {typ} {name}{dims};")
        elif name.startswith("my32_"):
            # my32_ 数据：定义在同 TU 早期（wrappedlibc.c L1909+），
            # 声明 unsigned char[N] 会 conflicting → 一律跳过
            # （跨 TU 的 in6addr/stack_end/r_debug 走 _EXPLICIT）
            continue
        elif local_data_defs and name in local_data_defs:
            # box64 自身 EXPORT 定义的数据（如 wrappedlibpcre.c 的 pcre_free）：
            # 同 TU 内 extern unsigned char[N] 与定义类型冲突 → 跳过声明
            continue
        else:
            lines.append(f"extern unsigned char {name}[{data_refs[name][0]}];")

    lines.append("")
    lines.append("#endif /* _GLIBC_MISSING_SYMBOLS_H */")
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
    ap.add_argument("--output-h", default=None,
                    help="输出 extern 声明 .h 文件路径（默认同目录 glibc_missing_symbols.h）")
    ap.add_argument("--musl-src", default=None,
                    help="musl 源码目录（提供则跳过下载）")
    ap.add_argument("--musl-url", default=None, help="musl tarball 下载 URL（可选）")
    ap.add_argument("--musl-syms", default=None,
                    help="nm 提取的符号文件（优先；最精确）")
    ap.add_argument("--cache-dir", default=None,
                    help="musl tarball 缓存目录（默认 <box64-src 同级目录>/.cache）")
    ap.add_argument("--force-stub", action="append", default=[],
                    help="强制对某符号生成 stub（即使符号集认为 musl 存在）")
    ap.add_argument("--force-stub-data", action="append", default=[],
                    help="强制为某数据符号生成 stub，格式 NAME=SIZE（即使不在缺失列表）")
    ap.add_argument("--no-stub", action="append", default=[],
                    help="强制跳过某符号（即使缺失）")
    ap.add_argument("--no-stub-regex", action="append", default=[],
                    help="按正则（fullmatch）批量排除，兜底上游新增编译器 RT 符号，"
                         "如 '__u?(div|mod|divmod)(t[if]|d[if])[0-9]+'")
    ap.add_argument("--musl-header-syms", default=None,
                    help="musl 头文件可见符号列表文件（交叉编译器预处理提取）")
    ap.add_argument("--musl-header-decls", default=None,
                    help="musl 头文件函数/类型声明列表文件（#undef 宏后预处理提取）")
    ap.add_argument("--musl-macros", default=None,
                    help="musl 头文件宏定义名列表文件（-dM 提取）")
    ap.add_argument("--check", action="store_true",
                    help="若检测到交叉 gcc，对生成的 stub.c 做 -fsyntax-only 校验")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    priv = args.private or os.path.join(args.box64_src, "src", "wrapped",
                                        "wrappedlibc_private.h")
    slh = args.static_libc_h or os.path.join(args.box64_src, "src", "libtools",
                                             "static_libc.h")
    if not os.path.isfile(priv):
        sys.exit(f"找不到文件: {priv}")
    if not os.path.isfile(slh):
        # 老版本（如 v0.2.4）尚无 src/libtools/static_libc.h，降级运行
        print(f"[warn] static_libc.h 不存在，按空集处理: {slh}")
        slh = None

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

    # 2. box64 引用符号：扫描所有 wrapped*_private.h
    # glibc_missing_symbols.h 通过 wrappedlib_init.h 被所有 wrappedXXX.c include，
    # 因此必须覆盖所有 wrapped*_private.h 中引用的符号
    func_refs, data_refs = parse_private_refs(priv)
    wrapped_dir = os.path.join(args.box64_src, "src", "wrapped")
    # 也扫描 wrapped32 子目录（如果有）
    wrapped32_dir = os.path.join(args.box64_src, "src", "wrapped32")
    scanned = {priv}  # 避免重复扫描 wrappedlibc_private.h
    for scan_dir in [wrapped_dir, wrapped32_dir]:
        if not os.path.isdir(scan_dir):
            continue
        for fn in sorted(os.listdir(scan_dir)):
            if not fn.endswith("_private.h"):
                continue
            p = os.path.join(scan_dir, fn)
            if p in scanned:
                continue
            scanned.add(p)
            extra_func, extra_data = parse_private_refs(p)
            new_funcs = set(extra_func) - set(func_refs)
            new_data = set(extra_data) - set(data_refs)
            if new_funcs or new_data:
                print(f"[box64] {fn}: +{len(new_funcs)} 函数, +{len(new_data)} 数据")
            func_refs.update(extra_func)
            data_refs.update(extra_data)
    sigs = parse_static_libc_signatures(slh) if slh else {}
    static_libc_syms = parse_static_libc_symbols(slh) if slh else set()
    # my32_ 本地定义签名（header 声明须兼容，避免 conflicting types）
    # 扫 wrapped32 + libtools（my32_imaxdiv/div 等可能在 libtools/*32*.c）
    libtools_dir = os.path.join(args.box64_src, "src", "libtools")
    box32_sigs = parse_box32_sigs(
        wrapped32_dir if os.path.isdir(wrapped32_dir) else None,
        libtools_dir if os.path.isdir(libtools_dir) else None,
    )
    if args.verbose:
        print(f"[box64] 引用函数 {len(func_refs)}、数据 {len(data_refs)}、"
              f"static_libc.h 签名 {len(sigs)}、定义+声明 {len(static_libc_syms)}、"
              f"my32_ 签名 {len(box32_sigs)}")

    # 3. 差集
    missing_funcs = {s for s in func_refs if s not in musl_syms}
    missing_datas = {s: sz for s, (sz, _m) in data_refs.items() if s not in musl_syms}

    # FORBIDDEN_STUB：这些符号绝不能是恒 0/NULL 的 weak stub（会静默产生错误语义，
    # 比 B-10 编译器 RT 恒 0 更隐蔽）。命中且无智能实现（smart）且将要生成 stub → 硬错。
    # __memcmpeq：glibc memcmp==0 降级目标，musl 不提供，恒 0 恒“相等”（见 SMART_EXTRA 正实现）
    FORBIDDEN_STUB = {
        "__memcmpeq", "__stack_chk_fail", "__tls_get_addr",
        "__cxa_atexit", "__cxa_finalize", "__cxa_pure_virtual",
        "__errno_location",
    }

    for s in args.no_stub:
        missing_funcs.discard(s)
        missing_datas.pop(s, None)
    if args.no_stub_regex:
        import re as _re
        _pats = [_re.compile(p) for p in args.no_stub_regex]
        _hit = sorted(s for s in missing_funcs
                      if any(p.fullmatch(s) for p in _pats))
        for s in _hit:
            missing_funcs.discard(s)
        if _hit:
            print(f"[no-stub-regex] 排除 {len(_hit)} 个: {' '.join(_hit)}")
    for s in args.force_stub:
        missing_funcs.add(s)
    for item in args.force_stub_data:
        nm, _, sz = item.partition("=")
        missing_datas[nm.strip()] = int(sz) if sz.strip() else 64

    smart = build_smart_map(missing_funcs)

    # FORBIDDEN_STUB 检查：进了 missing、无智能实现、即将被生成为 stub → 失败
    forbidden_hit = sorted(FORBIDDEN_STUB & (missing_funcs - set(smart)))
    if forbidden_hit:
        print("[FATAL] 下列高危符号将被生成恒 0/NULL stub（会静默产生错误语义），"
              "请加入 SMART_EXTRA 正实现或 --no-stub 排除:",
              " ".join(forbidden_hit), file=sys.stderr)
        sys.exit(1)

    # 3.5 加载 musl 头文件声明集（供 generate_stubs 和 generate_header 使用）
    musl_header_syms = set()
    if args.musl_header_syms and os.path.isfile(args.musl_header_syms):
        with open(args.musl_header_syms, encoding="utf-8") as f:
            musl_header_syms = {line.strip() for line in f if line.strip()}
        print(f"[musl] 头文件可见符号: {len(musl_header_syms)}")

    musl_header_decls = set()
    if args.musl_header_decls and os.path.isfile(args.musl_header_decls):
        with open(args.musl_header_decls, encoding="utf-8") as f:
            musl_header_decls = {line.strip() for line in f if line.strip()}
        print(f"[musl] 头文件函数/类型声明: {len(musl_header_decls)}")

    musl_macros = set()
    if args.musl_macros and os.path.isfile(args.musl_macros):
        with open(args.musl_macros, encoding="utf-8") as f:
            musl_macros = {line.strip() for line in f if line.strip()}
        print(f"[musl] 头文件宏: {len(musl_macros)}")

    # 4. 生成
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    nlines = generate_stubs(missing_funcs, missing_datas, sigs, smart, args.output,
                            musl_header_decls=musl_header_decls,
                            force_names=set(args.force_stub))

    h_path = args.output_h or os.path.join(
        os.path.dirname(args.output), "glibc_missing_symbols.h")

    local_data = find_local_export_data(args.box64_src)
    print(f"[box64] EXPORT 数据定义 {len(local_data)} 个（声明跳过交集）")

    h_lines = generate_header(func_refs, data_refs, sigs, smart, h_path,
                              musl_header_decls=musl_header_decls,
                              musl_macros=musl_macros,
                              static_libc_syms=static_libc_syms,
                              musl_syms=musl_syms,
                              box32_sigs=box32_sigs,
                              has_static_libc=slh is not None,
                              local_data_defs=local_data)

    # 注入 GO2 目标 my_* 声明到 wrappedlib_init32.h（须在 patch 注入 glibc_missing 之后）
    inject_my64_decls_into_init32(args.box64_src)

    print(f"[生成] 输出 {args.output}（{nlines} 行）")
    print(f"[生成] 输出 {h_path}（{h_lines} 行）")
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