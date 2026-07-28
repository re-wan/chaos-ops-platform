# ChaosOps — Self-hosted AI Operations Platform

> Open-source free edition. Monitor your servers, get alerted, and let AI help you fix problems — all on your own infrastructure.

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
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
tar -xzf chaosops-free-v0.1.0-linux-x86_64.tar.gz
cd chaosops-free-v0.1.0-linux-x86_64
sudo ./install-server.sh
```

Open `http://your-server-ip:8000` and log in with the generated admin credentials (saved to `server/.env`).

### Windows

Run `bin\chaosops-installer.exe` as Administrator.

### Docker

```bash
docker load -i chaosops-free-docker-v0.1.0.tar.gz
docker run -d -p 8000:8000 -v chaosops-data:/opt/chaosops/data chaosops-free:v0.1.0
```

## Architecture

Single Server + multiple Agents:
- **Server**: FastAPI backend, alert engine, incident center, notifications, web console (Vue 3)
- **Agent**: Deployed on monitored hosts, collects metrics and executes healing actions

## Documentation

- [Quick Start Guide](docs/QUICKSTART.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)

## License

MIT — see [LICENSE](LICENSE).

## Links

- **Website**: [chaosm.io](https://chaosm.io)
- **Professional Edition**: ¥9,999 CNY / $1,399 USD (perpetual license, 90-day free updates)
