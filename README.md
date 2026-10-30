<div align="center">

# OCI IP Guard

**一个 Docker 服务，守住你的多 IP 入口。**

多地区巡检 · 本地记录 · 故障告警 · DNS 自动切换

[![Tests](https://github.com/arrenxxxxx/oci-ip-guard/actions/workflows/test.yml/badge.svg)](https://github.com/arrenxxxxx/oci-ip-guard/actions/workflows/test.yml)
[![Images](https://github.com/arrenxxxxx/oci-ip-guard/actions/workflows/publish.yml/badge.svg)](https://github.com/arrenxxxxx/oci-ip-guard/actions/workflows/publish.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

[快速开始](#快速开始) · [配置指南](docs/quickstart.md) · [监控与运维](docs/operations.md) · [镜像发布](docs/publishing.md)

</div>

---

在 OCI 主机上，分别绑定每个公网入口对应的本机私网地址，探测国内多个地区的 TCP 连通性。当当前入口持续异常、备用入口稳定健康时，自动更新 Cloudflare 的 DNS-only A 记录。

**一个容器即可独立运行。** 巡检结果与事件保存在本地；通知和监控看板按需接入。安装时必须明确选择「真实自动切换」或「模拟观察模式」，没有静默默认模式。模拟模式故障时不会修改 DNS；真实模式必须通过检查并再次确认。

![OCI IP Guard architecture](docs/assets/architecture.svg)

## 如何工作

1. **巡检** — 每分钟检测各入口 IP 到多个地区的连通性。
2. **记录** — 状态、延迟和故障事件保存在本地 SQLite，通过命令即可查看。
3. **告警** — 连续失败触发告警；按需配置 Telegram、飞书 Webhook，或同时启用。
4. **切换** — 确认备用入口健康、冷却结束后更新 Cloudflare DNS；默认不自动切回。

通知异步发送并独立重试，不要求通知送达后才能切换。网络对照失败、映射未知、数据过期或全部入口异常时，只记录和告警。

**监控看板是可选项。** 不安装 Prometheus / Grafana 也能完成巡检、记录、告警和切换；需要图表时，再让 Prometheus 采集指标并导入 Grafana 看板。

## 快速开始

准备 Linux、Docker Engine、Compose v2.20+、Python 3.10+，以及已配置好的多个公网 / 私网 IP 映射。

```sh
git clone https://github.com/arrenxxxxx/oci-ip-guard.git
cd oci-ip-guard
./ip-guard setup
```

向导填写域名、候选 IP 和对应私网 IP；Telegram / 飞书通知可选填。真实模式需要指定 Zone 的 **DNS Edit** Token，并通过只读权限、记录和探测检查；无法证明写权限时不会宣称自动切换可用。密钥隐藏输入，保存到权限 `0600` 的私有 `.env`。

本次模式修复请先使用**当前源码构建**（镜像名称留空），或选择已明确包含本修复的版本 / digest。旧版 `v0.1.0` 不包含新的模式校验，不能仅换 `.env` 就获得新行为；本次提交不会自动发布镜像。

更多发布方式见[镜像文档](docs/publishing.md)。

```sh
./ip-guard status              # 运行状态
./ip-guard check               # 实测各 IP 的区域连通性
./ip-guard events              # 查看本地事件记录
./ip-guard test-notification   # 验证通知
```

请以 `./ip-guard status` 中的实际自动切换状态为准，不能把容器健康或普通故障告警当作切换成功。手动安装与旧配置升级见[模式选择和迁移](docs/quickstart.md#4-模式选择与旧配置迁移)。升级不会把旧模拟配置自动改为真实写入。

## 默认策略

| 项目 | 行为 |
| :--- | :--- |
| 探测 | 每 60 秒；北京、杭州、深圳 TCP 443，每目标一票 |
| 当前入口故障 | 成功比例 < 50%，连续 3 轮 |
| 可用备用入口 | 成功比例 ≥ 80%，连续 3 轮 |
| 中间状态 | 50% ≤ 成功比例 < 80%，重置连续次数 |
| 切换 | 15 分钟冷却；不自动切回；写入后读回核验 |
| 可选看板 | 区域成功率、延迟、当前 IP、故障、切换与本地探测心跳 |

## 适用范围

此模式检测**指定源 IP 到国内目标的出站 TCP 连通性**，不等于 VPN 入站端口或协议验收。默认三个目标属于同一家云厂商，不代表三大运营商；可自行更换区域目标。

项目不配置云端 IP、Netplan、VPN 或默认路由。整机宕机需外部监控；DNS 缓存可能延迟切换生效，已有连接不会随之迁移。

---

[MIT License](LICENSE) · 欢迎通过 [Issues](https://github.com/arrenxxxxx/oci-ip-guard/issues) 反馈可复现的问题。
