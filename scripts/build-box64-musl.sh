#!/bin/bash
# 交叉编译 box64：aarch64 musl 静态单文件（BOX32 + DYNAREC + STATICBUILD + BAD_SIGNAL）
# 仅在 GitHub Actions 上运行（本地设备性能红线，禁止本地编译）
set -euxo pipefail

MUSL_VERSION=20260515
MUSL_ARCH=aarch64-unknown-linux-musl
WORK=/tmp/box64-build
TOOLCHAIN=/opt/$MUSL_ARCH

echo "==> 下载 musl 交叉工具链"
mkdir -p $WORK
cd $WORK
curl -fsSL -o musl.tar.xz \
  "https://github.com/cross-tools/musl-cross/releases/download/$MUSL_VERSION/$MUSL_ARCH.tar.xz"
tar xf musl.tar.xz -C /opt

CROSS_CC=$TOOLCHAIN/bin/$MUSL_ARCH-gcc
$CROSS_CC --version

echo "==> 提取 musl 符号列表（用于精确生成缺失符号 stub）"
MUSL_SYMS=/tmp/musl-syms.txt
MUSL_LIBC_A=$(find $TOOLCHAIN -name 'libc.a' -type f 2>/dev/null | head -1)
if [ -n "$MUSL_LIBC_A" ]; then
  echo "找到 libc.a: $MUSL_LIBC_A"
  $TOOLCHAIN/bin/$MUSL_ARCH-nm -g --defined-only "$MUSL_LIBC_A" \
    | awk '/ [A-Z] /{print $3}' | sort -u > $MUSL_SYMS
  N_SYMS=$(wc -l < $MUSL_SYMS)
  echo "musl 符号数: $N_SYMS"
  if [ "$N_SYMS" -gt 0 ]; then
    export MUSL_SYMS_FILE=$MUSL_SYMS
  else
    echo "警告: musl 符号文件为空，不设置 MUSL_SYMS_FILE"
  fi
else
  echo "警告: 未找到 libc.a，不设置 MUSL_SYMS_FILE（将由 gen-libc-stubs.py 下载 musl 源码）"
fi

echo "==> 下载 box64 源码"
cd $WORK
rm -rf box64
git clone --depth 1 https://github.com/ptitSeb/box64.git

echo "==> 提取 wrappedlibc_private.h 引用符号（供 header 提取时精确定位需 undef 的宏）"
python3 -c "
import re, sys
priv = '$WORK/box64/src/wrapped/wrappedlibc_private.h'
try:
    with open(priv) as f:
        lines = f.readlines()
except FileNotFoundError:
    print(f'警告: {priv} 不存在', file=sys.stderr)
    sys.exit(0)
func_refs = set()
for line in lines:
    # GO(name, ...), GOM(name, ...), GOW(name, ...) 等宏
    for m in re.finditer(r'GO[NMSPW]*\(\s*(\w+)', line):
        func_refs.add(m.group(1))
    # DATA(name, ...)
    for m in re.finditer(r'DATA\(\s*(\w+)', line):
        func_refs.add(m.group(1))
with open('/tmp/func_refs.txt', 'w') as f:
    for s in sorted(func_refs):
        f.write(s + '\n')
print(f'func_refs 符号数: {len(func_refs)}')
"

