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

echo "==> cmake 交叉编译"
cd box64
mkdir build && cd build
cmake .. \
  -DCMAKE_C_COMPILER=$CROSS_CC \
  -DCMAKE_C_FLAGS="-D_DEFAULT_SOURCE -std=gnu11" \
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
