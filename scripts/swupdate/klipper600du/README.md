# 600DU Klipper + 喷头应用固件组合包

这是提供的 600DU 操作指南对应的专用测试包，必须由屏幕选择并确认安装。
它不适用于当前测试机 `192.168.1.125` 的 `4d4400000302` 喷头。

## 安装前提

- 屏幕已安装本项目当前修复后的 SWUpdate `.mdu` 运行时，使用本项目的发布公钥；
  请使用支持“旧版本未知仍可升级”、进度高水位与任务恢复的屏幕版本测试。
- 无需手动登记原程序或喷头版本。没有历史记录、旧版本为空或版本格式无法识别时，
  显示“未知”，继续通过包内硬件、配置、备份和恢复检查后升级；成功才登记新版本。
  已识别出的较新版本仍拒绝降级。旧版屏幕执行器遇到未登记组件时仍可能拒绝，
  需要先更新屏幕自带运行时；本包不使用 `installIfAbsent` 冒充组件不存在。
- Klipper 位于 `/home/mingda/klipper`，服务为 `klipper.service`，使用现有
  `/home/mingda/klippy-env/bin/python`。Git origin 必须是提供的
  `Mingda-tech/MD_PI_klipper` 仓库，受管源码干净。无法判断旧提交对应的发行版本或先后关系时
  保留真实旧提交并继续；能证明旧版本更新时拒绝降级。来源不符、现场修改或损坏的 Git
  安装结构仍会单独拒绝，不能作为“版本未知”忽略。
- 现有 Python 环境满足仓库声明的依赖，且本机有 GCC。包复用该环境，在内部暂存目录
  离线重建 `c_helper.so`；不执行联网 apt/pip 或旧的系统安装脚本。
- 活跃主配置为 `/home/mingda/printer_data/config/printer.cfg`，实际包含
  `load_cell.cfg`，已有 `[load_cell load_cell_head_l]`、CS1237 传感器和
  `600D_HB:CS_DOUT` / `600D_HB:CS_SCLK` 引脚。未知或缺少的现场配置会在写入前拒绝。
- `[mcu 600D_HB]` 地址为 `4d4400000a02`，`can0` 已工作在 1 Mbit/s；
  喷头为 STM32F072xB、48 MHz、PB8/PB9 CAN，CanBoot 应用起始地址为 `0x08002000`。
- 打印机 ready、空闲、未暂停或校准，所有加热目标为零，温度不超过 50°C。

## 包内物料

- Klipper：`https://github.com/Mingda-tech/MD_PI_klipper.git`，固定提交
  `d73ed227ecc7d2fae7bc1fa0278a8f04e8838ccb`，版本 `v0.13.1-32-gd73ed227`。
- 喷头应用：用户提供的 `klipper.bin`，34368 字节，SHA256：
  `62f476a00b1bddcfbcd02bac045c69ca30358916bc97189d4b57412d4ea3d08a`。
- 固件身份：`v0.13.1-34-g0770141c-dirty-20260715_224036-MINGDA`，包含 CS1237 v2 协议。
- 配置：用户提供的 `load_cell.cfg` 模板。仅迁移现有传感器的协议版本及缺少参数，
  保留校准值、已有调参、原宏、动态变量和 `SAVE_CONFIG` 内容。
- 刷写工具：上述 Klipper 提交中的 `lib/canboot/flash_can.py` 及包内安全适配器。

不刷写主板、Linux MCU 或任何 Bootloader，也不更改 KlipperScreen、自检页面、
Moonraker 程序或系统服务定义。运行时只停止/启动 `klipper.service`，并在配套切换后
请求一次应用固件重启，最后核对全部原 MCU 的连接、硬件身份及已知版本。

## 执行和恢复

1. 原生 SWUpdate 验签、校验物料摘要；屏幕准备页不执行硬件预检或刷写。
2. 包内 `preinst` 只读核对服务实际 PID/参数、活跃配置包含关系、Git、依赖、空间和板卡。
3. 同盘暂存 Git 源码、重建本机辅助库、检查活跃扩展依赖，并验证真实原子目录交换能力。
4. 再查打印状态，停止 Klipper，在进入 Bootloader 和首个写入命令前持久保存恢复记录。
5. 核对 Bootloader UUID、芯片和应用地址，再刷写指定喷头；逐块读回并核对摘要后才启动应用。
6. 切换配套 Klipper 和迁移后的配置，重启 Klipper，验证 ready、配置 v2、喷头明确的新版本及其他 MCU 的连接、硬件身份和已知原版本。

首次物理写入后，失败保留同任务、同包和备份进入恢复；不会单独回退旧 Klipper 或旧配置。
恢复仍拒绝明确的打印、暂停、校准或加热状态。只有原硬件因中断无法回答时，才允许该任务修复。
开机不自动刷写；需从屏幕原任务选择恢复。

本包尚未在目标 600DU 上实际刷写验收。签名、模拟传输、配置/Git 恢复测试和 ARM64
原生检查不等于实际板卡验收。

## 制作

在制包电脑运行 `scripts/build-600du-mdu.py`，指定隔离的源仓库、完整提交、原始固件、
配置模板、版本/序号、私钥和新的 `.mdu` 输出路径。源仓库本地的未提交修改不进入包中；
私钥不进入包中。生成的 `.materials/build-info.json` 记录确切物料和包摘要。
