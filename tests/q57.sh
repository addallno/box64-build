#!/bin/bash
# box32 q57 回归: proot 内 32 位 steamcmd 单轮登录
# 用法（Termux 侧直接执行，无需先进 proot）:
#   bash q57.sh [box64路径]   默认 /media/termux/home/box64-bin
# 判据: rc=0 且尾部 "Waiting for user info...OK" / "Unloading Steam API...OK"
# 注意: timeout 需 >=240s（Steam 网络重试会耗掉 65s+，65s 必超时）
BIN=${1:-/media/termux/home/box64-bin}
R=/data/data/com.termux/files/home/.local/share/tmoe-linux/containers/proot/ubuntu-jammy_arm64
P="proot --root-id --pwd=/root --rootfs=$R --mount=/system --kill-on-exit --mount=/storage:/storage --mount=/data/data/com.termux/files:/media/termux --sysvipc -L --link2symlink --mount=/proc:/proc --mount=/dev:/dev --mount=$R/tmp:/dev/shm --mount=/dev/urandom:/dev/random --mount=/sdcard:/media/sd"

$P /usr/bin/env -i HOME=/root USER=root PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin TERM=xterm /bin/bash -c \
  "cd /root && BOX64_LOG=1 timeout 240 $BIN /root/steam/linux32/steamcmd +login addallno +quit > /root/q57_r1.log 2>&1; echo rc=\$?"
$P /usr/bin/env -i HOME=/root USER=root PATH=/usr/sbin:/usr/bin:/sbin:/bin /bin/bash -c \
  "grep -c 'user info' /root/q57_r1.log | sed 's/^/ui=/'; grep -c 'OK' /root/q57_r1.log | sed 's/^/ok=/'; echo '== tail =='; tail -8 /root/q57_r1.log"
