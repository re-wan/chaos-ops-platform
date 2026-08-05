# ChaosOps — Self-hosted AI Operations Platform

> Open-source free edition. Monitor your servers, get alerted, and let AI help you fix problems — all on your own infrastructure.

[![License](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)

## What is ChaosOps?

ChaosOps is a self-hosted server monitoring and operations platform. It watches your servers, alerts you when things go wrong, and helps you fix them — with AI that runs **locally on your own servers** (your data never leaves).

**Free Edition (this repo)** includes:
- 📊 **Monitoring**: CPU, memory, disk, network, HTTP probes, processes, load, disk I/O, systemd services, TCP connections
- 🚨 **Alerting**: Rule engine (JSON DSL / PromQL), pending/firing/resolved states, silences, inhibitions, deduplication
- 📋 **Incidents**: Full lifecycle with timeline, auto-aggregation from alerts
- 📧 **Notifications**: Email, DingTalk, WeCom, Lark, Slack, generic webhooks (Chinese & English templates)
- 🖥️ **Node Management**: One-command agent install (Linux/Windows/ARM), auto-updates with rollback
- 👥 **Multi-user RBAC**: Admin and viewer roles
- 🌐 **Bilingual UI**: Chinese and English

**Professional / Enterprise editions** (available at [chaosm.io](https://chaosm.io)) add AI root-cause analysis, self-healing execution, remote batch execution, open API, and more.

## Quick Start

### Linux (Ubuntu 20.04+ / Debian 11+ / CentOS 7+)

```bash
tar -xzf chaosops-free-v1.0.0-linux-x86_64.tar.gz
cd chaosops-free-v1.0.0-linux-x86_64
sudo ./install-server.sh
```

Open `http://your-server-ip:8000` and log in with the generated admin credentials (saved to `server/.env`).

### Windows

Run `bin\chaosops-installer.exe` as Administrator.

## Architecture

Single Server + multiple Agents:
- **Server**: FastAPI backend, alert engine, incident center, notifications, web console (Vue 3)
- **Agent**: Deployed on monitored hosts, collects metrics and executes healing actions

## Downloads

See [Releases](https://github.com/re-wan/chaos-ops-platform/releases) for the latest packages (Linux x86_64 / ARM64).

## License

AGPL-3.0 — see [LICENSE](LICENSE).

## Links

- **Website**: [chaosm.io](https://chaosm.io)
- **Professional Edition**: $1,399 USD (perpetual license, 90-day free updates)

---

## 中文简介

ChaosOps 是**自托管的 AI 运维平台**——监控你的服务器、出问题自动告警、AI 帮你分析修复，数据全在你自己手里。

**免费版（本仓库）**包含：
- 📊 监控：CPU / 内存 / 磁盘 / 网络 / HTTP 探活 / 进程 / 负载 / 磁盘 IO / 服务状态 / 连接数
- 🚨 告警：规则引擎（JSON DSL / PromQL）、状态机、静默、抑制、去重
- 📋 事件：全生命周期 + 时间线，告警自动聚合
- 📧 通知：邮件、钉钉、企业微信、飞书、Slack、通用 Webhook（中英双语模板）
- 🖥️ 节点管理：一键安装 Agent（Linux / Windows / ARM），自动更新+失败回滚
- 👥 多用户权限：管理员 / 查看者
- 🌐 中英双语界面

**专业版 / 企业版**（见 [chaosm.io](https://chaosm.io)）增加 AI 根因分析、自愈执行、远程批量执行、开放 API 等。

### 快速开始

**Linux**（Ubuntu 20.04+ / Debian 11+ / CentOS 7+）：

```bash
tar -xzf chaosops-free-v1.0.0-linux-x86_64.tar.gz
cd chaosops-free-v1.0.0-linux-x86_64
sudo ./install-server.sh
```

打开 `http://你的服务器IP:8000`，用安装时生成的管理员账号登录（密码保存在 `server/.env`）。

**Windows**：以管理员身份运行 `bin\chaosops-installer.exe`。

### 链接

- **官网**：[chaosm.io](https://chaosm.io)
- **专业版**：$1,399 美元买断（含 90 天免费更新）

