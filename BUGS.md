# box64 测试发现的 Bug 清单

> 起始：v0.2.4 静态构建（A 组）→ main v0.4.5 静态 steamcmd 真机测试
> 环境：远端 Android/Termux proot ubuntu-jammy_arm64，主产物 `~/box64-aarch64-musl`（main v0.4.5 静态）
> 更新：2026-09-26

## 一、v0.2.4 静态构建期 bug（已修复，ci-param 分支）

| # | Bug | commit |
|---|-----|--------|
| 1 | threads cancel 编译失败 | 0491440 |
| 2 | debug.h 缺失 | af31b04 |
| 3 | CMake 未定义 STATICBUILD | bf1b1fb |
| 4 | gen-libc-stubs 缺 pcre_free | 9ba7bb1 |
| 5 | 链接 undefined 符号 | bfda060 |
| 6 | whole-archive 副作用 | a37492d |
| 7 | REMOVE_ITEM 清理 | 0bb7b7d |
| 8 | wrapped 引用适配 | 302cb50 |
| 9 | 单行包裹 patch | b9f0527 |
| 10 | patch_wrapped_globalrefs print bug | f0c7c64 |
| 11 | **tss_get 早崩**（thread_key_guard 缺失，88 处调用点） | 28665e6 |

### B-00 架构性缺陷（未修，用户决定搁置）
musl 静态运行时 `dlopen(NULL)/dlsym(任意 handle)` 全部返回 0（`Dynamic loading not supported`，`-Wl,--export-dynamic` 无效；CI 同款 cross-tools/musl-cross 20260515 本地编 dlstest1/2 实测）→ v0.2.4 的 121 个 `dlsym` 调用点 + wrapped 符号机制整体失效，libpthread/libutil/librt 初始化卡死。上游 CMake 本就注释 "static build (Warning, not working)"；main v0.4.5 用编译期 `&N` 绑定（171 处 STATICBUILD vs v024 原生约 10 处）。结论：v0.2.4 静态产物不用于运行测试，运行测试用 main 静态产物。

## 二、main v0.4.5 静态 steamcmd 运行时 bug

### B-01 `Loading Steam API...` 后段错误 SIGSEGV（shell 相关，未定位）⚠️ 用户已定位触发条件
- **触发条件（用户实测 2026-09-26）：zsh/bash 下必现 SIGSEGV；fish/sh 下 box64 正常加载运行。**
- 用户跑法：`fish --debug="complete" -c 'bash --debug -c "{Command}" 2>&1' 2>&1 | tee {log}`，Command=`BOX64_LOG=1 ~/box64-aarch64-musl steam/linux64/steamcmd +login addallno +quit 2>&1`；bash 内层报 `8733 段错误`，box64 日志止于 `Loading Steam API...`（tee 管道全缓冲可能丢尾部，真实崩溃点更晚）。
- 我方所有复现（run-in-proot 内 bash -c、直跑、重定向、ssh -tt pty、fish--debug+bash--debug+tee 嵌套）均**不复现**——需按 shell 环境差异排查（候选：bash/zsh 特有环境变量、job control/进程组行为、`$_`/`BASH_ENV` 等继承差异）。
- 下一步：对比 bash vs fish 下 `env` 差集，逐变量二分复现。

### B-02 CM WebSocket 全灭 → `ERROR (No Connection)`（已解决 ✅）
- 登录 addallno → Retrying ×4 → ERROR (No Connection)；connection_log 中所有 `PingWebSocketCM() (cmp*/ext*.steamserver.net:27018-27036/443)` starting 后**同秒 failed**（`timeout/neterror - Invalid`）→ `ConnectFailed('Connection Failed':0)`。
- 已排除：proot 内 curl 443/DNS OK；原生 curl TLS 到 cmp1-hkg1:27019、:443 均 TLSv1.3 verify ok；box64 下 x86_64 socktest getaddrinfo+TCP connect 全 OK；`OPENSSL_ia32cap` 禁 AES-NI/AVX 无效。
- 嫌疑：box64 下 OpenSSL TLS 握手 / WebSocket 层（吻合历史 stderr `ssl_generate_pkey_group`、`BN_mod_inverse` 线索）。
- **已解决 ✅（2026-09-26）**：实际根因即 B-10（`__udivti3` weak stub → BN mod 系全错 → EC 证书解析失败 → TLS 握手失败）。B-10 修复后 tlsx86 `TLS_OK ver=TLSv1.3 cipher=TLS_AES_256_GCM_SHA384`；`BOX64_DYNAREC=0` 解释器 steamcmd 完整登录（`Waiting for user info... OK`，connection_log `ConnectionCompleted → RecvMsgClientLogOnResponse 'OK' → [Logged On] → LogOff`）。

