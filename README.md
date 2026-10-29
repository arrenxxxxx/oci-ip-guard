<div align="center">

# OCI IP Guard

**一个 Docker 服务，守住你的多 IP 入口。**

多地区探测 · Cloudflare DNS 故障切换 · 飞书 / Telegram · Prometheus / Grafana

[![Tests](https://github.com/arrenxxxxx/oci-ip-guard/actions/workflows/test.yml/badge.svg)](https://github.com/arrenxxxxx/oci-ip-guard/actions/workflows/test.yml)
[![Images](https://github.com/arrenxxxxx/oci-ip-guard/actions/workflows/publish.yml/badge.svg)](https://github.com/arrenxxxxx/oci-ip-guard/actions/workflows/publish.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

[快速开始](#快速开始) · [配置指南](docs/quickstart.md) · [监控与运维](docs/operations.md) · [镜像发布](docs/publishing.md)

</div>

---

在 OCI 主机上，分别绑定每个公网入口对应的本机私网地址，探测国内多个地区的 TCP 连通性。当当前入口持续异常、备用入口稳定健康时，自动更新 Cloudflare 的 DNS-only A 记录。

**只部署在 OCI 上。** 无需额外 NAS 或远端探针；默认 Dry Run，先观察、再启用切换。

![OCI IP Guard architecture](docs/assets/architecture.svg)

## 为什么用它

- **少部署** — 一个容器，交互式安装，复用已有 Prometheus / Grafana。
- **稳切换** — 区域比例、连续判定、15 分钟冷却；默认不自动切回。
- **可追溯** — SQLite 持久化状态与事件，双渠道通知独立重试。
- **有保护** — 网络对照失败、映射未知、数据过期或全部入口异常时只告警。

## 快速开始

准备 Linux、Docker Engine、Compose v2.20+、Python 3.10+，以及已配置好的多个公网 / 私网 IP 映射。

```sh
git clone https://github.com/arrenxxxxx/oci-ip-guard.git
cd oci-ip-guard
./ip-guard setup
```

向导填写域名、候选 IP、对应私网 IP 和通知凭据。Cloudflare Token 仅需指定 Zone 的 **DNS Edit** 权限；密钥隐藏输入，保存到权限 `0600` 的私有 `.env`。

镜像支持 **amd64 / arm64**，固定版本及拉取方式见[镜像发布文档](docs/publishing.md)。向导中镜像留空会从源码构建。

```sh
./ip-guard status              # 运行状态
./ip-guard check               # 实测各 IP 的区域连通性
./ip-guard test-notification   # 验证通知
```

默认不会修改 DNS。确认稳定后，按[启用步骤](docs/quickstart.md#4-观察后再启用)开启自动切换。

## 默认策略

| 项目 | 行为 |
| :--- | :--- |
| 探测 | 每 60 秒；北京、杭州、深圳 TCP 443，每目标一票 |
| 当前入口故障 | 成功比例 < 50%，连续 3 轮 |
| 可用备用入口 | 成功比例 ≥ 80%，连续 3 轮 |
| 中间状态 | 50% ≤ 成功比例 < 80%，重置连续次数 |
| 切换 | 15 分钟冷却；不自动切回；写入后读回核验 |
| 监控 | 区域成功率、延迟、当前 IP、故障、切换与本地探测心跳 |

## 适用范围

此模式检测**指定源 IP 到国内目标的出站 TCP 连通性**，不等于 VPN 入站端口或协议验收。默认三个目标属于同一家云厂商，不代表三大运营商；可自行更换区域目标。

项目不配置云端 IP、Netplan、VPN 或默认路由。整机宕机需外部监控；DNS 缓存可能延迟切换生效，已有连接不会随之迁移。

---

[MIT License](LICENSE) · 欢迎通过 [Issues](https://github.com/arrenxxxxx/oci-ip-guard/issues) 反馈可复现的问题。
