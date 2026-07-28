#!/bin/bash
# ChaosOps Agent Linux 一键安装脚本（curl 在线安装版）
# 用法：curl -fsSL <server>/api/v1/agents/install.sh | SERVER_URL=<url> INSTALL_KEY=ik_xxxx sudo bash
#
# 流程：环境检查 -> 创建系统用户 -> 下载分发包（install_key 鉴权） -> sha256 校验
#       -> 防 zip-slip 解压 -> venv 离线安装依赖 -> 注册（消耗 install_key）
#       -> 写 config.json -> 安装并启动 systemd 服务

set -euo pipefail

# --------------- 配置 ---------------
AGENT_DIR="${AGENT_DIR:-/opt/chaosops-agent}"
SERVICE_NAME="${SERVICE_NAME:-chaosops-agent}"
AGENT_USER="chaosops-agent"
CONFIG_FILE="${AGENT_DIR}/config.json"
PACKAGE_FILE="/tmp/chaosops-agent.tar.gz"
PACKAGE_HEADERS="/tmp/chaosops-agent.headers"
STAGING_DIR=""

log_info() { echo "[INFO] $*"; }
log_warn() { echo "[WARN] $*"; }
log_error() { echo "[ERROR] $*" >&2; }

# 安全的 rm -rf：拒绝空 / 根 / 当前目录，防止变量为空时误删系统根。
safe_rm_rf() {
    local target="${1:-}"
    if [[ -z "${target}" ]]; then
        log_error "拒绝 rm -rf：目标为空"
        return 1
    fi
    if [[ "${target}" == "/" || "${target}" == "." || "${target}" == ".." ]]; then
        log_error "拒绝 rm -rf：目标为危险路径 ${target}"
        return 1
    fi
    local resolved
    if resolved="$(realpath -m "${target}" 2>/dev/null)"; then
        if [[ -z "${resolved}" || "${resolved}" == "/" ]]; then
            log_error "拒绝 rm -rf：解析后路径危险 ${resolved}"
            return 1
        fi
    fi
    rm -rf -- "${target}"
}

cleanup_on_failure() {
    log_warn "安装失败，正在清理残留..."
    if command -v systemctl >/dev/null 2>&1; then
        systemctl disable "${SERVICE_NAME}.service" 2>/dev/null || true
        rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
        systemctl daemon-reload 2>/dev/null || true
    fi
    safe_rm_rf "${AGENT_DIR}" || true
    [[ -n "${STAGING_DIR}" ]] && safe_rm_rf "${STAGING_DIR}" || true
    rm -f "${PACKAGE_FILE}" "${PACKAGE_HEADERS}"
    log_warn "清理完成"
}

# --------------- 参数校验 ---------------
if [[ -z "${SERVER_URL:-}" ]]; then
    log_error "未设置 SERVER_URL 环境变量"
    echo "示例：curl -fsSL <server>/api/v1/agents/install.sh | sudo SERVER_URL=http://192.168.1.10:8000 INSTALL_KEY=ik_xxxx bash" >&2
    exit 1
fi

if [[ -z "${INSTALL_KEY:-}" ]]; then
    log_error "未设置 INSTALL_KEY 环境变量"
    echo "示例：curl -fsSL <server>/api/v1/agents/install.sh | sudo SERVER_URL=http://192.168.1.10:8000 INSTALL_KEY=ik_xxxx bash" >&2
    exit 1
fi

# 去除尾部斜杠
SERVER_URL="${SERVER_URL%/}"

log_info "ChaosOps Agent 安装程序"
log_info "继续安装即表示您已阅读并同意最终用户许可协议（产品内「设置 → 用户协议」查看全文）"
log_info "Server: ${SERVER_URL}"

# --------------- root 权限检查 ---------------
# 需要写 /opt 与 /etc/systemd/system，必须 root
if [[ "$(id -u)" -ne 0 ]]; then
    log_error "Agent 安装需要 root 权限，请使用 sudo 运行"
    echo "示例：curl -fsSL <server>/api/v1/agents/install.sh | sudo SERVER_URL=.. INSTALL_KEY=.. bash" >&2
    exit 1
fi

# --------------- Python 环境检查 ---------------
PYTHON_CMD=""
for cmd in python3.12 python3.11 python3.10 python3; do
    if command -v "$cmd" >/dev/null 2>&1; then
        version=$($cmd --version 2>&1 | awk '{print $2}')
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [[ "$major" -eq 3 && "$minor" -ge 10 ]]; then
            PYTHON_CMD=$cmd
            break
        fi
    fi
done

if [[ -z "$PYTHON_CMD" ]]; then
    log_error "未找到 Python 3.10+，请先安装 Python 3.10 或更高版本"
    exit 1
