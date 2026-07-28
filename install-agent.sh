#!/usr/bin/env bash
# ChaosOps Agent Linux 一键安装脚本
# 用法: ./install-agent.sh --server-url=https://your-server --install-key=ik_xxxx [--prefix /path]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 离线包内 install-agent.sh 位于包根目录；开发/项目内位于 scripts/ 目录
if [[ "$(basename "${SCRIPT_DIR}")" == "scripts" ]]; then
    PACKAGE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
else
    PACKAGE_ROOT="${SCRIPT_DIR}"
fi
INSTALL_DIR="/opt/chaosops-agent"
CONFIG_FILE="${INSTALL_DIR}/config.json"
MOCK_MODE="${MOCK_MODE:-false}"
DRY_RUN="false"

SERVER_URL=""
INSTALL_KEY=""

log_info() { echo "[INFO] $*"; }
log_warn() { echo "[WARN] $*"; }
log_error() { echo "[ERROR] $*" >&2; }

# --prefix 白名单：仅允许 [A-Za-z0-9._/-]，必须为绝对路径，禁止空 / 根 / 相对 / 含 ..
# 该白名单同时保证后续 sed 替换的分隔符 | 不会出现在路径中（注入安全）。
validate_prefix() {
    local p="${INSTALL_DIR}"
    if [[ -z "${p}" ]]; then
        log_error "--prefix 不能为空"
        exit 1
    fi
    if [[ "${p}" == "/" ]]; then
        log_error "--prefix 不能为根目录 /"
        exit 1
    fi
    if [[ "${p}" != /* ]]; then
        log_error "--prefix 必须为绝对路径（以 / 开头），当前: ${p}"
        exit 1
    fi
    if [[ "${p}" == *".."* ]]; then
        log_error "--prefix 不得包含 ..，当前: ${p}"
        exit 1
    fi
    if ! [[ "${p}" =~ ^[A-Za-z0-9._/-]+$ ]]; then
        log_error "--prefix 含非法字符（仅允许字母/数字/点/下划线/连字符/斜杠），当前: ${p}"
        exit 1
    fi
}

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

usage() {
    cat <<EOF
用法: $0 --server-url=<URL> --install-key=<KEY> [选项]

参数:
  --server-url    ChaosOps Server 地址，例如 https://ops.example.com
  --install-key   一次性安装密钥，例如 ik_xxxx

选项:
  --prefix <dir>  安装目录前缀（默认 /opt/chaosops-agent）
  --dry-run       只打印安装计划，不修改系统
  -h, --help      显示本帮助
EOF
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --server-url=*)
                SERVER_URL="${1#*=}"
                shift
                ;;
            --server-url)
                SERVER_URL="$2"
                shift 2
                ;;
            --install-key=*)
                INSTALL_KEY="${1#*=}"
                shift
                ;;
            --install-key)
                INSTALL_KEY="$2"
                shift 2
                ;;
            --prefix=*)
                INSTALL_DIR="${1#*=}"
                shift
                ;;
            --prefix)
                INSTALL_DIR="$2"
                shift 2
                ;;
            --dry-run)
                DRY_RUN="true"
                shift
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                log_error "未知参数: $1"
                usage
                exit 1
                ;;
        esac
    done

    CONFIG_FILE="${INSTALL_DIR}/config.json"

    # 白名单校验 --prefix，失败直接退出并打印原因（fail-closed）
    validate_prefix

    if [[ -z "${SERVER_URL}" || -z "${INSTALL_KEY}" ]]; then
        log_error "--server-url 和 --install-key 为必填参数"
        usage
        exit 1
    fi
}

cleanup_on_failure() {
    if [[ "${MOCK_MODE}" == "true" ]]; then
        log_info "MOCK_MODE: 跳过清理"
        return
    fi
    if [[ "${DRY_RUN}" == "true" ]]; then
        log_info "DRY_RUN: 跳过清理"
        return
    fi
    log_warn "安装失败，正在清理残留..."
    systemctl disable chaosops-agent.service 2>/dev/null || true
    rm -f /etc/systemd/system/chaosops-agent.service
    systemctl daemon-reload 2>/dev/null || true
    safe_rm_rf "${INSTALL_DIR}" || true
    log_warn "清理完成"
}

check_requirements() {
    log_info "检查系统要求..."

    if [[ "$(uname -s)" != "Linux" ]]; then
        log_error "本安装脚本仅支持 Linux 系统"
        return 1
    fi

    if ! command -v python3 >/dev/null 2>&1; then
        log_error "未找到 python3，请先安装 Python 3.10 或更高版本"
        return 1
    fi
    if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"; then
        log_error "Python 版本过低，需要 3.10+"
        return 1
    fi

    if [[ "${MOCK_MODE}" != "true" && "${DRY_RUN}" != "true" ]]; then
        if ! command -v systemctl >/dev/null 2>&1; then
            log_error "未找到 systemctl，本脚本需要 systemd 环境"
            return 1
        fi
        if [[ "$(id -u)" -ne 0 ]]; then
            log_error "Agent 安装需要 root 权限，请使用 sudo 运行"
            return 1
        fi
    fi

    log_info "系统检查通过"
}

create_user() {
    if [[ "${MOCK_MODE}" == "true" || "${DRY_RUN}" == "true" ]]; then
        log_info "MOCK_MODE/DRY_RUN: 跳过创建系统用户"
        return
    fi
    if ! id -u chaosops-agent >/dev/null 2>&1; then
        useradd --system --no-create-home --home-dir "${INSTALL_DIR}" --shell /usr/sbin/nologin chaosops-agent
        log_info "已创建 chaosops-agent 用户"
    fi
}

register_agent() {
    log_info "向 Server 注册 Agent..."
    local hostname os arch version
    hostname="$(hostname)"
    os="$(uname -s | tr '[:upper:]' '[:lower:]')"
    arch="$(uname -m)"
    version="${AGENT_VERSION:-0.1.0}"

    if [[ "${MOCK_MODE}" == "true" || "${DRY_RUN}" == "true" ]]; then
        log_info "MOCK_MODE/DRY_RUN: 跳过真实注册"
        if [[ "${DRY_RUN}" == "true" ]]; then
            log_info "[dry-run] 将生成配置文件: ${CONFIG_FILE}"
            return
        fi
        cat > "${CONFIG_FILE}" <<EOF
{
  "server_url": "${SERVER_URL}",
  "agent_token": "mock-token-for-test",
  "node_id": "node_mock",
  "heartbeat_interval": 10,
  "auto_update": {
    "enabled": true,
    "mode": "manual",
    "check_interval_hours": 24,
    "channel": "stable"
  }
}
EOF
        return
    fi

    local response
    if ! response="$(curl -fsS -X POST "${SERVER_URL}/api/v1/agents/register" \
        -H "Content-Type: application/json" \
        -d "{\"install_key\":\"${INSTALL_KEY}\",\"hostname\":\"${hostname}\",\"os\":\"${os}\",\"arch\":\"${arch}\",\"version\":\"${version}\"}" 2>&1)"; then
        log_error "Agent 注册失败: ${response}"
        return 1
    fi

    local agent_token node_id heartbeat_interval
    agent_token="$(echo "${response}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["agent_token"])')"
    node_id="$(echo "${response}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["node_id"])')"
    heartbeat_interval="$(echo "${response}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("heartbeat_interval",10))')"

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
    # config.json 在 install_files（内含 chown -R）之后写入，属主为 root，
    # 必须补一次 chown，否则 chaosops-agent 用户无法读取配置导致服务启动失败。
    chown chaosops-agent:chaosops-agent "${CONFIG_FILE}" 2>/dev/null || true
    log_info "Agent 注册成功，节点 ID: ${node_id}"
}

install_files() {
    log_info "安装 Agent 文件到 ${INSTALL_DIR}..."
    if [[ "${DRY_RUN}" == "true" ]]; then
        log_info "[dry-run] 将复制: ${PACKAGE_ROOT}/agent -> ${INSTALL_DIR}/agent"
        log_info "[dry-run] 将创建 venv: ${INSTALL_DIR}/.venv"
        return
    fi

    mkdir -p "${INSTALL_DIR}"

    # 整个 agent/ 目录作为 Python 包安装到 ${INSTALL_DIR}/agent：
    # 代码内全部使用 `from agent.app...` 绝对导入，systemd 单元以
    # `python -m agent.app.main`（WorkingDirectory=${INSTALL_DIR}）启动，
    # 因此必须保留包目录结构，不能扁平化复制到 ${INSTALL_DIR} 根下。
    rm -rf "${INSTALL_DIR}/agent"
    cp -r "${PACKAGE_ROOT}/agent" "${INSTALL_DIR}/agent"

    python3 -m venv "${INSTALL_DIR}/.venv"
    # shellcheck source=/dev/null
    source "${INSTALL_DIR}/.venv/bin/activate"

    local pip_install_args=("-r" "${INSTALL_DIR}/agent/requirements.txt")
    if [[ -d "${INSTALL_DIR}/agent/wheels" ]]; then
        pip_install_args+=("--no-index" "--find-links" "${INSTALL_DIR}/agent/wheels")
    fi

    if [[ "${MOCK_MODE}" == "true" ]]; then
        log_info "MOCK_MODE: 跳过 pip install"
    else
        pip install "${pip_install_args[@]}"
    fi

    chown -R chaosops-agent:chaosops-agent "${INSTALL_DIR}" 2>/dev/null || true
    chmod 700 "${INSTALL_DIR}"
    log_info "Agent 文件安装完成"
}

install_service() {
    log_info "安装 systemd 服务..."
    if [[ "${MOCK_MODE}" == "true" || "${DRY_RUN}" == "true" ]]; then
        log_info "MOCK_MODE/DRY_RUN: 跳过 systemd 服务安装"
        return
    fi

    local service_src="${PACKAGE_ROOT}"
    if [[ ! -f "${service_src}/chaosops-agent.service" ]]; then
        service_src="${PACKAGE_ROOT}/deploy"
    fi

    local temp_dir
    temp_dir="$(mktemp -d)"
    # sed 分隔符用 |：validate_prefix 已保证 INSTALL_DIR 不含 | 与 &，故替换安全。
    sed -e "s|/opt/chaosops-agent|${INSTALL_DIR}|g" \
        -e "s|User=chaosops-agent|User=chaosops-agent|g" \
        -e "s|Group=chaosops-agent|Group=chaosops-agent|g" \
        "${service_src}/chaosops-agent.service" > "${temp_dir}/chaosops-agent.service"

    cp "${temp_dir}/chaosops-agent.service" /etc/systemd/system/chaosops-agent.service
    safe_rm_rf "${temp_dir}" || true

    systemctl daemon-reload
    systemctl enable chaosops-agent.service
    systemctl start chaosops-agent.service
    log_info "Agent 服务已启动"
}

print_plan() {
    log_info "========== 安装计划 =========="
    log_info "Server URL: ${SERVER_URL}"
    log_info "Install Key: ${INSTALL_KEY}"
    log_info "安装前缀: ${INSTALL_DIR}"
    log_info "配置文件: ${CONFIG_FILE}"
    log_info "MOCK_MODE: ${MOCK_MODE}"
    log_info "执行步骤:"
    log_info "  1. 检查系统要求"
    log_info "  2. 创建系统用户 chaosops-agent"
    log_info "  3. 安装 Agent 文件并创建 venv"
    log_info "  4. 向 Server 注册 Agent"
    log_info "  5. 安装并启动 systemd 服务"
    log_info "=============================="
}

main() {
    parse_args "$@"

    log_info "开始安装 ChaosOps Agent..."
    log_info "Server: ${SERVER_URL}"
    log_info "安装目录: ${INSTALL_DIR}"

    if [[ "${DRY_RUN}" == "true" ]]; then
        print_plan
    fi

    if ! check_requirements; then
        exit 1
    fi

    if [[ "${DRY_RUN}" == "true" ]]; then
        log_info "DRY_RUN: 安装计划预览完成，不执行实际安装"
        exit 0
    fi

    trap cleanup_on_failure ERR

    create_user
    install_files
    register_agent
    install_service

    trap - ERR

    log_info "ChaosOps Agent 安装完成"
}

main "$@"
