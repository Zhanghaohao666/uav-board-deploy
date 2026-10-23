# E1X 预编译采集工具与正常停止补丁

新板刷好同型号厂家 ARM64 镜像后，完整部署直接安装 `runtime/linux-aarch64/get_pcd_record`，不要求用户另外安装 SDK 或在新板编译。该工具来自已验证 SDK，动态依赖仅为镜像的 `libstdc++`、`libm`、`libgcc_s` 和 `libc`；安装前执行 ELF 架构、SHA256、动态库和 `--version` 检查。SHA256 为 `0e9531de5c2cafc6d57191dbd1db9a4d8580edc294fc54d68ca11ab649fce471`，版本与修复声明在同目录 `manifest.json`。

独立入口：

```bash
sudo python3 tools/seyond/install_runtime.py --interface eth1 --check
sudo python3 tools/seyond/install_runtime.py --interface eth1
```

`--check` 只读验证，`--no-start` 只安装并启用下次开机网络；这两个步骤都不会开始点云采集。默认主机雷达地址 `172.168.1.100/24`，雷达地址 `172.168.1.10`。安装器检查同网段/接口占用，保留其他网口和网关，支持已有 NetworkManager 静态连接复用。离线包包含工具和许可证，不需要互联网。

安装器为上层部署事务提供 `prepare_install(interface, host_cidr, lidar_ip, runtime_dir, reserved_ifaces)`、`apply_plan(plan, start=True)` 与 `rollback(receipt)`。预检返回计划且不写入，实际安装自动备份、逐文件原子替换，失败只撤销本次新增的地址及文件。调用者必须把板间、MK22、扩展网口作为 `reserved_ifaces` 传入；运行时还会重新读取主控配置检查。默认 `/opt/uav-lidar-runtime/<SHA 前16位>/` 不覆盖旧工具；相同 SHA 的现有可执行文件会直接复用。

下面的构建步骤仅供开发者重新生成工具或移植新 SDK，普通部署不需要执行。

适用于已测试的 Seyond SDK 3.103.13，提交 `73a24e98c283391b890cfd106e5cc13e9c9d7f5b`。厂家 SDK 的 `get_pcd` 默认不处理 SIGTERM/SIGINT，直接终止可能丢失最后尚在内存中的数据；这个补丁只给独立录制工具增加停止请求、下一个帧边界退出和文件缓存排空。

原始格式、点云计算与缓存大小不变。2 秒没有新帧边界时走停止兜底；板端监督程序另有最终超时。日志 `UAV_RECORD_STOPPED ... writer_drained=1` 证明缓存排空，不能证明没有丢包，也不等同于录满指定时长。

在 RK3588 上使用已经构建好、来源一致的源码与静态库离线构建：

```bash
sudo python3 tools/seyond/build_recorder.py \
  --sdk-root /data/uav-lidar-tools-73a24e9/sdk \
  --output /data/uav-lidar-record-sdk-new
```

输出目录必须不存在；原厂家工具不会被覆盖。需要本机 `g++`、`patch` 和对应 SDK 的 `lib/*.a`。源码/静态库须保留已验证的 TCP 初始化修复（TCP 不依赖 UDP 目的地址）；本补丁不设置雷达网络或 UDP 参数。

用历史 `.inno_pc` 验证自然分段、SIGTERM 停止与旧 SDK 离线转 PCD 后，方可更新运行包中的工具、清单及对应 SHA。自行编译时也可将 `/etc/uav-lidar.json` 的 `sdk` 指向生成的 `seyond-get-pcd-record`。其余配置字段可省略使用默认值。若只配置原版 SDK，后端仍支持录制，但提前停止时最后分段可能不完整。

104 的独立 ARM64 工具已验证：自然结束产生 3 个分段；SIGTERM 在帧边界停止并排空写入；四个文件均由原 SDK 转换为有效单帧 PCD。此测试使用历史样本，不是当前雷达在线长录验收。

厂家许可证见 `SEYOND_LICENSE`；补丁不包含任何授权私钥、板端凭证或原始现场点云。
