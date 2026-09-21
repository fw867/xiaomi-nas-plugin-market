# DPanel 容器管理（小米智能存储社区插件）

在小米智能存储客户端里启停 **DPanel** 轻量 Docker 管理面板。

**不是 DPanel 或小米的官方插件。** 上游：[DPanel](https://dpanel.cc/) / [GitHub](https://github.com/donknap/dpanel)

## 功能

- 初始化时可选择配置目录（挂载为 `/dpanel`），留空则用插件私有目录
- 启停 DPanel Lite 容器
- 局域网面板入口：`http://<NAS-IP>:8807/dpanel/ui`
- 首次打开面板后请在 DPanel 内设置管理员账号密码

## 权限说明（重要）

DPanel 用于管理 Docker，容器**必须**挂载宿主机 `/var/run/docker.sock`。  
这意味着面板内操作拥有 Docker 级权限。请仅在受信任网络使用，并妥善保管面板账号。

插件页本身只负责启停本容器，不代理 DPanel 的 Docker 操作接口。

## 镜像与端口

| 项 | 值 |
| --- | --- |
| 镜像 | `dpanel/dpanel:lite@sha256:befa4221aeebbeac9148cceca06f8b474f412b2de68b5f3968a68e04a0e632c1` |
| 面板端口 | 宿主机 `8807` → 容器 `8080` |
| 插件服务端口 | `18160`（仅回环 + 小米客户端证书） |
| 容器名 | `xiaomi-plugin-dpanel` |

选择 **Lite** 版：不占用 80/443，不需要域名转发/证书功能时更适合 NAS。

## 隔离与生命周期

- 不使用 privileged、不使用 host 网络
- 只挂载 docker.sock 与配置目录
- 资源上限：512 MiB 内存、1.5 CPU
- 停止/卸载插件只停止容器，配置保留

## 本地预览

```bash
cd projects/xiaomi-dpanel-plugin
python -m unittest discover -s tests -v
python server.py --dev
```

打开 `http://127.0.0.1:18160/`。预览模式不会操作 Docker。

## 上游

- [DPanel 文档 · Docker 安装](https://dpanel.cc/install/docker)
- 插件图标来自 DPanel CDN（`dpanel-logo-small.png`），仅用于识别兼容软件
