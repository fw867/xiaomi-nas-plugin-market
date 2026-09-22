# 内网穿透（fwclient）

小米智能存储社区插件：管理 **fwclient** 内网穿透客户端。

**不是 fwserver / fwclient 官方产品。** 随包分发 `fwclient-linux-arm64`。

## 功能

- 配置 **服务器域名**、**访问令牌**（可选跳过 TLS）
- 优先后台守护运行：`fwclient -s <域名> -t <令牌> -d`
- 显示当前客户端版本（`-v`）
- **检查升级**（`-u`）
- 启停服务；日志摘要来自 `fwclient.log`

## 使用

1. 在网关管理端创建访问令牌（`tk_…`）
2. 打开本插件，填写服务器域名与令牌后「保存并启动」
3. 到管理端「内网穿透」配置穿透规则
4. 需要升级时点「检查升级」

## 数据目录

| 文件 | 说明 |
| --- | --- |
| `fwclient` | 可执行文件（初始化时从包内拷入，升级会替换） |
| `fwclient.id` | 设备唯一标识 |
| `fwclient.log` | 运行日志 |
| `settings.json` | 服务器/令牌（0600，不回显明文令牌） |

## 本地预览

```bash
cd projects/xiaomi-fwclient-plugin
python -m unittest discover -s tests -v
python server.py --dev
```
