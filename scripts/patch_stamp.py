#!/usr/bin/env python3
"""批次3 #15 版本 stamp：把构建期已知的 ref/上游 sha/static/patch 链 commit
注入 git_head.h（CMake 生成命令扩展），banner（BOX64_BUILD_INFO_STRING）拼接打印。

依赖：build-box64-musl.sh 在 cmake 前 export BOX64_BUILD_STAMP=...
     （形如 " main@<sha> static=true box32=on patches=<commit>"，前导空格随 GITREV 打出）。
env 未设置时 shell 展开为空串，build_info.h 侧 #ifndef 兜底 ""。

锚点：
  1) CMakeLists.txt git_head.h 生成 COMMAND（DYNAREC if/else 两处相同文本，count==2）
  2) build_info.h banner 行 `" " GITREV`（count==1）
  3) build_info.h 守卫内缺省宏（count==1）

用法: patch_stamp.py <box64源码目录>   幂等: BOX64-BUILD: stamp
"""
import sys
import os

SENTINEL = "BOX64-BUILD: stamp"

CM_SRC = (
    'COMMAND sh -c "echo \\\\\\#define GITREV \\\\\\"$(git rev-parse --short HEAD)\\\\\\">'
    '\\"${BOX64_ROOT}/src/git_head.h\\""'
)
CM_DST = (
    'COMMAND sh -c "echo \\\\\\#define GITREV \\\\\\"$(git rev-parse --short HEAD)\\\\\\">'
    '\\"${BOX64_ROOT}/src/git_head.h\\" && '
    'echo \\\\\\#define BOX64_BUILD_STAMP \\\\\\"$BOX64_BUILD_STAMP\\\\\\"'
    '>>\\"${BOX64_ROOT}/src/git_head.h\\""  # ' + SENTINEL
)

BANNER_SRC = (
    '        " v" BOX64_BUILD_INFO_STR(BOX64_MAJOR) "." BOX64_BUILD_INFO_STR(BOX64_MINOR)'
    ' "." BOX64_BUILD_INFO_STR(BOX64_REVISION) " " GITREV\n'
)
BANNER_DST = (
    '        " v" BOX64_BUILD_INFO_STR(BOX64_MAJOR) "." BOX64_BUILD_INFO_STR(BOX64_MINOR)'
    ' "." BOX64_BUILD_INFO_STR(BOX64_REVISION) " " GITREV BOX64_BUILD_STAMP'
    '  // ' + SENTINEL + '\n'
)

DEFAULT_SRC = "#ifndef __BUILD_INFO_H__\n"
DEFAULT_DST = (
    "#ifndef __BUILD_INFO_H__\n"
    "#ifndef BOX64_BUILD_STAMP\n"
    '#define BOX64_BUILD_STAMP ""  // ' + SENTINEL + " env 未注入时缺省\n"
    "#endif\n"
)


def fail(msg: str) -> int:
    print(f"patch_stamp: {msg}", file=sys.stderr)
    return 1


def apply(src: str, old: str, new: str, want: int, name: str):
    c = src.count(old)
    if c != want:
        return None, fail(f"锚点 [{name}] 命中 {c} 次（需为 {want}）: {old[:60]!r}")
    return src.replace(old, new), 0


def patch(srcdir: str) -> int:
    p_cmake = os.path.join(srcdir, "CMakeLists.txt")
    p_info = os.path.join(srcdir, "src", "build_info.h")

    with open(p_cmake, "r", encoding="utf-8") as f:
        cmake = f.read()
    with open(p_info, "r", encoding="utf-8") as f:
        info = f.read()

    if SENTINEL in cmake and SENTINEL in info:
        print("patch_stamp: 已应用过，跳过")
        return 0

    cmake, rc = apply(cmake, CM_SRC, CM_DST, 2, "CMake git_head COMMAND")
    if rc != 0:
        return rc
    info, rc = apply(info, BANNER_SRC, BANNER_DST, 1, "build_info banner")
    if rc != 0:
        return rc
    info, rc = apply(info, DEFAULT_SRC, DEFAULT_DST, 1, "build_info 守卫")
    if rc != 0:
        return rc

    with open(p_cmake, "w", encoding="utf-8") as f:
        f.write(cmake)
    with open(p_info, "w", encoding="utf-8") as f:
        f.write(info)
    print(f"patch_stamp: 已应用 -> {p_cmake}(2处), {p_info}(2处)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"用法: {sys.argv[0]} <box64源码目录>")
        sys.exit(2)
    sys.exit(patch(sys.argv[1]))