### B-03 x86_64 OpenSSL 程序在 box64 下 SIGILL（signal 4）（已不复现 ✅，B-10 衍生）
- 自编静态 x86_64 TLS 测试 `~/tlsx86`（glibc 静态 + libssl.a 3.0.13）box64 下启动早期 SIGILL，`connecting` 未输出，BOX64_LOG=1 无日志落盘。
- 与 B-02 强相关嫌疑：OpenSSL 初始化（cpuid/AVX/AES-NI）撞未实现指令。待 debug 版 box64 抓指令地址。
- **已不复现 ✅（2026-09-26）**：B-10 修复版复测 tlsx86 输出 `TLS_OK ver=TLSv1.3 cipher=TLS_AES_256_GCM_SHA384`（403 为 HTTP 层预期，非 TLS 失败）；SIGILL 未再现。判为 B-10 的衍生症状。

### B-04 `Async I/O on closed handle` 断言 → SIGTRAP（EXIT=133）
- stderr.txt：`src/common/completionportmanager_posix.cpp (359): Assertion Failed: Async I/O on closed handle 33` → Signal 5 SIG_DFL → dying。sm1（60s 收尾阶段）出现，sm2/bash1 未见。

### B-05 `libSDL3.so.0` 加载失败（低危）
- stderr.txt：`[BOX64] Error loading needed lib libSDL3.so.0` + `Cannot dlopen(...)`。proot x86_64 库缺 SDL3；steamcmd CLI 后续仍能跑，暂判可忽略，但可能与 B-04 相关（UI 模块缺失）。

### B-06 线程退出刷屏噪声（低危）
- box 输出尾部大量 `Work thread 'CJobMgr::m_WorkThreadPool:N' is marked exited, but we could not immediately join prior to deleting`（数百行）；stderr.txt 另有 `Thread "CJobMgr::m_WorkThreadPool:0" failed to shut down` ×4。疑 box64 pthread join/退出时序。

### B-07 IPC 调用极慢（性能）
- `IPC function call IClientUtils/IClientUser::* took too long: 43-1387 msec`（几十处）。box64 功能正常但性能差；启动到登录耗时明显。

### B-08 box64 启动环境告警（低危/信息）
- `lscpu: 确定 CPU 数 失败：/sys/devices/system/cpu/possible: 没有那个文件或目录`（box64 调 lscpu 在 proot 内失败，仅告警）；反复 `Warning, program break not found`；`Hardware counter is too slow (0 kHz)`；`Didn't detect 48bits of address space, considering it's 39bits`；`Running on Unknown CPU`。

### B-09 SSL 证书错误（曾出现、已消失）
- 历史 stderr：`src/common/opensslconnection.cpp (1706): unable to load trusted SSL root certificates`。当前 proot 内 `/etc/ssl/certs/ca-certificates.crt` 存在（244 证书，ca-certificates 已装），最近三次运行 0 计数。

