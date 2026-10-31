# Mainsail 离线更新包

更新 `/home/mingda/mainsail` 的完整静态网页，原样保留现有 `config.json`、已存在路径的权限及所有者。
不运行 npm、不下载依赖、不修改 nginx 配置、不重启 nginx、Moonraker、Klipper 或屏幕，不涉及 MCU。

准备阶段只读检查签名物料、tar 成员摘要、安装路径、空间、打印空闲状态和当前 HTTP 页面。
安装阶段在同一文件系统的私有目录暂存新网页，持久保存原目录完整清单和交换意图，使用 Linux
`renameat2(RENAME_EXCHANGE)` 原子替换目录。旧目录作为完整备份留在任务记录指向的私有目录。
验证新 index、带 hash 的入口资源、service worker、版本及发布元数据；失败原子恢复旧目录并验证 HTTP。
掉电后通过同一任务标记判断目录是否已经交换，屏幕上的原任务可恢复；不增加开机服务或启动保护。

允许同版本重新安装。本次源仓库 `Mingda-tech/mainsail` 版本为 `v2.11.5`，与 `.125` 当前显示相同；
包版本是独立的离线发布号。浏览器已有页面可能显示 PWA 更新提示，刷新页面或确认更新后加载新资源。
不清除用户浏览器缓存或更改原有 PWA 行为。

包内 `mainsail-source.tar.gz` 为制包提交的完整 GPL 源码（不含 Git 元数据、node_modules、dist 或秘密），
`COPYING.mainsail` 为原仓库许可证，`BUILD-Mainsail.txt` 记录源码提交、锁文件和构建命令。
构建使用 Node 20、`npm ci`、`vite build`；最后的 zip 命令仅生成普通分发附件，MDU 使用同一 dist 文件。
签名私钥仅在制包端使用，不进入更新包。
