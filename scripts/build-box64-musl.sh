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

echo "==> 下载 box64 源码"
cd $WORK
rm -rf box64
git clone --depth 1 https://github.com/ptitSeb/box64.git

echo "==> 打 musl 补丁（isnanf -> isnan / fts 注入 / stub 头）"
mkdir -p $WORK/include
python3 $GITHUB_WORKSPACE/scripts/patch-musl-isnanf.py $WORK/box64 $WORK/include

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
export CI=true
# musl 无 PTHREAD_ERRORCHECK/RECURSIVE_MUTEX_INITIALIZER 静态宏，按 musl mutex 结构体布局注入：
# pthread_mutex_t = { union { int __i[10]; } __u; }，_m_type=__u.__i[0]（0=NORMAL 1=RECURSIVE 2=ERRORCHECK）
MUTEX_MACROS='-DPTHREAD_ERRORCHECK_MUTEX_INITIALIZER={{{2}}} -DPTHREAD_RECURSIVE_MUTEX_INITIALIZER={{{1}}}'
cmake .. \
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