### B-10 `__udivti3` weak stub 抢占 libgcc → host 128 位除法商恒 0 → BN mod 系全错 → SSL 证书解析失败（已修复 ✅）
- **症状链**：`BN_mod_inverse`/`BN_mod`/`BN_mod_exp` 结果错误 → `X509_PUBKEY_get0: decode error x_pubkey.c:458`/`unable to find public key parameters statem_clnt.c:1904`（EC P-256 证书解析失败）→ TLS 握手失败 → CM WebSocket 全灭 → steamcmd `ERROR (No Connection)`。bntest 首个失败点 `FAIL inverse check`；x509test native `X509_OK PKEY id=0x198 bits=256` vs box `PUBKEY_FAIL`。
- **根因**：`wrappedlibc_private.h:2762 GOM(__udivti3, HFHH)` 被 `gen-libc-stubs.py` 提取为**返回 0 的 weak stub**，`glibc_missing_symbols.c` 作为 CMake 无条件 object **直接参与链接** → weak 定义先满足 `div64` 对 `__udivti3` 的 undefined → **libgcc.a 强符号成员不再拉入** → host 侧 `dvd / s`（128/128 除法）商恒 0（高 64 位 = 参数寄存器 x1 残值）。`__umodti3` 不在提取源 → 无 stub → libgcc 照常拉入 → **余数始终正确**（该不对称是定案关键）。`my___udivti3`（guest 用）`return a/b` 同样递归到坏 stub。
- **后果**（解释器 `div64` @x64primop.c）：商=0/残值 → `if (div > 0xffffffffffffffffL)` 被残值高位触发 `INTR_RAISE_DIV0`（`emu->error |= ERR_DIVBY0=2`，不杀进程）→ **RAX/RDX 不写回**（minidiv 表现 `q=5 r=2` 输入保持）；小除数场景商静默写 0。dynarec 的 RDX≠0 路径 `CALL(const_div64)` 同中招；RDX==0 走内联 UDIV 快路径不受影响（故日常程序没事，BN 的 128/64 除法必炸）。x86 bn_div_words → `divq hi≠0` 全错 → `BN_div`/`nnmod` 错 → mod 系全线错。
- **证据**（解释器打点版 [DIVT]/[DIV64]，CI run 36226795730）：
  - `[DIVT] s=3 rax=5 rdx=2 rip=4018e7` → `[DIV64] dvd_hi=2 dvd_lo=5 quot_hi=2 quot_lo=0 mod=1 DIV0_overflow` → `[DIVT]-> rax=5 rdx=2 err=2`（除数读取正确、RIP 正确、mod=1 正确，仅商错）。
  - `s=40 dvd_lo=3b7 → quot=0 mod=37`（应商 0xE）、`s=8 dvd_lo=600000 → quot=0 mod=0`（应 0xC0000）——**商恒 0 或 (残值<<64)，mod 全对**。
  - 反证排除：div64 C 源/编译产物（`bl __udivti3; cmp x1,#0; b.gt`）逻辑正确；musl gcc16.1 -O2 同款 probe 远端原生跑 `cmp(div > 0xffffffffffffffffL)=0` 且商正确；flagstest/flagstest2（含 ADX/BMI2/128 位 mul）全绿；`BOX64_DYNAREC=0` 与 dynarec 同错。
- **修复**：`build-box64-musl.sh` 的 gen-libc-stubs 调用加 `--no-stub __udivti3 __divti3 __umodti3 __modti3 __udivmodti4 __udivdi3 __divdi3 __umoddi3 __moddi3 __udivmoddi4`（编译器 RT 黑名单，libgcc 提供定义，`&N` 取址由强符号满足）。
- **排查副产物（坑）**：`BOX64_LOG=3` == `LOG_NEVER`（debug.h），`applyCustomRules()`（env.c:152）会降级为 `log=2` 并自动 `dump=1` → 此构建**无指令级 trace 档**，只能源码打点；日志中 "Variables overridden" 即此机制。proot 内写 `/media/termux/home/logs/*.log` 事后不可见，须写 `/root/` 并 grep 到 stdout。
- **状态：已修复并全链验证 ✅**（2026-09-26）。CI 36227644544（修复+打点）→ minidiv/divtest/bntest2 与 native diff 零（`quot=0xaaaaaaaaaaaaaaac`、`nnmod/mod_inverse` 全对）；x509test `X509_OK PKEY id=0x198 bits=256`（native 同值，原 `PUBKEY_FAIL`）；tlsx86 `TLS_OK ver=TLSv1.3 cipher=TLS_AES_256_GCM_SHA384`；正式版 CI 36228057671（已撤打点，产物 grep 无 DIV64/DIVT）部署 `~/box64-patched`；steamcmd 对照实验：旧版 `Retrying → ERROR (No Connection)` vs 新版 `Waiting for client config OK → Waiting for user info OK`，connection_log `ConnectionCompleted + RecvMsgClientLogOnResponse 'OK' → [Logged On]`，本窗口零 ConnectFailed，16:07:02 正常 LogOff。首次运行偶发卡 `Loading Steam API...`（"didn't shutdown cleanly → update check" 后），重跑即通，未见于正式版复测。

