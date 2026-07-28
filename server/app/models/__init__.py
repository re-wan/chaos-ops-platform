"""数据模型包。

导入所有 SQLModel 模型，确保 SQLModel.metadata 能正确收集表定义。
"""

from app.models.agent_version import AgentUpdateTask, AgentVersion
from app.models.ai_analysis import AIAnalysis
from app.models.analysis_job import AnalysisJob
from app.models.ai_config import AIConfig
from app.models.ai_generated_tool import AIGeneratedTool
from app.models.alert_event import AlertEvent
from app.models.alert_inhibition import AlertInhibition
from app.models.api_key import ApiKey, WebhookSubscription
from app.models.bulk_registration import BulkRegistrationTask
from app.models.alert_rule import AlertRule
from app.models.alert_silence import AlertSilence
from app.models.alert_state import AlertState
from app.models.heal_action import HealAction
from app.models.system_setting import SystemSetting
from app.models.heal_rule import HealRule
from app.models.heal_task import HealTask
from app.models.heal_whitelist import HealAutoApproveWhitelist
from app.models.incident import Incident
from app.models.license_claim import LicenseClaim
from app.models.manual_order import ManualOrder
from app.models.incident_event import IncidentEvent
from app.models.incident_alert import IncidentAlert
from app.models.node_group import NodeGroup
from app.models.notification_channel import NotificationChannel
from app.models.notification_log import NotificationLog
from app.models.notification_template import NotificationTemplate
from app.models.node import Node
from app.models.optimization_effect import OptimizationEffect
from app.models.optimization_suggestion import OptimizationSuggestion
from app.models.remote_execution import RemoteExecution
from app.models.password_reset_token import PasswordResetToken
from app.models.user import User
from app.models.user_session import UserSession
from app.models.metric_sqlite import SQLiteMetricSample

__all__ = [
    "AgentUpdateTask",
    "AgentVersion",
    "AIAnalysis",
    "AIConfig",
    "AnalysisJob",
    "AIGeneratedTool",
    "AlertEvent",
    "AlertInhibition",
    "ApiKey",
    "WebhookSubscription",
    "BulkRegistrationTask",
    "AlertRule",
    "AlertSilence",
    "AlertState",
    "HealAction",
    "SystemSetting",
    "HealRule",
    "HealTask",
    "HealAutoApproveWhitelist",
    "Incident",
    "IncidentAlert",
    "LicenseClaim",
    "ManualOrder",
    "IncidentEvent",
    "NodeGroup",
    "NotificationChannel",
    "NotificationLog",
    "NotificationTemplate",
    "Node",
    "OptimizationEffect",
    "OptimizationSuggestion",
    "RemoteExecution",
    "PasswordResetToken",
    "User",
    "UserSession",
    "SQLiteMetricSample",
]
