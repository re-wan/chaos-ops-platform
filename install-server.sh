#!/bin/bash
# ChaosOps Server 一键安装脚本（V2 薄包装）
# 实际安装逻辑由 bin/chaosops-installer 实现。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"
exec "${SCRIPT_DIR}/bin/chaosops-installer" "$@"