fi

log_info "使用 Python: ${PYTHON_CMD} ($(${PYTHON_CMD} --version 2>&1))"

if ! command -v systemctl >/dev/null 2>&1; then
    log_error "未找到 systemctl，本脚本需要 systemd 环境"
    exit 1
fi

# 从这里起的任何失败都要清理半成品（服务文件、安装目录、下载临时文件）
trap cleanup_on_failure ERR

# --------------- 创建系统用户 ---------------
create_user() {
    if ! id -u "${AGENT_USER}" >/dev/null 2>&1; then
        useradd --system --no-create-home --home-dir "${AGENT_DIR}" --shell /usr/sbin/nologin "${AGENT_USER}"
        log_info "已创建 ${AGENT_USER} 用户"
    fi
}

# --------------- 下载分发包 ---------------
# 下载只校验 install_key 有效性，不消耗；消耗发生在后续 register 步骤。
download_package() {
    log_info "下载 Agent 分发包..."
    if ! curl -fsSL -D "${PACKAGE_HEADERS}" -o "${PACKAGE_FILE}" \
        "${SERVER_URL}/api/v1/agents/dist/latest?install_key=${INSTALL_KEY}"; then
        log_error "下载 Agent 分发包失败，请检查 SERVER_URL 与 INSTALL_KEY 是否正确、未过期"
        return 1
    fi

    # 从响应头解析 X-Agent-Checksum（不区分大小写，去 \r 与首尾空白）
    EXPECTED_CHECKSUM="$(grep -i '^X-Agent-Checksum:' "${PACKAGE_HEADERS}" | tail -1 | cut -d: -f2- | tr -d '\r' | xargs || true)"
    if [[ -z "${EXPECTED_CHECKSUM}" ]]; then
        log_error "响应头缺少 X-Agent-Checksum，无法校验分发包完整性"
        return 1
    fi

    log_info "校验分发包 sha256..."
    local actual_checksum
    actual_checksum="$(sha256sum "${PACKAGE_FILE}" | awk '{print $1}')"
    if [[ "${actual_checksum}" != "${EXPECTED_CHECKSUM}" ]]; then
        log_error "分发包 sha256 校验失败（期望 ${EXPECTED_CHECKSUM}，实际 ${actual_checksum}），可能下载损坏或被篡改"
        return 1
    fi
    log_info "sha256 校验通过"
}

# --------------- 解压分发包（防 zip-slip） ---------------
extract_package() {
    log_info "解压 Agent 分发包..."
    STAGING_DIR="$(mktemp -d)"

    # 解压前先列出全部条目做安全检查：
    # 1) 拒绝绝对路径与含 .. 的路径穿越（zip-slip）
    # 2) 拒绝符号链接/硬链接条目（tar -tvf 输出首字符 l/h）
    local entry
    while IFS= read -r entry; do
        [[ -z "${entry}" ]] && continue
        case "${entry}" in
            /*)
                log_error "分发包含绝对路径条目，拒绝解压: ${entry}"
                return 1
                ;;
        esac
        # 把条目包在 /.../ 中判断 .. 是否作为独立路径段出现
        case "/${entry}/" in
            */../*)
                log_error "分发包含路径穿越条目，拒绝解压: ${entry}"
                return 1
                ;;
        esac
    done < <(tar -tzf "${PACKAGE_FILE}")

    local line
    while IFS= read -r line; do
        case "${line:0:1}" in
            l|h)
                log_error "分发包含链接条目，拒绝解压: ${line}"
                return 1
                ;;
        esac
    done < <(tar -tvzf "${PACKAGE_FILE}")

    tar -xzf "${PACKAGE_FILE}" -C "${STAGING_DIR}"

    # 包内顶层目录为 chaosops-agent-<version>/agent/...，保留 agent/ 包结构安装：
    # 代码用 `from agent.app...` 绝对导入，systemd 以 `python -m agent.app.main` 启动。
    local pkg_agent_dir=""
    local d
    for d in "${STAGING_DIR}"/chaosops-agent-*; do
        if [[ -d "${d}/agent" ]]; then
            pkg_agent_dir="${d}/agent"
            break
        fi
    done
    if [[ -z "${pkg_agent_dir}" ]]; then
        log_error "分发包结构异常：未找到 chaosops-agent-*/agent 目录"
        return 1
    fi

    mkdir -p "${AGENT_DIR}"
    rm -rf "${AGENT_DIR}/agent"
    cp -r "${pkg_agent_dir}" "${AGENT_DIR}/agent"
    log_info "Agent 文件已安装到 ${AGENT_DIR}/agent"
}

