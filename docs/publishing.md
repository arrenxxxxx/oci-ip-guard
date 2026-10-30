# 镜像与发布

## 使用镜像

镜像仓库：`ghcr.io/arrenxxxxx/oci-ip-guard-controller`，支持 `linux/amd64`、`linux/arm64`。

本次模式修复不会自动发布镜像。请先从当前源码构建，或在 [Actions](https://github.com/arrenxxxxx/oci-ip-guard/actions/workflows/publish.yml) 确认指定版本确实包含该修复后拉取。下面 `VERSION` 必须替换为已验证的真实版本，不要把旧版 `v0.1.0` 当作包含修复的镜像：

```sh
docker pull ghcr.io/arrenxxxxx/oci-ip-guard-controller:VERSION
```

安装向导询问镜像时填写上述完整名称；留空从源码构建。已有部署可设置 `deploy/controller/.env` 中的 `IP_GUARD_IMAGE`，再运行 `./ip-guard start`。生产环境建议固定版本，或使用 Actions 输出的 `@sha256:...` digest。不发布漂移的 `latest` 标签。

## GitHub Actions → GHCR

普通 push / PR 运行测试。推送 `v*` 标签时先测试，再构建并发布多架构镜像，附带 SBOM、provenance、源码版本标签和 digest。也可以手动运行 `publish-images`，选择 `ghcr` 并输入版本号。

首次启用需在仓库 Settings → Secrets and variables → Actions → Variables 设置：

```text
ENABLE_IMAGE_PUBLISH=true
```

这是 fork 的发布保护开关。GHCR 使用短期 `GITHUB_TOKEN` 和 `packages: write`，无需保存个人 PAT。

```sh
git tag v0.1.0
git push origin v0.1.0
```

首次发布后，确认 GitHub Packages 的可见性为 Public，并验证匿名拉取；公开源码不代表 package 自动公开。若同名 package 已存在，需授权当前仓库写入。已发布版本不覆盖，后续使用新版本号。

## 可选：Docker Hub

在 Docker Hub 创建 `oci-ip-guard-controller` 仓库，然后配置 GitHub 仓库：

| 类型 | 名称 | 值 |
| --- | --- | --- |
| Variable | `ENABLE_IMAGE_PUBLISH` | `true` |
| Variable | `DOCKERHUB_USERNAME` | Docker Hub 用户名 |
| Secret | `DOCKERHUB_TOKEN` | 具备目标仓库推送权限的 Access Token |

手动运行 `publish-images`，选择 `dockerhub` 并输入版本。镜像为 `<用户名>/oci-ip-guard-controller:<版本>`；Git 标签默认只发布 GHCR。

## 本地验证

```sh
python3 -m unittest discover -s tests -v
docker build --target controller -t ip-guard-controller:local .
```

业务凭据只留在部署机器的 `.env`。Docker 构建上下文仅包含 Dockerfile 和 `guard/*.py`。构建成功不能替代真实网络、通知和 DNS 切换验收，部署必须明确选择运行模式，真实模式必须完成只读校验和确认；旧模拟配置升级后仍禁止写入。
