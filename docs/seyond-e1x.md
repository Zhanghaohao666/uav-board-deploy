# Seyond Robin E1X 接入 104 和保存点云

依据用户提供的《Seyond Robin E1X 激光雷达用户手册》CN V1.4.0（2025-05-08），以及 [厂家 SDK](https://github.com/Seyond-Inc/inno-lidar-sdk) 和 [get_pcd 文档](https://github.com/Seyond-Inc/inno-lidar-sdk/blob/main/docs/get_pcd.md)。雷达点云不是 RTSP 视频，不能直接填入地面站视频地址。

## 接线和网络

手册第 9–10、15–19 页：供电 9–16 V DC，推荐 12 V，典型约 6 W；数据口是 1000BASE-T1。使用厂家 T1 → 普通以太网转换盒，再用 RJ45 接 104 的千兆网口或千兆交换机。用户已确认有转换盒。上电约 7–8 秒初始化。

默认雷达 IP **`172.168.1.10`**。给实际接雷达的主控网口添加例如 **`172.168.1.100/24`**，不设置网关，不替换已有算法板/云台地址。这是厂家实际使用的地址，并非 `192.168.1.10`。该网段不是 RFC1918 私网地址，只用于隔离的载荷网络，不接到互联网侧。

优先使用空闲网口或额外千兆 USB 网卡。需要与算法板共用主控 eth0 时，通过千兆交换机连接主控、算法板及转换盒，并给 eth0 添加第二个网段即可。MK22 的网口继续单独使用。104 在准备工具时 eth0 连接算法板、扩展 USB 网卡连接云台，eth1 为 DOWN；是否有可用物理 eth1 口仍须现场核实。

以下是**确认转换盒确实接 eth0 后**的临时配置示例，不会清空其他地址：

```bash
ip -br addr
sudo ip address add 172.168.1.100/24 dev eth0
ip route get 172.168.1.10
ping -I eth0 -c 3 172.168.1.10
```

`route get` 应指向实际载荷接口及 `src 172.168.1.100`。其他网口替换 `eth0`；先确认同网段没有另一台 `.100`。以上地址重启后消失，永久配置需在确认接线并验收后加入该板的受管网络配置。准备工具阶段没有替用户修改网口地址。

## 104 上已准备的接收工具

已编译厂家 SDK 的 ARM64 `get_pcd`，命令入口：

```bash
seyond-get-pcd --help
```

SDK 固定提交 `73a24e98c283391b890cfd106e5cc13e9c9d7f5b`（SDK 3.103.13），源代码、BSD 许可证及构建文件保存在 `/data/uav-lidar-tools-73a24e9/`。仅将这份副本的 x86 编译标志改为 ARM64 CRC 标志；没有改驱动数据处理，也没有部署 ROS、修改雷达参数或启动点云采集。已验证命令帮助和动态库加载；实际 E1X 数据接收仍须接线后测试。

网络通后，先采 100 帧原始点云，10 Hz 时约 10 秒。先创建新的数据目录，避免厂家工具覆盖同名文件：

```bash
mountpoint -q /data || exit 1
mkdir -p /data/uav-lidar
capture_dir=$(mktemp -d /data/uav-lidar/e1x_$(date +%Y%m%d_%H%M%S)_XXXXXX)
seyond-get-pcd --lidar-ip 172.168.1.10 --lidar-port 8010 --use-tcp \
  --frame-number 100 --output-filename "$capture_dir/raw.inno_pc"
```

SDK 使用 TCP 8010 建立数据连接；最初调试用 TCP 避免 UDP 目的地址/接收端口配错。看日志中收到的数据包及帧号是否增加，并检查输出文件增长。命令录满指定帧后自动关闭文件。若雷达固件版本不兼容，先向厂家确认与 SDK 3.103.13 的匹配版本。

`.inno_pc` 是 SDK 支持回放的原始点云格式。此版本对 Robin 紧凑点云会在文件中写入角度表，保留原始数据便于后续解码。没有雷达输入时，工具可能持续等连接，不能仅凭进程存活判断成功。直接 Ctrl+C 强制退出厂家程序可能截断最后的数据，不保证完整收尾；常规采样建议使用指定帧数自动结束。

延长采样可以用 `--frame-number 600`（10 Hz 约 1 分钟）。分多个文件，例如：

```bash
seyond-get-pcd --lidar-ip 172.168.1.10 --lidar-port 8010 --use-tcp \
  --frame-number 600 --file-number 10 --output-filename "$capture_dir/raw.inno_pc"
```

这会生成 `raw-0.inno_pc` 等文件，总计 6000 帧，约 10 分钟。`--frame-number` 是**每个文件**的帧数，不是整个采集的秒数。该 SDK 原生命令不包含视频录像工具的磁盘余量保护，长采集前先检查 `df -h /data`，不要录到磁盘满。不要给两个接收程序同时连接同一雷达做重复采集。

离线取其中一帧转换为 PCD，不再占用雷达网口：

```bash
seyond-get-pcd --inno-pc-filename "$capture_dir/raw.inno_pc" \
  --frame-start 0 --frame-number 1 --output-filename "$capture_dir/frame0.pcd"
```

可以在电脑上用点云软件查看 PCD，或在 ROS 中回放原始文件。PCD/CSV 适合导出检查；连续保存推荐原始 `.inno_pc`，XYZ 点云、CSV 和 ROS bag 通常比网络原始码流大。若采用 tcpdump/PCAP，必须先建立雷达数据输出，抓包本身不能代替 SDK 启动采集；紧凑点云离线解码还需匹配角度表。

## ROS 可选方式

104 当前是 Ubuntu 22.04，没有安装 ROS。先用上面的轻量 SDK 验证数据，不必为保存点云先装一整套 ROS。需要 SLAM、点云实时显示或与其他传感器统一记录时，再安装兼容系统的 ROS 及 [厂家 ROS 驱动](https://github.com/Seyond-Inc/seyond_ros_driver)。

用户手册 ROS1 示例（需其对应驱动包，不适用于当前未装 ROS 的 104）：

```bash
roslaunch innovusion_pointcloud innovusion_points.launch device_ip:=172.168.1.10 udp_port:=8010
rosbag record /iv_points -o /data/uav-lidar/e1x
```

手册 ROS2 示例使用 Foxy 和 `innovusion` 包。Ubuntu 22.04 应选择匹配版本（例如 Humble），并按所选厂家驱动核实 launch 名和实际话题；现代 `seyond_ros_driver` 的包名/话题可能不同，不能直接套旧手册命令。

## 带宽和存储

手册标称单回波约 **54 Mbps = 6.75 MB/s ≈ 24.3 GB/小时**，模式、回波数及封装会影响实际数值。这对千兆板间以太网通常占比不大，但须接线后测实际吞吐、丢包和 CPU。视频七路标称合计约 9 GB/小时，与雷达一起保存约 33 GB/小时；512 GB 数据盘需要规划剩余空间和导出频率。

点云仅在载荷网络传到 104 并写本地 SSD，不经 MK22，因而不会额外占 MK22 的无线带宽。后续地面站如需显示，可以发送降采样点云或渲染画面；先不要将全量原始点云经过 MK22。未做视频与雷达同时长时间采集的性能验收。

## 其他同型号板准备 SDK

本部署包不自动安装雷达或变更雷达网络。104 已准备上述工具；其他板可下载厂家源码、使用 ARM64 GCC/CMake 原生编译。以下固定到同一提交，在单独数据盘目录执行：

```bash
git clone https://github.com/Seyond-Inc/inno-lidar-sdk.git
cd inno-lidar-sdk
git checkout 73a24e98c283391b890cfd106e5cc13e9c9d7f5b
# 此提交公共 CMake 的编译标志为 x86；ARM64 副本替换这一行：
python3 - <<'PY'
from pathlib import Path
p = Path('build/inno_lidar_base.cmake')
s = p.read_text()
assert ' -mno-ms-bitfields -w -mcrc32' in s
p.write_text(s.replace(' -mno-ms-bitfields -w -mcrc32', ' -w -march=armv8-a+crc'))
PY
cmake -S . -B build-arm64 -DARCH_TAG=-arm -DCMAKE_BUILD_TYPE=Release
# 此提交按静态库文件名链接 get_pcd，需要先构建三项库。
cmake --build build-arm64 --target innolidarutils -j2
cmake --build build-arm64 --target innolidarsdkcommon -j2
cmake --build build-arm64 --target innolidarsdkclient -j2
cmake --build build-arm64 --target get_pcd -j2
build-arm64/apps/tools/get_pcd/get_pcd --help
```

复制二进制到其他兼容板时一并保留厂家的 `LICENSE`；不同 CPU/系统环境先重新构建和检查运行依赖。