echo "==> 提取 musl 头文件可见符号（用于精确区分 header 声明 vs stub 定义）"
# 手工列表：覆盖所有 POSIX/系统头文件，确保 wrappedlibc_private.h 引用的符号能被正确识别
# 已知 find 自动发现方案会导致 gcc -E 预处理失败（某些内部头冲突），故用手工列表
cat > /tmp/all_musl_headers.c << 'CEOF'
#define _GNU_SOURCE
#define _DEFAULT_SOURCE
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <wchar.h>
#include <wctype.h>
#include <ctype.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#include <dirent.h>
#include <signal.h>
#include <time.h>
#include <locale.h>
#include <regex.h>
#include <poll.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <limits.h>
#include <errno.h>
#include <sys/statfs.h>
#include <sys/statvfs.h>
#include <sys/sendfile.h>
#include <sys/syscall.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <sys/uio.h>
#include <termios.h>
#include <pthread.h>
#include <setjmp.h>
#include <sched.h>
#include <grp.h>
#include <pwd.h>
#include <netdb.h>
#include <syslog.h>
#include <libgen.h>
#include <spawn.h>
#include <fenv.h>
#include <complex.h>
#include <math.h>
#include <iconv.h>
#include <nl_types.h>
#include <glob.h>
#include <fnmatch.h>
#include <wordexp.h>
#include <search.h>
#include <uchar.h>
#include <utmpx.h>
#include <utmp.h>
#include <sys/epoll.h>
#include <sys/inotify.h>
#include <sys/signalfd.h>
#include <sys/timerfd.h>
#include <sys/mount.h>
#include <sys/shm.h>
#include <sys/sem.h>
#include <sys/msg.h>
#include <sys/random.h>
#include <sys/ioctl.h>
#include <sys/personality.h>
#include <sys/sysinfo.h>
#include <sys/fsuid.h>
#include <sys/timex.h>
#include <sys/un.h>
#include <sys/prctl.h>
#include <sys/ptrace.h>
#include <sys/xattr.h>
#include <sys/utsname.h>
#include <sys/ipc.h>
#include <arpa/inet.h>
#include <netinet/tcp.h>
#include <net/ethernet.h>
#include <net/if.h>
#include <mntent.h>
#include <ifaddrs.h>
#include <langinfo.h>
#include <sys/times.h>
#include <utime.h>
#include <fts.h>
CEOF
echo "头文件数: $(grep -c '#include' /tmp/all_musl_headers.c)"

MUSL_HEADER_SYMS=/tmp/musl-header-syms.txt
MUSL_HEADER_MACROS=/tmp/musl-header-macros.txt
MUSL_HEADER_DECLS=/tmp/musl-header-decls.txt

# 用 Python 预处理 musl 头文件并提取符号（比 bash 管道更可靠）
python3 -c "
import subprocess, re, sys, os

cc = '$CROSS_CC'
flags = ['-D_GNU_SOURCE', '-D_DEFAULT_SOURCE']
test_file = '/tmp/all_musl_headers.c'

# 第一遍：提取宏定义
r2 = subprocess.run([cc, '-E', '-dM'] + flags + [test_file],
                    capture_output=True, text=True)
macros = set()
for line in r2.stdout.splitlines():
    m = re.match(r'^#define\s+(\w+)', line)
    if m:
        macros.add(m.group(1))
if len(macros) == 0:
    print(f'gcc -E -dM 退出码: {r2.returncode}', file=sys.stderr)
    print(f'stdout 长度: {len(r2.stdout)}', file=sys.stderr)
    print(f'stdout 前 500 字符: {r2.stdout[:500]}', file=sys.stderr)
    print(f'stderr 前 500 字符: {r2.stderr[:500]}', file=sys.stderr)
print(f'宏定义: {len(macros)}')

# 第二遍：提取函数/类型声明（排除被宏隐藏的函数声明如 iswdigit）
# 只 undef 与 func_refs 重名的宏（不 undef 编译器/特性宏如 _GNU_SOURCE）
# 否则重包含头文件时 GNU 特有声明（__sigaddset/arc4random 等）会消失
func_refs_file = '/tmp/func_refs.txt'
func_refs = set()
if os.path.isfile(func_refs_file):
    with open(func_refs_file) as f:
        func_refs = {line.strip() for line in f if line.strip()}
# 只 undef 在 func_refs 中出现的宏（这些宏可能隐藏了函数声明）
target_undefs = [m for m in sorted(macros) if m in func_refs]
undefs = ''.join(f'#undef {m}\\n' for m in target_undefs)
with open('/tmp/all_musl_headers_nounDEF.c', 'w') as f:
    f.write(undefs)
    f.write(open(test_file).read())
print(f'需 undef 的宏: {len(target_undefs)}/{len(macros)}')

r_clean = subprocess.run(
    [cc, '-E'] + flags + ['/tmp/all_musl_headers_nounDEF.c'],
    capture_output=True, text=True)
if r_clean.returncode != 0:
    print(f'警告: gcc -E (clean) 失败（退出码 {r_clean.returncode}）', file=sys.stderr)
    decls = set()
else:
    decls = set(re.findall(r'[A-Za-z_][A-Za-z0-9_]*', r_clean.stdout))
print(f'预处理提取标识符(undef后): {len(decls)}')

# 写入文件
with open('$MUSL_HEADER_MACROS', 'w') as f:
    for s in sorted(macros):
        f.write(s + '\n')

# decls = 有真实函数/类型/变量声明的符号（不含纯宏）
with open('$MUSL_HEADER_DECLS', 'w') as f:
    for s in sorted(decls):
        f.write(s + '\n')

