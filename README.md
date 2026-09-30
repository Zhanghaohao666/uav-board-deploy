# RK3588 下视主控板一键部署

用于与当前下视主控相同的 RK3588 ARM64 BSP（现场参考 Ubuntu 22.04），安装 MIPI/D455 彩色采集、RTSP 转发、控制 TCP 转发、三模式切换菜单和开机自启。**本包安装在下视主控板，不安装在算法板或 RK3576 云台上**。算法板/云台继续使用其已有采集程序。

地面站源码和历史版本：[uav-ground-station](https://github.com/Zhanghaohao666/uav-ground-station)。

## 本地一条命令安装

将本目录完整复制到新板，进入目录执行：

```bash
sudo bash install.sh
```

依次选择：算法板单网口 / 云台单网口 / 双网口并行、板间网口、MK22 网口（双网口模式另选云台扩展口）、本板 MK22 地址。D455 彩色设备按本机设备和格式识别，有多个候选时要求选择；不会使用旧板序列号。默认 MIPI 接口适用于当前同型号 BSP；其他相机接口请用 `--board-camera` 明确指定。

安装程序检查 RK3588、硬件编码驱动、动态库、GStreamer 插件、相机格式、网络冲突及相机/端口占用；然后复制文件、启用开机服务、立即启动并验证画面。依赖只安装缺少的用户态软件包，不运行系统整体升级、不安装/替换内核或 BSP。

**同一 MK22/以太网网络上同时使用多块主控，必须分配不同 IP。** 例如第一块 `.36`，第二块 `.37`；地面站的视频方案按钮预设是 `.36`。`.37` 的板需在地面站各窗口改对应主机地址。两套完全隔离的网络可以各用 `.36`。

明确参数、无需逐项选择的例子（网卡名仅是例子，先用 `ip -br addr` 查看本板）：

```bash
sudo bash install.sh --mode algorithm --payload-iface eth0 --mk22-iface eth1 --mk22-address 192.168.2.36/24 --user dev --yes
sudo bash install.sh --mode gimbal --payload-iface eth0 --mk22-iface eth1 --mk22-address 192.168.2.37/24 --user dev --yes
sudo bash install.sh --mode dual --payload-iface eth0 --mk22-iface eth1 --extension-iface eth2 --mk22-address 192.168.2.36/24 --user dev --yes
```

未接 D455 时加 `--without-d455`；要安装但下次开机再启动，加 `--no-start`。没有相机驱动/MPP/RGA 的通用系统镜像无法靠此脚本补齐，须先刷同型号厂家 BSP、接好相机。

## GitHub 一条命令入口

公开仓库：https://github.com/Zhanghaohao666/uav-board-deploy 。以下命令固定到 `v1.0.0`，避免主分支后续变动影响当前部署。

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Zhanghaohao666/uav-board-deploy/v1.0.0/bootstrap.sh) --repo Zhanghaohao666/uav-board-deploy --ref v1.0.0
```

如果板卡访问 `raw.githubusercontent.com` 超时，使用下面的 GitHub API 入口（当前主控已通过此入口下载并完成只读 doctor，未重启服务）：

```bash
bash <(curl -fsSL -H 'Accept: application/vnd.github.raw+json' 'https://api.github.com/repos/Zhanghaohao666/uav-board-deploy/contents/bootstrap.sh?ref=v1.0.0') --repo Zhanghaohao666/uav-board-deploy --ref v1.0.0
```

也可在命令后添加上文的 `--mode ... --mk22-iface ... --yes` 参数。入口下载完整仓库、检查路径和 SHA-256 清单，再执行安装程序；保留终端以供选择模式，不使用 `curl | bash`。

私有仓库请使用自己的 GitHub SSH/凭据下载，不将 token 写进命令或本仓库：

```bash
git clone git@github.com:Zhanghaohao666/uav-board-deploy.git uav-board-deploy && cd uav-board-deploy && sudo bash install.sh
```

## 安装后切换与开机自启

```bash
uav-switch
systemctl status uav-switch-boot.service
python3 /opt/uav-switch/manager.py status
python3 /opt/uav-switch/manager.py verify
```

菜单 1 算法板、2 云台、3 双网口并行、4 状态和流地址、5 验证出图、6 保存当前模式为开机默认、0 退出。1～3 临时切换；选择 6 后下次开机使用当前模式。单网口部署未登记云台扩展口时，不支持直接切到双网口；须为 `/opt/uav-switch/config.json` 的 `extension_iface` 配置实际扩展网口后再切换。

`uav-switch-boot.service` 在 network-online 后加载 `/opt/uav-switch/config.json` 的 default_mode，设置本机地址并启动 `uav-switch@*.service`。工作进程异常退出由 systemd 重启。网络或硬件稍晚上线时会重试。当前模式在 `/var/lib/uav-switch/active.json`，重启按保存的默认模式重新应用。

| 模式 | 板间接线 | 视频数（含 D455 彩色） |
|---|---|---|
| algorithm | 算法板接 eth0 | 5 |
| gimbal | 云台接 eth0 | 4 |
| dual | 算法板接 eth0，云台接登记的扩展口 | 7 |

单网口方案的 eth0 同时配 `192.168.10.1/24`、`192.168.144.1/24`，只启动选中载荷的主控侧转发；双网口分别配置这两个地址。MK22 使用配置指定的独立接口。工具不改 Wi-Fi、不清空路由、不更改默认网关。BSP 自带网络配置不得在这些接口上配置冲突网段。

## 视频与载荷前提

以下以 MK22 地址 `.36` 为例，其他板更换主机地址即可：

| 视频 | 地址 |
|---|---|
| 下视 MIPI | `rtsp://192.168.2.36:8554/board` |
| D455i 彩色 | `rtsp://192.168.2.36:8554/rgb` |
| 前视算法 | `rtsp://192.168.2.36:8554/algorithm` |
| 前视原始预览 | `rtsp://192.168.2.36:8554/preview` |
| 算法板红外 | `rtsp://192.168.2.36:8554/infrared` |
| 云台可见光 | `rtsp://192.168.2.36:8555/gimbal` |
| 云台红外 | `rtsp://192.168.2.36:8556/gimbal_ir` |