# --------------- 创建 venv 并离线安装依赖 ---------------
install_venv() {
    log_info "创建 Python 虚拟环境并安装依赖（离线 wheels）..."
    if ! "${PYTHON_CMD}" -m venv "${AGENT_DIR}/.venv"; then
        log_error "创建 venv 失败，请确认已安装 python3-venv（Debian/Ubuntu: apt install python3-venv）"
        return 1
    fi

    # 分发包内嵌离线 wheels，目标机无需外网
    "${AGENT_DIR}/.venv/bin/pip" install --no-index \
        --find-links "${AGENT_DIR}/agent/wheels" \
        -r "${AGENT_DIR}/agent/requirements.txt"
    log_info "依赖安装完成"
}

# --------------- 注册 Agent（消耗 install_key） ---------------
register_agent() {
    log_info "向 Server 注册 Agent..."
    local hostname os arch version
    hostname="$(hostname)"
    os="$(uname -s | tr '[:upper:]' '[:lower:]')"
    arch="$(uname -m)"
    version="${AGENT_VERSION:-0.1.0}"

    local response
    if ! response="$(curl -fsS -X POST "${SERVER_URL}/api/v1/agents/register" \
        -H "Content-Type: application/json" \
        -d "{\"install_key\":\"${INSTALL_KEY}\",\"hostname\":\"${hostname}\",\"os\":\"${os}\",\"arch\":\"${arch}\",\"version\":\"${version}\"}" 2>&1)"; then
        log_error "Agent 注册失败: ${response}"
        return 1
    fi

    local agent_token node_id heartbeat_interval
    agent_token="$(echo "${response}" | "${PYTHON_CMD}" -c 'import json,sys; print(json.load(sys.stdin)["agent_token"])')"
    node_id="$(echo "${response}" | "${PYTHON_CMD}" -c 'import json,sys; print(json.load(sys.stdin)["node_id"])')"
    heartbeat_interval="$(echo "${response}" | "${PYTHON_CMD}" -c 'import json,sys; print(json.load(sys.stdin).get("heartbeat_interval",10))')"

    cat > "${CONFIG_FILE}" <<EOF
{
  "server_url": "${SERVER_URL}",
  "agent_token": "${agent_token}",
  "node_id": "${node_id}",
  "heartbeat_interval": ${heartbeat_interval},
  "auto_update": {
    "enabled": true,
    "mode": "manual",
    "check_interval_hours": 24,
    "channel": "stable"
  }
}
EOF
    chmod 600 "${CONFIG_FILE}"
    log_info "Agent 注册成功，节点 ID: ${node_id}"
}

# --------------- 安装并启动 systemd 服务 ---------------
# curl 安装场景没有 deploy/ 目录，服务单元内容内联在此（与 deploy/chaosops-agent.service 保持一致）
install_service() {
    log_info "安装 systemd 服务..."
    cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=ChaosOps Agent
After=network.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=${AGENT_USER}
Group=${AGENT_USER}
WorkingDirectory=${AGENT_DIR}
EnvironmentFile=-${AGENT_DIR}/config.env
ExecStart=${AGENT_DIR}/.venv/bin/python -m agent.app.main --config-dir ${AGENT_DIR}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}.service"
    systemctl start "${SERVICE_NAME}.service"
    log_info "Agent 服务已启动"
}

# --------------- 主流程 ---------------
main() {
    create_user
    download_package
    extract_package
    install_venv
    register_agent
    install_service

    # 下载卸载脚本到安装目录，客户随时可用（失败不阻断安装）
    if curl -fsSL "${SERVER_URL}/api/v1/agents/uninstall.sh" -o "${AGENT_DIR}/uninstall.sh" 2>/dev/null; then
        chmod +x "${AGENT_DIR}/uninstall.sh"
    else
        log_warn "下载卸载脚本失败，可随时从 ${SERVER_URL}/api/v1/agents/uninstall.sh 获取"
    fi

    # 权限收尾：整个安装目录归 agent 用户所有，仅其可访问（config.json 含 agent_token）
    chown -R "${AGENT_USER}:${AGENT_USER}" "${AGENT_DIR}"
    chmod 700 "${AGENT_DIR}"

    trap - ERR
    [[ -n "${STAGING_DIR}" ]] && safe_rm_rf "${STAGING_DIR}" || true
    rm -f "${PACKAGE_FILE}" "${PACKAGE_HEADERS}"

    log_info "ChaosOps Agent 安装完成"
    log_info "查看状态: systemctl status ${SERVICE_NAME}"
    log_info "查看日志: journalctl -u ${SERVICE_NAME} -f"
}

main "$@"