all_syms = decls | macros
with open('$MUSL_HEADER_SYMS', 'w') as f:
    for s in sorted(all_syms):
        f.write(s + '\n')

print(f'musl 头文件可见符号: {len(all_syms)}（声明: {len(decls)}，宏: {len(macros)}）')
"

N_HDR=$(wc -l < $MUSL_HEADER_SYMS 2>/dev/null || echo 0)
N_MAC=$(wc -l < $MUSL_HEADER_MACROS 2>/dev/null || echo 0)
N_DCL=$(wc -l < $MUSL_HEADER_DECLS 2>/dev/null || echo 0)
echo "musl 头文件: 声明 $N_DCL + 宏 $N_MAC = 总 $N_HDR"

# 补充 mmap64.h（我们的注入头）声明的符号，避免 header 重复声明冲突
for sym in __ctype_b_loc __ctype_tolower_loc __ctype_toupper_loc __compar_d_fn_t mmap64; do
  grep -qxF "$sym" $MUSL_HEADER_SYMS || echo "$sym" >> $MUSL_HEADER_SYMS
  grep -qxF "$sym" $MUSL_HEADER_DECLS || echo "$sym" >> $MUSL_HEADER_DECLS
done

export MUSL_HEADER_SYMS_FILE=$MUSL_HEADER_SYMS
export MUSL_HEADER_MACROS_FILE=$MUSL_HEADER_MACROS
export MUSL_HEADER_DECLS_FILE=$MUSL_HEADER_DECLS

echo "==> 打 musl 补丁（isnanf -> isnan / fts 注入 / stub 头）"
mkdir -p $WORK/include
python3 $GITHUB_WORKSPACE/scripts/patch-musl-isnanf.py $WORK/box64 $WORK/include

echo "==> 复制缺失符号声明头到 include 目录"
cp $WORK/box64/src/libtools/glibc_missing_symbols.h $WORK/include/ || true

echo "==> 编译测试检测 header 冲突符号并自动修复"
# 生成测试文件：包含所有 musl 头文件 + glibc_missing_symbols.h
cat > /tmp/check_header.c << 'CHEOF'
#define _GNU_SOURCE
#define _DEFAULT_SOURCE
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <wchar.h>
#include <wctype.h>
#include <ctype.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#include <dirent.h>
#include <signal.h>
#include <time.h>
#include <locale.h>
#include <regex.h>
#include <poll.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <limits.h>
#include <errno.h>
#include <sys/statfs.h>
#include <sys/statvfs.h>
#include <sys/sendfile.h>
#include <sys/syscall.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <sys/uio.h>
#include <termios.h>
#include <pthread.h>
#include <setjmp.h>
#include <sched.h>
#include <grp.h>
#include <pwd.h>
#include <netdb.h>
#include <syslog.h>
#include <libgen.h>
#include <spawn.h>
#include <fenv.h>
#include <complex.h>
#include <math.h>
#include <iconv.h>
#include <nl_types.h>
#include <glob.h>
#include <fnmatch.h>
#include <wordexp.h>
#include <search.h>
#include <uchar.h>
#include <utmpx.h>
#include <utmp.h>
#include <sys/epoll.h>
#include <sys/inotify.h>
#include <sys/signalfd.h>
#include <sys/timerfd.h>
#include <sys/mount.h>
#include <sys/shm.h>
#include <sys/sem.h>
#include <sys/msg.h>
#include <sys/random.h>
#include <sys/ioctl.h>
#include <sys/personality.h>
#include <sys/sysinfo.h>
#include <sys/fsuid.h>
#include <sys/timex.h>
#include <sys/un.h>
#include <sys/prctl.h>
#include <sys/ptrace.h>
#include <sys/xattr.h>
#include <sys/utsname.h>
#include <sys/ipc.h>
#include <arpa/inet.h>
#include <netinet/tcp.h>
#include <net/ethernet.h>
#include <net/if.h>
#include <mntent.h>
#include <ifaddrs.h>
#include <langinfo.h>
#include <sys/times.h>
#include <utime.h>
#include <fts.h>
#include "glibc_missing_symbols.h"
CHEOF
# 用交叉编译器检测冲突（-fsyntax-only 只做语法检查不生成目标文件）
$CROSS_CC -fsyntax-only -D_GNU_SOURCE -D_DEFAULT_SOURCE \
  -I$WORK/include -include $WORK/include/mmap64.h \
  /tmp/check_header.c 2>/tmp/header_errors.txt || true
