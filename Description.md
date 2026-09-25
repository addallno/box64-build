# box64-build

通过 GitHub Actions 交叉编译 **box64** 的 aarch64 静态单文件（支持 box32，即同时模拟 x86_64 与 x86_32 程序）。

## 用途

- 目标平台：**aarch64**（Proot Linux 与 Android 通用），产物为 musl 静态链接单文件
- 启用特性：
  - `ARM_DYNAREC=ON` —— ARM64 动态重编译（必须）
  - `BOX32=ON` —— 同时模拟 32 位 x86 程序（替代 box86，无需单独编 armhf box86）
  - `STATICBUILD=ON` —— 静态链接，单文件分发
  - `BAD_SIGNAL=ON` —— Android 混合内核信号兼容
  - `NOGIT=1` —— 从源码 zip 构建时省略 git SHA
- 硬件：骁龙 652 (MSM8976SG) 8 核 A53，Android 7.1.1，6GB RAM / 512MB swap，磁盘仅剩 ~7.6GB

## 架构

- **CI**：GitHub Actions（`ubuntu-latest`），本地不做任何编译（设备性能红线）
- **工具链**：`cross-tools/musl-cross` release `20260515` 的 `aarch64-unknown-linux-musl.tar.xz`（与 termoneplus-tools 的 dropbear 编译同一工具链源）
- **box64 源码**：`ptitSeb/box64` 官方仓库 git clone（`-DNOGIT=1` 用不上，直接 clone 取最新 main）

## 文件

| 文件 | 说明 |
|------|------|
| `.github/workflows/build.yml` | CI 工作流：拉工具链 → clone box64 → cmake 交叉编译 → strip → 上传 artifact |
| `scripts/build-box64-musl.sh` | 实际编译脚本（workflow 调用） |
| `scripts/patch-musl-isnanf.py` | 源码补丁脚本：修 musl isnanf + 伪造 robust futex 系统调用（编译前注入） |
| `Description.md` | 本文档 |

## 命令

```bash
# 手动触发编译（必须显式触发，默认不自动跑）
gh workflow run build.yml
```

## 产物

`box64-aarch64-musl`（静态单文件，strip 后预计 ~几 MB）。运行示例：

```sh
BOX64_LD_LIBRARY_PATH=./x64lib ./box64-aarch64-musl ./some_x86_64_program
```

## steamcmd get_robust_list 断言修复（patch-musl-isnanf.py）

STATICBUILD box64 下运行 steamcmd 时，steamclient.so 的 robust futex 初始化代码会因宿主
`get_robust_list` 返回 EPERM 而走入断言失败路径（`mov dword[0],0` 主动 SIGSEGV）。
补丁在 `x64syscall.c` 中伪造 273/274 号系统调用：

- **伪造结构**：file-scope `static` 5 字段结构 `rh`，自满足 steamclient 的链表回指校验
  `[*head & ~1 - 8] == head`（`next = &rh+0x20`、`head_ref = &rh`、`futex_offset = -0x20`）；
  `len` 写死 `0x18`（断言硬要求，与 `sizeof(rh)` 无关）。
- **双槽写入**：断言检查帧比 syscall 入口帧高 `0x190`（实测），除 `head_p/len_p` 直接槽外
  还需补写 `+0x190` 槽位。
- **方案A 恢复**：`+0x190` 槽位原值是上层帧 flag/canary，`my274` 写假值前存入 file-scope
  `rb_rest`，同线程下一次进入 `my_syscall` / `x64Syscall_linux` 时恢复，避免 stack_chk 报错。
- 幂等：`patch_x64syscall_c` 共 6 处替换，重复执行返回 0；仅修改 `x64syscall.c`，
  干净源用 `git -C box64 show HEAD:src/emu/x64syscall.c` 导出。

验证（远端 proot 容器内 `~/run-in-proot.sh`）：`Loading Steam API... OK` → 正常登录流程，
无 SIGSEGV / stack_chk。

## 说明

- box64 静态版仅内置少量 wrapped 库（libc/libm/libpthread），图形/音频等 x86 库需通过 `BOX64_LD_LIBRARY_PATH` 指向 x64lib。
- BOX32 模式运行时需把 32 位 x86 库路径放入 `BOX32_LD_LIBRARY_PATH`（从 box64 仓库 `x86lib/` 获取）。
