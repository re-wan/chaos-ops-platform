#!/bin/bash
# ChaosOps Agent Linux 卸载脚本
# 用法：sudo bash uninstall.sh
#
# 流程：停止并移除 systemd 服务 -> 删除安装目录 -> 删除系统用户

set -euo pipefail

AGENT_DIR="${AGENT_DIR:-/opt/chaosops-agent}"
SERVICE_NAME="${SERVICE_NAME:-chaosops-agent}"
AGENT_USER="chaosops-agent"

log_info() { echo "[INFO] $*"; }
log_warn() { echo "[WARN] $*"; }
log_error() { echo "[ERROR] $*" >&2; }

# 安全的 rm -rf：拒绝空 / 根 / 当前目录，防止变量为空时误删系统根。
safe_rm_rf() {
    local target="${1:-}"
    if [[ -z "${target}" || "${target}" == "/" || "${target}" == "." || "${target}" == ".." ]]; then
        log_error "拒绝 rm -rf：危险路径 '${target}'"
        return 1
    fi
    rm -rf "${target}"
}

# --------------- root 权限检查 ---------------
if [[ "$(id -u)" -ne 0 ]]; then
    log_error "卸载需要 root 权限，请使用 sudo 运行"
    echo "示例：sudo bash uninstall.sh" >&2
    exit 1
fi

log_info "ChaosOps Agent 卸载程序"

# --------------- 停止并移除 systemd 服务 ---------------
if command -v systemctl >/dev/null 2>&1; then
    if systemctl list-unit-files "${SERVICE_NAME}.service" >/dev/null 2>&1; then
        systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
        systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
        log_info "已停止并禁用服务 ${SERVICE_NAME}"
    fi
    rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
    systemctl daemon-reload
    log_info "已移除 systemd 单元"
else
    log_warn "未找到 systemctl，跳过服务移除"
fi

# --------------- 删除安装目录 ---------------
if [[ -d "${AGENT_DIR}" ]]; then
    safe_rm_rf "${AGENT_DIR}"
    log_info "已删除安装目录 ${AGENT_DIR}"
else
    log_info "安装目录 ${AGENT_DIR} 不存在，跳过"
fi

# --------------- 删除系统用户 ---------------
if id "${AGENT_USER}" >/dev/null 2>&1; then
    userdel "${AGENT_USER}" 2>/dev/null || log_warn "删除用户 ${AGENT_USER} 失败，可手动执行 userdel ${AGENT_USER}"
    log_info "已删除系统用户 ${AGENT_USER}"
fi

log_info "ChaosOps Agent 卸载完成"
log_info "提示：Server 控制台的节点记录仍需在「节点列表」中手动删除"
