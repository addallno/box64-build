#!/bin/bash
# box32 测试: 32位 steamcmd (steam/linux32/steamcmd) 单轮
H=/media/termux/home
B=$H/box64-patched
cd /root || exit 9
BOX64_LOG=1 timeout 65 "$B" /root/steam/linux32/steamcmd +login addallno +quit > /root/q57_r1.log 2>&1
rc=$?
echo "rc=$rc size=$(stat -c %s /root/q57_r1.log 2>/dev/null)"
grep -c "user info" /root/q57_r1.log | sed 's/^/ui=/'
grep -c "OK" /root/q57_r1.log | sed 's/^/ok=/'
echo "== tail =="
tail -12 /root/q57_r1.log
