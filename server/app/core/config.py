"""ChaosOps Server 应用配置。

所有配置项优先从环境变量读取，并提供合理的本地开发默认值。
生产环境部署时，务必通过环境变量覆盖敏感字段，尤其是 SECRET_KEY。
"""

import os
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用全局配置类。

    字段说明：
        APP_NAME: 应用显示名称。
        APP_VERSION: 当前版本号。
        DEBUG: 是否开启调试模式，生产环境应为 False。
        DATABASE_URL: SQLModel/SQLAlchemy 数据库连接字符串。
        SECRET_KEY: 用于签名 Session/Token 等，生产环境必须替换。
    """

    APP_NAME: str = "ChaosOps"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    DATABASE_URL: str = "sqlite:///./chaosops.db"
    SECRET_KEY: str = "change-me-in-production-please"

    # 用户 Session 配置
    USER_SESSION_MAX_AGE_DAYS: int = 30
    USER_SESSION_SLIDE_DAYS: int = 7
    BCRYPT_ROUNDS: int = 12

    # 默认管理员账号（仅 init_admin.py 使用）
    # 不再内置固定弱口令：DEFAULT_ADMIN_PASSWORD 留空时，init_admin 随机生成一次性初始口令
    # 并仅在首次创建账号时打印到 stdout（运维需立即登录修改）。显式设置时保持兼容，
    # 但弱口令（chaosops/123456/过短等）会在启动自检与 init 时记强警告。
    DEFAULT_ADMIN_USERNAME: str = "admin"
    DEFAULT_ADMIN_PASSWORD: Optional[str] = None

    # Agent 认证与接入配置
    INSTALL_KEY_TTL_SECONDS: int = 3600
    AGENT_HEARTBEAT_INTERVAL: int = 10
    # 未配置时会根据请求头（X-Forwarded-Host / Host / base_url）推断
    PUBLIC_SERVER_URL: Optional[str] = None

    # 默认通知模板 seed 语言（zh/en）：模板面向邮件/IM 接收端，按部署环境选择；
    # 仅影响首次 seed（insert-only），存量模板不刷新
    DEFAULT_LOCALE: str = "zh"

    # Server 自身韧性配置
    # 多 worker 部署下后台任务领导权 watchdog 间隔（秒）：未抢到文件锁的 worker 每隔该间隔
    # 非阻塞重试取锁；leader 进程崩溃后锁被 OS 回收，其余 worker 在下一个 tick 自动接管
    # 调度器/告警检测/队列消费等后台任务，无需人工重启。已持锁 / 无 fcntl 降级 / 显式标记的
    # 非 leader worker（SERVER_WORKER_ID）下 watchdog 均空转，无副作用。
    SINGLE_INSTANCE_WATCHDOG_SECONDS: float = 5.0
    LOG_DIR: str = "./logs"
    BACKUP_DIR: str = "./backups"
    LOG_MAX_BYTES: int = 10 * 1024 * 1024  # 10MB
    LOG_BACKUP_COUNT: int = 7
    BACKUP_RETENTION_COUNT: int = 7
    LOCAL_AGENT_ENABLED: bool = True
    LOCAL_AGENT_CHECK_INTERVAL: int = 10
    LOCAL_AGENT_HEALTH_TIMEOUT: float = 5.0
    # 本地 Agent 死亡自动重启（批 16，H2 修复）：watchdog 发现 Agent 退出后告警，
    # 冷却 COOLDOWN 秒后重新拉起；滑动窗口 WINDOW 秒内重启达 MAX 次则放弃并告警
    # 人工介入（崩溃循环防护）。Agent 稳定存活超冷却期后重启计数重置（衰减）。
    LOCAL_AGENT_RESTART_COOLDOWN_SECONDS: float = 30.0
    LOCAL_AGENT_RESTART_MAX_FAILURES: int = 5
    LOCAL_AGENT_RESTART_WINDOW_SECONDS: float = 600.0

    # Cookie 安全设置：默认开启 secure（默认安全）。本地 HTTP 开发时通过环境变量
    # COOKIE_SECURE=false 显式关闭；生产必须由前置 TLS 反代终结 HTTPS。
    COOKIE_SECURE: bool = True
    COOKIE_SAMESITE: str = "lax"

    # 文档开关：生产默认关闭 /docs 与 /openapi.json，开发环境通过 DOCS_ENABLED=true 开启
    DOCS_ENABLED: bool = False

    # License 配置（Phase 2 Step 10）
    LICENSE_FILE_PATH: str = "./license.json"
    SERVER_INSTALL_ID_SALT: str = "chaosops-server-salt"

    # License 自助生成（license claim）配置
    # 签名密钥对目录（默认 deploy/license_keys，测试可指向临时目录）
    LICENSE_KEYS_DIR: Optional[str] = None
    # Paddle 订单核验：未配置 PADDLE_API_KEY 时核验降级为「暂不可用」
    PADDLE_API_KEY: Optional[str] = None
    PADDLE_API_BASE_URL: str = "https://api.paddle.com"
    # Cloudflare Turnstile 人机验证：未配置时降级为「同一 IP 提交最小间隔 3 秒」
    TURNSTILE_SECRET_KEY: Optional[str] = None
    LICENSE_CLAIM_MIN_INTERVAL_SECONDS: float = 3.0
    # 一次性下载令牌有效期（小时）
    LICENSE_DOWNLOAD_TOKEN_TTL_HOURS: int = 24

    # PostgreSQL 连接池配置（Phase 2 Step 13）。
    # 默认水位按「单 worker 中等并发 + ingest 高峰」设定（H4：Y3 压测 200 节点 /
    # 50k samples/s 时 5+10 耗尽，QueuePool 连接超时累计 165 次）。
    # 多 worker 部署时总连接数 ≈ workers × (pool_size + max_overflow)，
    # 请按 worker 数 × 并发与 PG max_connections 预算调整。
    # 注意：SQLite 分支不使用连接池参数（单写者，调大收益有限）。
    DATABASE_POOL_SIZE: int = 20
    DATABASE_MAX_OVERFLOW: int = 20
    DATABASE_POOL_RECYCLE_SECONDS: int = 3600

    # 指标存储后端（Phase 2 支持 influxdb / sqlite 切换）。
    # 默认 sqlite：开箱即用、无外部依赖，避免无 InfluxDB 时启动告警写入失败；
    # 生产高吞吐场景可通过 METRIC_BACKEND_TYPE=influxdb 或管理后台切换。
    METRIC_BACKEND_TYPE: str = "sqlite"  # influxdb / sqlite
    METRIC_SQLITE_DATABASE_URL: Optional[str] = None  # 默认复用 DATABASE_URL

    # 指标摄入 DoS 防护（收尾修复第 4 批）。
    # 单请求最大样本数：超过则 422（Pydantic max_items 在反序列化阶段拦截）。
    METRIC_INGEST_MAX_SAMPLES: int = 5000
    # 全局请求体字节上限：超过则 413，由中间件基于 Content-Length 在读 body 前拦截，
    # 防止认证 Agent 发超大 payload 打爆内存。默认 2MB 远大于正常上报体量。
    METRIC_INGEST_MAX_BODY_BYTES: int = 2 * 1024 * 1024

    # InfluxDB（Phase 1 默认时序指标后端）
    INFLUXDB_URL: str = "http://influxdb:8086"
    INFLUXDB_TOKEN: str = "change-influxdb-token-in-production"
    INFLUXDB_ORG: str = "chaosops"
    INFLUXDB_BUCKET: str = "chaosops"
    INFLUXDB_RETENTION_DAYS: int = 30

    # 系统邮件（密码重置等）SMTP 配置
    SMTP_HOST: Optional[str] = None
    SMTP_PORT: int = 587
    SMTP_USERNAME: Optional[str] = None
    SMTP_PASSWORD: Optional[str] = None
    SMTP_USE_TLS: bool = True
    SMTP_FROM_ADDR: Optional[str] = None

    # 启动时是否检查指标后端健康（测试环境可关闭）
    METRIC_BACKEND_HEALTH_CHECK_AT_STARTUP: bool = True

    # 告警检测引擎配置
    ALERT_DETECTOR_ENABLED: bool = True
    ALERT_DETECTOR_CACHE_TTL_SECONDS: int = 45
    ALERT_DETECTOR_CACHE_MAX_ENTRIES: int = 10000
    ALERT_DETECTOR_WORKERS: int = 2
    ALERT_DETECTOR_RELOAD_INTERVAL_SECONDS: int = 60

    # 告警去重与通知聚合配置
    ALERT_DEDUP_WINDOW_SECONDS: int = 300
    ALERT_AGGREGATE_MAX_BYTES: int = 4096

    # 自愈执行引擎配置
    HEAL_EXECUTION_TIMEOUT_SECONDS: int = 120
    # running 卡死清扫宽限（秒）：仅清扫 started_at 早于 (超时 + 宽限) 的任务，
    # 避免误清扫刚启动、仍在正常执行窗口内的任务。
    HEAL_RUNNING_SWEEP_GRACE_SECONDS: int = 60

    # AI 自我优化（Phase 3 Step 01）配置
    # 默认关闭：需要积累足够历史数据后才有效，避免意外消耗 LLM token。
    AI_SELF_OPTIMIZATION_ENABLED: bool = False
    AI_SELF_OPTIMIZATION_WINDOW_DAYS: int = 30
    AI_SELF_OPTIMIZATION_MIN_DATA_DAYS: int = 7

    # 优化建议生命周期（Phase 3 Step 03）配置
    # apply 后效果追踪窗口（天）：到期由每日任务对比 before/after 写 OptimizationEffect。
    # 周分析调度复用 STEP_01 的周日 03:30，不新增 cron 配置项。
    OPTIMIZATION_EFFECT_TRACK_DAYS: int = 7

    # AI 自定义工具生成（Phase 3 Step 02）配置（企业版专属，安全敏感）
    # 功能总开关；但企业版 License 校验失败仍优先 403（见 deps.require_feature）。
    AI_TOOL_GENERATION_ENABLED: bool = True
    # 沙箱验证默认超时（秒）：超时/异常的脚本不得进入审批流。
    AI_TOOL_SANDBOX_TIMEOUT_SECONDS: int = 30
    # 生成脚本的最大字节数：超过则拒绝生成，避免巨型脚本注入。
    AI_TOOL_MAX_SCRIPT_BYTES: int = 65536
    # 批 15：沙箱验证 Runner 选择（blacklist|docker，默认 blacklist 行为不变）。
    # docker 为可选容器级隔离（断网/只读 FS/资源限额），需 Server 主机可用 docker
    # 且镜像本地预置；探测失败自动降级黑名单沙箱（30s 节流告警）。
    AI_TOOL_SANDBOX_RUNNER: str = "blacklist"
    AI_TOOL_SANDBOX_DOCKER_IMAGE: str = "python:3.12-slim"

    # 开放 API（Phase 3 Step 04）配置
    # 总开关：关闭后 /api/open/v1/* 路由不注册（404）。
    OPEN_API_ENABLED: bool = True
    # 按 License edition 的默认限流（次/分钟），JSON 字符串；
    # ApiKey.rate_limit>0 时以 Key 自身值覆盖。缺省/解析失败时回退到内置表。
    OPEN_API_DEFAULT_RATE_LIMITS: str = '{"free":60,"professional":600,"enterprise":6000}'
    # 限流滑动窗口（秒）。
    OPEN_API_RATE_LIMIT_WINDOW_SECONDS: int = 60
    # 限流故障期降级限额比例（收尾修复第 9 批，宁严勿宽）：Redis 运行期故障（熔断窗口内
    # 或计数异常）回退进程内窗口时，限额 = max(1, int(limit * 本比例))。多 worker 下回退窗口
    # 各自计数、总额度会被放大，故故障期主动收紧（默认 0.5 即砍半）逼近 fail-closed。
    # 取值 (0, 1]，越小越严；1.0 表示不收紧。**仅影响故障期**——Redis 正常走分布式窗口、
    # 未启用 Redis（纯同步模式）走全额内存窗口，均不受本项影响。
    OPEN_API_RATE_LIMIT_DEGRADED_RATIO: float = Field(default=0.5, gt=0.0, le=1.0)
    # 开放 API 指标查询单次最大时间窗（天），防止超大查询拖垮时序库。
    OPEN_API_METRIC_MAX_RANGE_DAYS: int = 7
    # API Key 明文密钥体长度（secrets.token_urlsafe 的字节数）。
    OPEN_API_KEY_TOKEN_BYTES: int = 32
    # last_used_at 写入节流（秒）：同一 Key 在该间隔内最多写库一次，降低热路径写放大。
    OPEN_API_LAST_USED_WRITE_INTERVAL_SECONDS: int = 30

    # 开放 API Webhook（Phase 3 Step 04）配置
    # 投递失败最大重试次数（指数退避）。
    WEBHOOK_RETRY_MAX: int = 5
    # 指数退避基数（秒）：第 i 次重试前等待 base * 2**i。
    WEBHOOK_BACKOFF_BASE_SECONDS: int = 2
    # 单次 Webhook 投递 HTTP 超时（秒）。
    WEBHOOK_HTTP_TIMEOUT_SECONDS: float = 10.0
    # 出站 Webhook 投递并发上限（在飞任务数，收尾修复第 5 批）：
    # 告警风暴下超出上限的投递进入等待队列，避免事件循环被无界任务饱和。
    WEBHOOK_MAX_CONCURRENT: int = 100
    # 投递等待队列上限：满后新投递被丢弃并记 warning（drop-newest 背压）。
    WEBHOOK_QUEUE_MAX: int = 1000

    # 出站请求 SSRF 防护（收尾修复第 5 批）
    # 默认拦截指向环回/私有/链路本地/保留地址的出站 URL（Webhook 投递与通知渠道）。
    # 仅在完全受信的内网私有化部署中可显式开启放行；开启后 SSRF 风险由部署方承担，
    # 详见 docs/api/API_OPEN_DESIGN.md §12。
    OUTBOUND_ALLOW_PRIVATE_HOSTS: bool = False

    # Redis 任务队列（Phase 3 Step 05 性能优化）配置
    # 未配置 REDIS_URL 时任务队列自动降级为进程内同步处理，Server 不依赖 Redis 也能启动。
    REDIS_URL: Optional[str] = None
    # 任务队列开关：auto（默认，配置了 REDIS_URL 且可达才启用）/ on（强制启用）/ off（强制禁用，走同步降级）。
    TASK_QUEUE_ENABLED: str = "auto"
    # 单任务最大处理次数（首次 + 重试），超过则进入死信队列（DLQ）。
    TASK_QUEUE_MAX_ATTEMPTS: int = 5
    # 重试指数退避基数（秒）：第 i 次重试前等待 base * 2**(i-1)。
    TASK_QUEUE_BACKOFF_BASE_SECONDS: float = 1.0
    # 每个队列的 worker 线程数（Redis 模式下单队列多消费者天然不重复）。
    TASK_QUEUE_WORKER_CONCURRENCY: int = 2
    # Redis BLPOP 阻塞超时（秒），越小停机响应越快。
    TASK_QUEUE_BLOCK_TIMEOUT_SECONDS: int = 2
    # 运行期 Redis 故障熔断窗口（秒）：窗口内 is_async() 返 False，enqueue 改走同步直调，
    # 避免在请求线程内反复重试/sleep；窗口后首次 Redis 操作成功即恢复异步。
    TASK_QUEUE_CIRCUIT_TTL_SECONDS: float = 30.0
    # 主队列水位阈值：达到后 enqueue 拒绝（QueueFullError→503），让 Agent 退避，防 Redis OOM。
    # 默认 10 万，足以覆盖正常突发；设为 0 可关闭背压。
    TASK_QUEUE_MAX_SIZE: int = 100000
    # DLQ 告警阈值：超过则记 warning（不阻塞，仅观测）；设为 0 关闭告警。
    TASK_QUEUE_DLQ_WARN_SIZE: int = 1000

    # 自愈 5 分钟去重（Phase 3 Step 05，设计 §4.3）
    # 去重窗口（秒）：同一 (node_id, action_id) 在窗口内不重复下发。
    HEAL_DEDUP_WINDOW_SECONDS: int = 300
    # auto（默认，Redis 可用时启用分布式去重，否则关闭以避免单进程误杀）/ on / off。
    HEAL_DEDUP_ENABLED: str = "auto"

    # 慢查询日志（Phase 3 Step 05）：超过阈值记录 warning。
    SLOW_QUERY_LOG_ENABLED: bool = True
    SLOW_QUERY_THRESHOLD_MS: float = 200.0

    model_config = SettingsConfigDict(
        # V2 安装器将 .env 放到 data/.env，并通过 ENV_FILE 环境变量指定路径；
        # 未指定时保持向后兼容（工作目录下的 .env）。
        env_file=os.getenv("ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",  # 忽略未定义的环境变量，避免启动失败
    )


# 全局单例配置对象，供其他模块导入使用
settings = Settings()
