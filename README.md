# 思源笔记 · 懒猫微服自动更新适配器

本仓库维护懒猫应用商店的 `cloud.lazycat.app.siyuan`，以 [SiYuan 官方仓库](https://github.com/siyuan-note/siyuan) 的稳定 Git 标签和同名 Docker Hub 镜像为上游。工作流调用 [wcaqrl/lazycat-action@v1](https://github.com/wcaqrl/lazycat-action)。

## 工作流程

1. 选择形如 `v3.8.10` 的最新稳定标签，读取对应 GitHub Release 说明。预发布标签和 `latest` 不参与版本选择。
2. 解析 `docker.io/b3log/siyuan:<tag>` 的 `linux/amd64` 摘要。共享 Action 使用开发者 PAT 转存镜像到 `registry.lazycat.cloud`。
3. 打包前检查源码镜像摘要、官方副本的文件系统和运行配置，并运行旧包到候选镜像的升级测试。旧工作空间、访问码、剪藏令牌、文档、附件和重启后的内容都必须通过验证。
4. 构建 LPK，将候选版本和审核状态写入 `.lazycat-action.lock.yml`。共享工作流处理官方商店送审和 GitHub Release 的 LPK 下载附件。重试被拒版本时，LPK 版本可以和上游镜像版本不同。

推送到 `main` 触发检查。GitHub Actions 计划任务约每六小时检查并处理有更新的版本。手动运行 **Update SiYuan for LazyCat** 时，`dry-run` 默认勾选，可填写 `test-tag` 额外执行容器升级测试。取消勾选后，符合发布条件的候选会转存、验证、打包并送审。仓库需配置 `LZC_API_TOKEN` Repository secret，令牌所属开发者需有本应用权限。

## 升级兼容

沿用旧包的包名、`siyuan` 子域名、多实例支持、`authcode` 安装参数、UID/GID `1001:1002` 和 `/lzcapp/var/:/siyuan/workspace` 持久化挂载。访问码由思源原生的 `SIYUAN_ACCESS_AUTH_CODE` 环境变量读取。入口代理保留 Origin 和 Referer，供新版思源按请求来源验证会话。该适配器生成的 LPK v2 要求 LzcOS 1.5.0 或更新版本。 [懒猫部署参数](https://developer.lazycat.cloud/spec/deploy-params.html) · [LPK v2 格式](https://developer.lazycat.cloud/spec/lpk-format.html)

## 本地验证

使用 Python 3.12、可访问 Docker Hub 和懒猫官方 CDN 的网络，以及 Linux amd64 Docker。

```bash
python3 -m venv .lazycat-action/venv
source .lazycat-action/venv/bin/activate
python -m pip install -r requirements-test.txt
python3 -B -m unittest discover -s tests -v
python3 -B scripts/test_upgrade.py --tag v3.8.10
lzc-cli project lint
```

容器测试从旧商店包的固定镜像摘要创建合成数据，完成后清理测试容器与临时卷，Docker 保留已校验的镜像缓存。以原始基线配置直接执行构建会触发交付检查，需先由共享 Action 写入待发布版本的上游摘要与官方镜像引用。

图标取自 [SiYuan v3.8.10 源码](https://github.com/siyuan-note/siyuan/blob/v3.8.10/app/electron/static/icon.png)。