### B-11 musl `clone()` 包装层对线程型 flags 返回 EINVAL → guest `pthread_create` 全灭 EPERM（已修复 ✅）
- **症状**：现代 glibc 程序在 box64 下所有 `pthread_create` 返回 1 (EPERM)、join 3 (ESRCH)——thrmin 矩阵全 rc=1、mthrd mutex 计数 0/barrier 卡死（steamcmd 自身老 glibc 用 raw clone 不受影响，12 guest 线程能建成）。
- **根因链**：guest glibc raw `clone(CLONE_THREAD|SETTLS|CHILD_CLEARTID|...)` → box64 case56 新栈分支调 **musl 公开 `clone()` 包装**（x64syscall.c:795/1265）→ musl `clone` C 层反汇编（libc.a）：`mov w7,#0x290000`(=CLEARTID|SETTLS|THREAD) + `tst` + `ccmp` + `b.eq` → **flags 命中任一位直接 `return -EINVAL`**（musl pthread 内部直调 `__clone` 跳过此检查故无碍）→ box64 `S_RAX = ret`（clone() 失败返回 -1）→ guest 解读 errno=1(EPERM)（-1==EPERM 巧合），glibc 收 EPERM 不 fallback。
- **证据**：[CLONE] 打点（CI 36236391508）`newstk ret=-1 errno=22`；clonehost3 矩阵：同 flags `clone()` 包装 EINVAL vs **raw `syscall(SYS_clone,...)` 成功**（内核/proot 无罪）；box64 对 clone3(435) 返 -ENOSYS 正常 fallback（非本因）；host musl 直跑测试全 rc=0（环境正常）。
- **修复**（b148589 `scripts/patch_clone_raw.py`）：case56 两处新栈分支改 `extern int __clone(...) __attribute__((weak))` 直调底层 `__clone`（musl pthread 同路径，无检查，返回 raw -errno 顺带修复假 EPERM；glibc 构建下弱符号回退原 `clone()`——glibc 包装无此坑）。
- **验证**（CI 36237479586 → `~/box64cf`）：thrmin 解释器+dynarec 全 rc=0（串行/attr/并发10/释放重建）；mthrd `mutex_counter=160000 ok`、`payload=10000 ok`、barrier/sem ok、join 全 0；cloneprobe `r=8208 errno=0`。

### B-12 dynarec 下 steamcmd 偶发卡死 `Loading Steam API...`（观察中 ⏳）
- **症状**：dynarec 跑 steamcmd 约 50% 概率输出停在 `Loading Steam API...`（rc=124 timeout），无 [CLONERAW]、无登录判据；解释器（`BOX64_DYNAREC=0`）稳定完成登录。
- **已排除**：clone 线程创建失败（B-11 修复后仍复现，卡死时无 clone 打点）；单开关效应——`BIGBLOCK=0/CALLRET=0/SAFEFLAGS=2/AVX=0` 各有成败、`BIGBLOCK=0` 连跑 2 次 1 过 1 卡、默认连跑 3 次全卡 → **非确定性开关 bug，属偶发竞态**（与 B-06 退出期 `could not immediately join` 同域嫌疑）。
- **已知缓解**：重跑即通（概率性）；解释器模式必通但慢。
- **现场采样（b12probe.sh，14 线程）**：主线程 `hrtimer_nanosleep` 轮询（syscall 101）；12 个 guest 线程全在 `futex_wait_queue_me`、2 个在 `SyS_epoll_wait`（IOCP Thread 0、IPC:CSteamEngin）→ 所有 worker 均休眠、无忙等，**主线程在等一个永不到达的条件（工作项/唤醒丢失形态）**，非死循环。
- **已排除（subagent 审查后定向实验）**：P0-1 内存序——`BOX64_DYNAREC_STRONGMEM=1` 对照 6 轮 4 卡（与基线 ~50-60% 无差异）；P0-4 epoll_pwait2 超时溢出——修复后累计 12 轮 8 卡 4 GOT_CONFIG、0 LOGIN_OK（与基线相当，guest 不走该路径）。
- **嫌疑收窄（dynarec 独有无锁路径，subagent 报告1不确定①）**：`dynablock.c:447` 一带 `need_lock=0` 时 `FreeDynablock(block,0,0)`/`getDB`/`MarkDynablock` 无锁操作块表与 jump table——填块失败竞态可致跳转表残缺 → 偶发执行流丢失，待验证。
- **下一步**：验证 need_lock=0 路径是否真无锁（assert/插桩）；2 个 epoll 线程行为深入（无法从 wchan 区分正常等事件与永久阻塞）。

