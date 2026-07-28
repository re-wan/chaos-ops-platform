"""ChaosOps Server 结构化日志配置与轮转。"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.core.config import settings


LOG_FORMAT = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging() -> logging.Logger:
    """配置并返回根日志记录器。

    - 控制台输出：INFO 及以上
    - 文件输出：DEBUG 及以上，按大小轮转（默认 10MB，保留 7 个）
    """
    logger = logging.getLogger("chaosops")
    logger.setLevel(logging.DEBUG)

    # 避免重复添加 handler（如 lifespan 被多次触发）
    if logger.handlers:
        return logger

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    # 控制台 handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 文件 handler（带轮转）
    log_dir = Path(settings.LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "chaosops-server.log"

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=settings.LOG_MAX_BYTES,
        backupCount=settings.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def get_logger(name: str) -> logging.Logger:
    """获取指定模块的日志记录器。"""
    return logging.getLogger(f"chaosops.{name}")