算法板 `192.168.10.2` 需保持已有程序向主控 `192.168.10.1` 的 UDP 5602/5603/5604 推送 H.264 RTP。云台 `192.168.144.108` 需提供 `rtsp://192.168.144.108:554/live/0` 和 `/live/1`。本包只安装主控，不会自动给一块全新的算法板或云台部署算法程序。

主控 TCP 9001 转发至算法板 `192.168.10.2:9000`；9000 转发至云台 `192.168.144.108:9000`。地面站视频方案按钮只改拉流地址，控制目标需按用途单独选择。

## 预检查、重复安装与失败处理

```bash
# 有依赖的板子上生成配置，随后只读检查：
python3 install.py configure --mode algorithm --payload-iface eth0 --mk22-iface eth1 --mk22-address 192.168.2.36/24 --user dev --output board-config.json --yes
python3 install.py doctor --config board-config.json
sudo bash install.sh --config board-config.json
```

`doctor` 不安装依赖，不修改网络或服务。相同安装包、相同配置的再次安装会跳过，不重启服务。已有旧版本、旧开机服务或未知占用会拒绝安装，并保留原状；本包面向新板，升级现有板需要另行迁移，不能强行覆盖。

安装前状态保存在 `/var/backups/uav-switch/时间/`。安装命令失败会尝试停止本次服务、删除本次文件、恢复本次修改的受管 IP。已安装的 apt 依赖保留。设备未出图会明确报告，但不撤销已经完成的服务安装；服务 active 不代表视频验证通过。

自动配置不能代替物理接线、配置载荷板、刷 BSP、设置 MK22 或解决重复地址。网络断开时 ARP 查重不能判断断开的另一台主控，所以仍需规划唯一地址。

## 验证范围

- 29 项 Python 测试通过，覆盖三模式网络/服务规划、完整安装流程的隔离模拟、失败回退、接口选择、重复安装保护、ffmpeg 版本差异。
- 在现有 RK3588 上运行只读 doctor 通过；缺少的 arping 仅解压到临时目录后加入检查 PATH，未安装到当前系统。未覆盖正在使用的部署，未重启现有服务。
- 没有新板地址，因此完整新板安装及断电自启验收尚待在新板执行。
- 不含 SSH 密码、GitHub token、现场日志或个人密钥。

维护者修改文件后，运行 `python3 make_release.py` 重建 SHA-256 清单及本地压缩包，再提交 GitHub。不要提交自动生成的 `board-config.json`。