### 批次1 修复清单（subagent 三报告落地，CI run 36247763029，已远端验证无回归）
- **P0-2** `arm64_lock.S` storeb/store/store_dd 屏障在 store 后 → 改 release store（`dmb ish; stlr*`）。
- **P0-3** `arm64_atomic_storeifref_d` casal 后缺 `cmp` 就 `bne`（NZCV 残留）→ 补 cmp 再 bne。
- **P0-4** epoll_pwait2 `1<<31` int 溢出恒真 → INT_MIN 无限阻塞 → 改 0x7fffffff 夹取。
- **P1-7** my_epoll_wait/pwait 无界 VLA → 入口 `maxevents<=0||>4096` 拒绝。
- **P1-8** `arm64_lock_write_dq` 缺 ldxp/stlxp 失败重试 → 补 CAS 环（read_dq 已有前导 dmb，原报告该项不成立）。
- **B10 附带**：syscalls 表补 `recvmmsg(308)`（与 sendmmsg(307) 对称，原缺 → ENOSYS）。
- **B-10 防复发**：gen-libc-stubs `--no-stub-regex` 7 条内置默认（div/mod/mul/add/sub/ash/float/fix/unord/extents 族）+ `FORBIDDEN_STUB` 检查（含 `__memcmpeq` 恒0隐患、`__cxa_pure_virtual`——本地 grep 结论错误，CI 实测在 func_refs 中）+ `--check` 接通 + SMART_EXTRA 两个实现（__memcmpeq→memcmp、__cxa_pure_virtual→abort）。
- **构建期 fail-fast（#6/#7）**：patch-musl-isnanf.py 15 处 assert + 23 处 print警告 改 `fail()` 收集、末尾 `sys.exit(1)`；build 脚本 gcc -E(clean) 失败改硬错。
- **管线（#1/#2/#3/#13）**：objcopy 符号分离（box64+同源 .debug 双产物，artifact 12.2MB）；actions/cache（工具链 tarball+ccache，CI 4m8s）；`timeout-minutes: 45`；**qemu-user 冒烟测试**（minidiv/thrmin，DIV_OK/THR_OK，B-10 类静默错误 5 分钟红灯）。
- **GCC14 兼容**：gen-libc-stubs `.c` 模板 include `<math.h>`（lgamma 隐式声明由 warning 升 error）。
- **远端验证**：thrmin/mthrd/x509test(cmcert.pem 参数)/tlsx86/bntest2 全绿；steamcmd 12 轮 8 卡 4 GOT（无回归、无改善）。

## 三、遗留未完成项

- ~~ADX 测试：/tmp/test_adx.c 待编译验证。~~ 已完成：flagstest2（mulx/adcx/adox/bn_mul_add 链/rep movsq/lzcnt/tzcnt）native 与 box 均 ALL_OK。
- get_robust_list 断言未处理。
- `crashhandler.so.bak` 未恢复（linux64/ 下 crashhandler.so 与 .bak 并存）。
- v024 运行时机制移植（main 静态符号绑定）：用户已选搁置。
- v024 符号版产物：/tmp/fix-art/、/tmp/sym-art/（28.9MB）保留。
- **开发环境（本地 WSL，非 box64 bug）**：① GitHub SSH 22 被拒 → `~/.ssh/config` 走 `ssh.github.com:443`；② 本地 Windows 侧 SteamTools 向 GitHub 注入 MITM 证书 → gh/curl 需 `SSL_CERT_FILE=/tmp/full-ca.pem`（curl 用 `CURL_CA_BUNDLE=`），CA 合并文件由 Windows 根库导出的 steamtools-ca.pem 拼成；③ artifact 下载 gh run download 卡 → `curl -C -` 断点续传。
- **subagent 审查未修项（报告1）**：P1-5 my_pthread_once 10ms 强制放行；P1-6 guest sigset 不转换（signalfd/pthread_sigmask 直通）；P2-9 pthread 静默丢 guest 栈 + SetFS 注释；P2-10 TERMUX 分支 attr 假成功。
- 目标机 CPU：**Cortex-A53×8**（无 dotprod/fp16/atomics/rcpc）→ A1 优化按 `-march=armv8-a+crypto+crc -mtune=cortex-a53` 落地，禁用 armv8.2-a 系（SIGILL）。
