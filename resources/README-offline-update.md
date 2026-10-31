# 随屏幕 Git 仓库交付的离线更新运行时

发布人员将 `build-offline-runtime.py` 验证后的完整输出放入 `resources/offline-update/`，并作为普通 Git 文件发布。
打印机启动时读取 `manifest.json`，部署匹配的 ARM64 引擎、公钥和本机配置；现场用户不运行安装命令。

本仓库从 `KlipperScreenWithAI` 移植离线更新功能及已校验的 ARM64 运行时，沿用其
`resources/offline-update/release-public.pem` 发布公钥；未创建或复制发布私钥。
签发 `.mdu` 需要由持有对应私钥的发布环境完成，私钥不进入 Git、打印机或更新包。

当前已交付完整 `resources/offline-update/manifest.json`、ARM64 SWUpdate、五个私有运行库、发布公钥、
源码与许可证，以及不限制打印机机型的通用程序级策略。构建收据标记 `cross-validated`：
构建人员使用 Ubuntu 22.04 ARM64 的目标头文件和库进行交叉编译；ABI 报告中引擎及随附库要求的最高 GLIBC 版本为 2.34。

引擎报告包含真实 ARM64 的签名、兼容性与内容完整性检查；检查模式不执行包内脚本。
报告位于 `engine/validation/checks.json`，收据仍声明 `physicalHardwareTested=false`；
物理 ARM64、屏幕自更新、CAN 刷写及断电恢复仍待验收。

当前通用策略不要求出厂 `device-profile.json`，不匹配 SKU、序列号或主板型号。初始化按 CPU 选择资源，不限制发行版或 Ubuntu 版本，并实际执行引擎的 `--version/--help` 自检。
当前资源仅含 `aarch64`；其他 CPU 需要对应引擎，库或系统能力不足导致引擎无法运行时仍会阻止初始化。包的系统适用条件交给签名脚本检查。
屏幕不保存或验证 MCU 参数表；固件、工具、参数和恢复流程由包提供并核对实物板卡。`baseline={}` 不猜现有组件版本；已有本机库存保留，新设备的未知组件仍受基线准入限制。
仅有本次运行时并不意味着已具备所有程序和板卡的正式 `.mdu`。缺文件、权限、CPU 不支持或引擎无法运行时仍显示具体 `BLOCKED` 原因。
程序不查询 Moonraker 或 Updater 的更新互斥接口。本程序内部锁只协调离线任务，不能阻止外部在线更新或 Web、云端打印命令。
初始化不安装 Klipper 或 Linux MCU 的启动附加配置，不增加核心服务的启动条件或恢复依赖。打印状态、温度、任务冲突和包身份由执行器在开始升级时检查；独立开机恢复只处理屏幕文件与任务记录。
初始化和运行时同步不要求 Klipper 就绪、打印空闲或温度冷却；存在未完成任务或内部操作竞争时显示 `DEFERRED` 并重试。资源、权限、CPU 架构和原生引擎自检仍须通过。

发布示例：

```sh
python3 scripts/build-offline-runtime.py --recipe /release/runtime.recipe.json --output /release/runtime-ready
python3 scripts/build-offline-runtime.py --check-only /release/runtime-ready
```

制包人员使用 `scripts/swupdate/runtime.recipe.example.json` 编写真实物料清单。相对路径以 recipe 所在目录为基准。
工具拒绝覆盖已有输出目录；验证通过后，用输出完整替换 `resources/offline-update/`，并将输出中所有普通文件加入 Git。
发布检查必须对仓库中的最终目录再次执行 `--check-only resources/offline-update`。不使用 Git LFS 指针。

输出包含：

- `manifest.json`：schemaVersion 1、目标 CPU 架构、所有资产的 SHA-256、通用策略和引擎路径。
- `engine/`：ARM64 `swupdate`、生产构建收据 `build-info.json` 及真实 `swupdate.config`。
- `engine/COPYING.swupdate`、`engine/LICENSES/`、源码归档或 `engine/sources/`：发行所需的源码和许可证。
- `engine/dependency-sources/`、`engine/dependency-licenses/`：私有运行库的对应源码、发行补丁、版权和许可文本。
- `engine/reproducibility/`：构建和来源校验脚本。
- `engine/abi-validation.json`、构建环境及 `engine/validation/` 等报告：现有构建证据，全部纳入摘要，不复制到任务执行目录。
- `release-public.pem`：RSA 发布公钥；私钥留在制包端。
- `profiles/generic/`：通用 policy 和基线库存。
- `lib/`：引擎依赖的 ARM64 动态库；可从引擎交付目录的 `lib/` 自动收集，或由 recipe 的 `libraries` 提供。提供真实文件，不使用符号链接。

每条基线库存必须提供 `version`、正整数 `sequence` 及 `evidence`。初始化和后续同步先保留已登记条目，
再匹配随屏幕发布的 baseline 中尚未登记组件的实际证据，匹配后追加版本，不改已有版本、序号或包身份。
自动补登记仅来自屏幕发行物和真实安装证据，不从任意 `.mdu` 提取或猜测。不匹配或文件缺失的组件不登记，更新包涉及这些组件时拒绝安装，
其他已核实组件可以更新。基线结构错误会阻止初始化。支持 Git 完整 commit、JSON 顶层版本字段、纯文本版本文件：

```json
{
  "sdk": {
    "version": "实际发布版本",
    "sequence": 1,
    "evidence": {
      "kind": "git",
      "path": "/home/mingda/MINGDA-3DPrinting-SDK",
      "commit": "填入实际40位小写commit，不可使用占位值"
    }
  }
}
```

屏幕自身使用同版本提交的版本文件提供 `text` 或 `json` 证据。不要在屏幕仓库的基线里填写
该次提交自身的 Git commit：基线也是此次提交的一部分，发布人员无法预先获得这个值。

运行时 policy 不需要 MCU 参数表。`.mdu` 的 `hardware/boardProfiles` 为可选说明，制包工具不要求正式板卡表；签名包的 `preinst` 必须只读检查所有目标适用条件，写入前再次核对真实板卡及固件。
进入 Bootloader 前及逐板写入前必须在任务目录持久记录步骤，以便中断恢复。签名或检查模式通过不能证明 MCU 检查或刷写成功。
发布人员仍需完成目标 ARM64 系统的引擎启动、库依赖、更新及恢复验收。