# 提取 conflicting types 的符号名
grep "conflicting types for" /tmp/header_errors.txt \
  | sed "s/.*conflicting types for '\([^']*\)'.*/\1/" | sort -u > /tmp/conflicts.txt
# 也提取 undeclared 符号名（需要确保它们被声明）
grep "undeclared here" /tmp/header_errors.txt \
  | sed "s/.*error: '\([^']*\)' undeclared.*/\1/" | sort -u > /tmp/undeclared.txt
N_CONFLICTS=$(wc -l < /tmp/conflicts.txt)
N_UNDECLARED=$(wc -l < /tmp/undeclared.txt)
echo "冲突符号: $N_CONFLICTS，未声明符号: $N_UNDECLARED"
if [ "$N_CONFLICTS" -gt 0 ]; then
  echo "冲突符号: $(cat /tmp/conflicts.txt | tr '\n' ' ')"
  python3 -c "
import re
conflicts = set()
with open('/tmp/conflicts.txt') as f:
    for line in f:
        s = line.strip()
        if s: conflicts.add(s)
header_path = '$WORK/include/glibc_missing_symbols.h'
with open(header_path) as f:
    lines = f.readlines()
new_lines = []
for line in lines:
    skip = False
    for sym in conflicts:
        if re.match(rf'extern\s+\S+\s+{re.escape(sym)}\s*\(', line):
            skip = True
            break
    if not skip:
        new_lines.append(line)
with open(header_path, 'w') as f:
    f.writelines(new_lines)
print(f'已移除 {len(conflicts)} 个冲突声明')
"
fi
# 第二轮验证：重新编译确认无冲突
$CROSS_CC -fsyntax-only -D_GNU_SOURCE -D_DEFAULT_SOURCE \
  -I$WORK/include -include $WORK/include/mmap64.h \
  /tmp/check_header.c 2>/tmp/header_errors2.txt || true
REMAINING=$(grep -c "conflicting types for" /tmp/header_errors2.txt || true)
echo "第二轮验证: 剩余冲突 $REMAINING"

echo "==> 提供 execinfo.h stub（musl 无此头，但 libc 含 backtrace 实现）"
mkdir -p $WORK/include
cat > $WORK/include/execinfo.h <<'EOF'
#ifndef _EXECINFO_H
#define _EXECINFO_H
#include <stddef.h>
#ifdef __cplusplus
extern "C" {
#endif
int backtrace(void**, int);
char** backtrace_symbols(void* const*, int);
void backtrace_symbols_fd(void* const*, int, int);
#ifdef __cplusplus
}
#endif
#endif
EOF

echo "==> cmake 交叉编译"
cd box64
mkdir build && cd build
# 强制 CI 模式：跳过 rebuild_wrappers_32.py 重新生成 wrapper32.c/h（官方预生成版已验证完整，
# 含 LFp_32/vFX_32/vFppi_32 等签名；CI 环境下重新生成会因 musl 头环境缺失这些签名）
# 注意：CMakeLists 用 if(NOT CI) 检查的是 CMake 变量，必须用 -DCI=1 传入（环境变量 export 无效）
export CI=true
# musl 无 PTHREAD_ERRORCHECK/RECURSIVE_MUTEX_INITIALIZER 静态宏，按 musl mutex 结构体布局注入：
# pthread_mutex_t = { union { int __i[10]; } __u; }，_m_type=__u.__i[0]（0=NORMAL 1=RECURSIVE 2=ERRORCHECK）
MUTEX_MACROS='-DPTHREAD_ERRORCHECK_MUTEX_INITIALIZER={{{2}}} -DPTHREAD_RECURSIVE_MUTEX_INITIALIZER={{{1}}}'
cmake .. \
  -DCI=1 \
  -DCMAKE_C_COMPILER=$CROSS_CC \
  -DCMAKE_C_FLAGS="-D_GNU_SOURCE -D_DEFAULT_SOURCE -I$WORK/include -include $WORK/include/mmap64.h $MUTEX_MACROS" \
  -DARM_DYNAREC=ON \
  -DBOX32=ON \
  -DSTATICBUILD=ON \
  -DBAD_SIGNAL=ON \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo
make -j$(nproc)

echo "==> strip 静态产物"
file ./box64
cp ./box64 /tmp/box64-aarch64-musl
$TOOLCHAIN/bin/$MUSL_ARCH-strip /tmp/box64-aarch64-musl || true
file /tmp/box64-aarch64-musl
ls -lh /tmp/box64-aarch64-musl
