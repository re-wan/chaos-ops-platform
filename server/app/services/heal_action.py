"""自愈动作库业务逻辑。

包含动作校验、hash 计算、预置动作初始化、自定义脚本审批等。
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from app.core.logger import get_logger
from app.models.heal_action import HealAction

logger = get_logger("services.heal_action")

VALID_ACTION_TYPES = {"builtin", "custom_script", "ai_generated_script"}
VALID_RISK_LEVELS = {"low", "medium", "high"}
VALID_INTERPRETERS = {"bash", "python", "python3"}

# 需要“审批 + hash 双重校验 + 下发内容”的脚本类动作。
# custom_script：人工编写的自定义脚本；ai_generated_script：Phase 3 AI 生成、
# 经人工审批后入库的脚本。二者在执行链路上共享同一套不下发/校验逻辑。
SCRIPT_ACTION_TYPES = {"custom_script", "ai_generated_script"}

# Server 端自定义脚本黑名单规则（与 Agent 端保持一致）
# 注意：黑名单规则以“不误伤正常脚本”为优先，故意放过一些可绕过写法；
# 文档中必须说明生产环境应配合容器/OS 级隔离。
_BLOCKED_SCRIPT_PATTERNS = [
    # rm -rf / 及其常见变体
    r"\brm\s+-rf\s+/",
    r"\b(/bin/rm|/usr/bin/rm)\s+-rf\s+/",
    # 格式化/分区/危险 dd
    r"\bmkfs\.?\w*\b",
    r"\bfdisk\b",
    r"\bdd\s+if\s*=\s*/dev",
    # 写入敏感系统路径（重定向或 dd）
    r">\s*/(etc|bin|sbin|usr/bin|usr/sbin|dev|sys|proc|boot|lib|lib64)(/|$)",
    r"\bdd\s+.*\bof\s*=\s*/(dev|sys|proc|boot|etc)(/|$)",
    # 关机/重启/系统控制类命令（要求作为命令出现，避免误伤 echo reboot）
    r"(?:^|[;|&`$()])\s*(shutdown|reboot|halt|poweroff|init\s+\d|systemctl|journalctl)\b",
    # 强杀命令
    r"\bkill\s+-9\b",
    r"\bkillall\b",
    r"\bpkill\b",
    # fork 炸弹
    r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",
]
_BLOCKED_SCRIPT_REGEX = [re.compile(p, re.IGNORECASE) for p in _BLOCKED_SCRIPT_PATTERNS]


def _compute_script_hash(script_content: Optional[str]) -> Optional[str]:
    """计算脚本内容的 SHA256 hash。"""
    if script_content is None:
        return None
    return hashlib.sha256(script_content.encode("utf-8")).hexdigest()


def _validate_action_id(action_id: str) -> None:
    """校验 action_id 格式。"""
    if not action_id or len(action_id) > 128:
        raise ValueError("action_id 长度必须在 1-128 字符之间")


def _validate_parameter_schema(parameter_schema: dict) -> None:
    """校验参数 Schema 基本结构。"""
    if not isinstance(parameter_schema, dict):
        raise ValueError("parameter_schema 必须是 JSON 对象")
    if parameter_schema.get("type") not in {"object", None}:
        raise ValueError("parameter_schema 根类型必须是 object")


def _validate_action_type(action_type: str) -> None:
    """校验 action_type。"""
    if action_type not in VALID_ACTION_TYPES:
        raise ValueError(f"action_type 必须是 {VALID_ACTION_TYPES} 之一")


def _validate_risk_level(risk_level: str) -> None:
    """校验 risk_level。"""
    if risk_level not in VALID_RISK_LEVELS:
        raise ValueError(f"risk_level 必须是 {VALID_RISK_LEVELS} 之一")


def _validate_interpreter(interpreter: str) -> None:
    """校验 interpreter。"""
    if interpreter not in VALID_INTERPRETERS:
        raise ValueError(f"interpreter 必须是 {VALID_INTERPRETERS} 之一")


def validate_script_safety(script_content: Optional[str]) -> bool:
    """对脚本内容进行静态黑名单扫描。

    命中黑名单则抛出 ValueError，说明原因。
    注意：黑名单无法防御所有绕过，仅作为基础防护。
    """
    if not script_content:
        return True
    for line_no, line in enumerate(script_content.splitlines(), start=1):
        for pattern in _BLOCKED_SCRIPT_REGEX:
            if pattern.search(line):
                raise ValueError(
                    f"脚本内容包含危险指令 (line {line_no}): {line.strip()[:80]}"
                )
    return True


def get_action_by_id(session: Session, action_id: int) -> Optional[HealAction]:
    """根据数据库 ID 获取动作。"""
    return session.get(HealAction, action_id)


def get_action_by_action_id(
    session: Session, action_id: str
) -> Optional[HealAction]:
    """根据 action_id 标识获取动作。"""
    return session.exec(
        select(HealAction).where(HealAction.action_id == action_id)
    ).first()


def list_heal_actions(
    session: Session,
    risk_level: Optional[str] = None,
) -> list[HealAction]:
    """列出动作，支持按 risk_level 过滤。"""
    query = select(HealAction).order_by(HealAction.created_at)
    if risk_level is not None:
        query = query.where(HealAction.risk_level == risk_level)
    return list(session.exec(query).all())


def create_custom_action(
    session: Session,
    action_id: str,
    name: str,
    description: Optional[str],
    action_type: str,
    script_content: Optional[str],
    parameter_schema: dict,
    risk_level: str,
    interpreter: str = "bash",
) -> HealAction:
    """创建自定义动作。"""
    _validate_action_id(action_id)
    _validate_action_type(action_type)
    _validate_risk_level(risk_level)
    _validate_interpreter(interpreter)
    _validate_parameter_schema(parameter_schema)

    existing = get_action_by_action_id(session, action_id)
    if existing is not None:
        raise ValueError("action_id 已存在")

    if action_type == "custom_script" and not script_content:
        raise ValueError("custom_script 类型必须提供 script_content")

    if action_type == "custom_script":
        validate_script_safety(script_content)

    action = HealAction(
        action_id=action_id,
        name=name,
        description=description,
        action_type=action_type,
        script_content=script_content,
        script_hash=_compute_script_hash(script_content),
        interpreter=interpreter,
        parameter_schema=json.dumps(parameter_schema, ensure_ascii=False),
        risk_level=risk_level,
        is_approved=False,
        is_builtin=False,
    )
    session.add(action)
    session.commit()
    session.refresh(action)

    logger.info(f"创建自定义动作: id={action.id}, action_id={action_id}")
    return action


def update_custom_action(
    session: Session,
    action: HealAction,
    name: Optional[str] = None,
    description: Optional[str] = None,
    script_content: Optional[str] = None,
    interpreter: Optional[str] = None,
    parameter_schema: Optional[dict] = None,
    risk_level: Optional[str] = None,
) -> HealAction:
    """更新自定义动作。

    预置动作不可更新；脚本内容变更后需重新审批。
    """
    if action.is_builtin:
        raise ValueError("预置动作不可更新")

    if name is not None:
        action.name = name
    if description is not None:
        action.description = description
    if script_content is not None:
        validate_script_safety(script_content)
        action.script_content = script_content
        action.script_hash = _compute_script_hash(script_content)
        # 脚本内容变更后重置审批状态
        action.is_approved = False
    if interpreter is not None:
        _validate_interpreter(interpreter)
        action.interpreter = interpreter
    if parameter_schema is not None:
        _validate_parameter_schema(parameter_schema)
        action.parameter_schema = json.dumps(parameter_schema, ensure_ascii=False)
    if risk_level is not None:
        _validate_risk_level(risk_level)
        action.risk_level = risk_level

    action.updated_at = datetime.now(timezone.utc)
    session.add(action)
    session.commit()
    session.refresh(action)

    logger.info(f"更新自定义动作: id={action.id}, action_id={action.action_id}")
    return action


def delete_custom_action(session: Session, action: HealAction) -> None:
    """删除自定义动作。

    预置动作不可删除；删除动作不影响历史执行记录。
    """
    if action.is_builtin:
        raise ValueError("预置动作不可删除")

    session.delete(action)
    session.commit()

    logger.info(f"删除自定义动作: id={action.id}, action_id={action.action_id}")


def approve_custom_action(session: Session, action: HealAction) -> HealAction:
    """审批自定义脚本动作。"""
    if action.is_builtin:
        raise ValueError("预置动作无需审批")
    if action.action_type != "custom_script":
        raise ValueError("只有 custom_script 类型需要审批")

    action.is_approved = True
    action.updated_at = datetime.now(timezone.utc)
    session.add(action)
    session.commit()
    session.refresh(action)

    logger.info(f"审批自定义动作: id={action.id}, action_id={action.action_id}")
    return action


def create_or_update_ai_generated_action(
    session: Session,
    *,
    action_id: str,
    name: str,
    description: Optional[str],
    script_content: str,
    interpreter: str,
    risk_level: str,
    commit: bool = True,
) -> HealAction:
    """创建或更新一条已审批的 AI 生成脚本动作，使其进入动作库。

    仅在 AI 工具审批通过时由服务层调用。幂等：若 ``action_id`` 已存在则更新其
    内容/hash/风险等级并保持已审批状态。

    安全：再次执行服务端黑名单扫描作为兜底（fail-closed）；hash 由内容实时计算，
    与 Agent 端执行前校验算法一致。

    Args:
        commit: True（默认）保持向后兼容——函数内部自行提交事务。
            False 时仅 ``add + flush``（分配主键但不提交），提交责任交给调用方；
            ``approve_tool`` 借此把"动作入库 + 工具状态/关联"放进同一个事务，
            避免两阶段提交中途失败留下已 approved 动作 + pending 工具的中间态。
    """
    _validate_action_id(action_id)
    _validate_interpreter(interpreter)
    _validate_risk_level(risk_level)
    if not script_content:
        raise ValueError("ai_generated_script 必须提供 script_content")
    # 兜底黑名单扫描：AI 工具自有静态扫描已覆盖该黑名单，此处作为深度防御。
    validate_script_safety(script_content)

    script_hash = _compute_script_hash(script_content)
    action = get_action_by_action_id(session, action_id)
    now = datetime.now(timezone.utc)
    if action is None:
        action = HealAction(
            action_id=action_id,
            name=name,
            description=description,
            action_type="ai_generated_script",
            script_content=script_content,
            script_hash=script_hash,
            interpreter=interpreter,
            parameter_schema=json.dumps(
                {"type": "object", "properties": {}}, ensure_ascii=False
            ),
            risk_level=risk_level,
            is_approved=True,
            is_builtin=False,
        )
    else:
        action.name = name
        action.description = description
        action.action_type = "ai_generated_script"
        action.script_content = script_content
        action.script_hash = script_hash
        action.interpreter = interpreter
        action.risk_level = risk_level
        action.is_approved = True
        action.is_builtin = False
        action.updated_at = now

    session.add(action)
    if commit:
        session.commit()
        session.refresh(action)
    else:
        # 仅 flush 分配主键（供日志/外键引用），提交交给外层事务统一完成。
        session.flush()
    logger.info(
        f"AI 生成脚本进入动作库: id={action.id}, action_id={action.action_id}"
    )
    return action


def verify_script_hash(action: HealAction) -> bool:
    """校验脚本 hash 是否与内容匹配。"""
    if action.action_type not in SCRIPT_ACTION_TYPES:
        return True
    if action.script_content is None:
        return False
    expected = _compute_script_hash(action.script_content)
    return expected == action.script_hash


def get_script_for_execution(
    session: Session, action_id: str
) -> Optional[HealAction]:
    """获取用于执行（下发给 Agent）的自定义脚本。

    返回 None 表示动作不存在、不是自定义脚本、未审批或 hash 校验失败。
    """
    action = get_action_by_action_id(session, action_id)
    if action is None:
        return None
    if action.action_type not in SCRIPT_ACTION_TYPES:
        return None
    if not action.is_approved:
        logger.warning(f"自定义脚本未审批，拒绝下发: action_id={action_id}")
        return None
    if not verify_script_hash(action):
        logger.warning(f"自定义脚本 hash 校验失败，拒绝下发: action_id={action_id}")
        return None
    return action


# 预置动作定义
_BUILTIN_ACTIONS = [
    {
        "action_id": "restart_service",
        "name": "heal.builtin.restart_service.name",
        "description": "heal.builtin.restart_service.description",
        "action_type": "builtin",
        "risk_level": "medium",
        "parameter_schema": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string"},
            },
            "required": ["service_name"],
        },
    },
    {
        "action_id": "restart_container",
        "name": "heal.builtin.restart_container.name",
        "description": "heal.builtin.restart_container.description",
        "action_type": "builtin",
        "risk_level": "medium",
        "parameter_schema": {
            "type": "object",
            "properties": {
                "container_name": {"type": "string"},
            },
            "required": ["container_name"],
        },
    },
    {
        "action_id": "clear_log",
        "name": "heal.builtin.clear_log.name",
        "description": "heal.builtin.clear_log.description",
        "action_type": "builtin",
        "risk_level": "low",
        "parameter_schema": {
            "type": "object",
            "properties": {
                "log_path": {"type": "string"},
                "keep_days": {"type": "integer", "minimum": 0},
            },
            "required": ["log_path", "keep_days"],
        },
    },
    {
        "action_id": "kill_process",
        "name": "heal.builtin.kill_process.name",
        "description": "heal.builtin.kill_process.description",
        "action_type": "builtin",
        "risk_level": "high",
        "parameter_schema": {
            "type": "object",
            "properties": {
                "process_name": {"type": "string"},
                "pid": {"type": "integer", "minimum": 1},
            },
            "anyOf": [
                {"required": ["process_name"]},
                {"required": ["pid"]},
            ],
        },
    },
    {
        "action_id": "reboot_node",
        "name": "heal.builtin.reboot_node.name",
        "description": "heal.builtin.reboot_node.description",
        "action_type": "builtin",
        "risk_level": "high",
        "parameter_schema": {
            "type": "object",
            "properties": {
                "delay_seconds": {"type": "integer", "minimum": 0},
            },
            "required": ["delay_seconds"],
        },
    },
    {
        "action_id": "run_script",
        "name": "heal.builtin.run_script.name",
        "description": "heal.builtin.run_script.description",
        "action_type": "builtin",
        "risk_level": "high",
        "parameter_schema": {
            "type": "object",
            "properties": {
                "script_id": {"type": "string"},
                "args": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["script_id"],
        },
    },
    {
        "action_id": "view_logs",
        "name": "heal.builtin.view_logs.name",
        "description": "heal.builtin.view_logs.description",
        "action_type": "builtin",
        "risk_level": "low",
        "snapshot_target_field": "log_path",
        "parameter_schema": {
            "type": "object",
            "properties": {
                "log_path": {"type": "string"},
                "lines": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            "required": ["log_path"],
        },
    },
    {
        "action_id": "disk_usage",
        "name": "heal.builtin.disk_usage.name",
        "description": "heal.builtin.disk_usage.description",
        "action_type": "builtin",
        "risk_level": "low",
        "snapshot_target_field": "path",
        "parameter_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
            },
        },
    },
    {
        "action_id": "service_status",
        "name": "heal.builtin.service_status.name",
        "description": "heal.builtin.service_status.description",
        "action_type": "builtin",
        "risk_level": "low",
        "parameter_schema": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string"},
            },
            "required": ["service_name"],
        },
    },
]


def init_builtin_actions(session: Session) -> int:
    """初始化预置动作，返回新增/更新的动作数量。"""
    count = 0
    for definition in _BUILTIN_ACTIONS:
        action = get_action_by_action_id(session, definition["action_id"])
        if action is None:
            action = HealAction(
                action_id=definition["action_id"],
                name=definition["name"],
                description=definition["description"],
                action_type=definition["action_type"],
                parameter_schema=json.dumps(
                    definition["parameter_schema"], ensure_ascii=False
                ),
                risk_level=definition["risk_level"],
                snapshot_target_field=definition.get("snapshot_target_field"),
                is_approved=True,
                is_builtin=True,
            )
            session.add(action)
            count += 1
        else:
            # 预置动作存在则更新元数据，保留 ID
            action.name = definition["name"]
            action.description = definition["description"]
            action.parameter_schema = json.dumps(
                definition["parameter_schema"], ensure_ascii=False
            )
            action.risk_level = definition["risk_level"]
            action.snapshot_target_field = definition.get("snapshot_target_field")
            action.is_builtin = True
            action.is_approved = True
            action.updated_at = datetime.now(timezone.utc)
            session.add(action)
            count += 1

    if count > 0:
        session.commit()
        logger.info(f"预置动作初始化完成: {count} 个")
    return count
