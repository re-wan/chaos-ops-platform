"""Agent 自动更新模块。

提供版本检查、下载、校验、升级、失败回滚能力。
"""

import hashlib
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("agent.updater")

DEFAULT_BACKUP_COUNT = 2
HEALTH_CHECK_TIMEOUT_SECONDS = 60
# 允许使用明文 HTTP 下载更新包的主机（仅本地开发/测试）；其余一律强制 HTTPS，
# 防止更新包被中间人篡改（与 Server 端 COOKIE_SECURE/PUBLIC_SERVER_URL 联动）。
_HTTP_DOWNLOAD_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}


class UpdateError(Exception):
    """更新过程中发生的错误。"""


class UpdateManager:
    """Agent 更新管理器。

    负责检查版本、下载更新包、校验、升级与失败回滚。
    """

    def __init__(
        self,
        server_url: str,
        agent_token: str,
        current_version: str,
        install_dir: Path,
        config_dir: Path,
        channel: str = "stable",
        mode: str = "manual",
        http_client: Optional[httpx.Client] = None,
    ):
        self.server_url = server_url.rstrip("/")
        self.agent_token = agent_token
        self.current_version = current_version
        self.install_dir = install_dir.resolve()
        self.config_dir = config_dir.resolve()
        self.channel = channel
        self.mode = mode
        self.client = http_client or httpx.Client(timeout=30.0)
        self.headers = {"Authorization": f"Bearer {agent_token}"}
        self.versions_dir = self.install_dir / "versions"
        self.versions_dir.mkdir(parents=True, exist_ok=True)

    def check_for_update(self) -> Optional[dict]:
        """向 Server 查询最新版本信息。

        Returns:
            有更新时返回包含 latest/download_url/checksum 的字典，否则 None。
        """
        url = f"{self.server_url}/api/v1/agents/version"
        params = {"current": self.current_version, "channel": self.channel}
        try:
            response = self.client.get(url, params=params, headers=self.headers)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            logger.warning(f"检查更新失败: {exc}")
            return None

        latest = data.get("latest")
        if not latest:
            logger.info("没有可用的新版本")
            return None

        if latest == self.current_version:
            logger.info(f"当前已是最新版本: {self.current_version}")
            return None

        logger.info(f"发现新版本: {latest}")
        return data

    def download_update(self, version_info: dict) -> str:
        """下载更新包到临时目录。

        Returns:
            下载后的本地文件路径。
        """
        download_url = version_info.get("download_url")
        if not download_url:
            raise UpdateError("版本信息缺少 download_url")

        # 支持相对路径
        if download_url.startswith("/"):
            download_url = f"{self.server_url}{download_url}"

        # 强制 HTTPS（仅本地回环允许明文 HTTP，便于开发/测试）
        parsed = urlparse(download_url)
        if parsed.scheme == "http" and (parsed.hostname or "").lower() not in _HTTP_DOWNLOAD_ALLOWED_HOSTS:
            raise UpdateError(f"更新包必须使用 HTTPS 下载: {download_url}")
        if parsed.scheme not in {"http", "https"}:
            raise UpdateError(f"不支持的下载协议: {parsed.scheme}")

        filename = Path(download_url).name
        temp_dir = Path(tempfile.mkdtemp(prefix="chaosops_update_"))
        local_path = temp_dir / filename

        logger.info(f"开始下载更新包: {download_url}")
        try:
            with self.client.stream("GET", download_url, headers=self.headers) as response:
                response.raise_for_status()
                with open(local_path, "wb") as f:
                    for chunk in response.iter_bytes(chunk_size=8192):
                        f.write(chunk)
        except Exception:
            # 下载任一环节失败必须清理临时目录，避免 /tmp 残留半成品；
            # 成功路径的 temp dir 由调用方 apply_update 消费，不在此清理。
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

        logger.info(f"更新包已下载: {local_path}")
        return str(local_path)

    def verify_checksum(self, filepath: str, expected: str) -> bool:
        """校验文件 SHA256。

        Args:
            filepath: 本地文件路径
            expected: 期望的 SHA256 值（支持前缀 sha256:）
        Returns:
            校验是否通过
        """
        expected = expected.lower().removeprefix("sha256:")
        sha256 = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        actual = sha256.hexdigest()
        logger.info(f"SHA256 校验: actual={actual}, expected={expected}")
        return actual == expected

    def _backup_current_version(self, backup_name: str) -> Path:
        """备份当前版本到 versions 目录。"""
        backup_path = self.versions_dir / backup_name
        if backup_path.exists():
            shutil.rmtree(backup_path)
        shutil.copytree(self.install_dir, backup_path, ignore=shutil.ignore_patterns("versions", ".venv", "__pycache__"))
        logger.info(f"已备份当前版本到: {backup_path}")
        return backup_path

    def _cleanup_old_backups(self) -> None:
        """保留最近的 DEFAULT_BACKUP_COUNT 个备份。"""
        backups = sorted(
            [d for d in self.versions_dir.iterdir() if d.is_dir()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in backups[DEFAULT_BACKUP_COUNT:]:
            shutil.rmtree(old)
            logger.info(f"清理旧备份: {old}")

    def apply_update(self, package_path: str) -> bool:
        """应用更新包：备份、解压到 staging、原子切换、健康检查。

        Args:
            package_path: 更新包本地路径
        Returns:
            升级是否成功
        """
        logger.info(f"开始应用更新: {package_path}")
        timestamp = int(time.time())
        backup_name = f"backup_{self.current_version}_{timestamp}"
        self._backup_current_version(backup_name)

        staging_dir: Optional[Path] = None
        try:
            # 1) 解压到临时 staging 目录（带 zip-slip / 链接 / 绝对路径校验）
            staging_dir = self._extract_package(package_path)
            # 2) 校验通过后用原子 os.replace 切入 install_dir
            self._apply_staged(staging_dir)

            # 3) 健康检查：60 秒内成功心跳
            if not self._health_check():
                logger.error("升级后健康检查失败，准备回滚")
                self.rollback()
                return False

            self._cleanup_old_backups()
            logger.info("升级成功")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"升级过程中出现异常: {exc}")
            self.rollback()
            return False
        finally:
            if staging_dir is not None:
                shutil.rmtree(staging_dir, ignore_errors=True)

    def _validate_member(self, member: tarfile.TarInfo, staging_dir: Path) -> None:
        """校验单个 tar 条目安全：拒绝链接、绝对路径、.. 与越界路径。"""
        name = member.name
        if member.issym() or member.islnk():
            raise UpdateError(f"更新包包含不允许的链接条目: {name}")
        if os.path.isabs(name) or name.startswith("/"):
            raise UpdateError(f"更新包包含绝对路径: {name}")
        if any(part == ".." for part in Path(name).parts):
            raise UpdateError(f"更新包包含路径穿越: {name}")
        # 用 relative_to 严格校验目标不越界（修复 startswith 可被相似前缀绕过）
        target = (staging_dir / name).resolve()
        try:
            target.relative_to(staging_dir.resolve())
        except ValueError as exc:
            raise UpdateError(f"更新包包含非法路径: {name}") from exc

    def _extract_package(self, package_path: str) -> Path:
        """校验并解压 tar.gz 更新包到**临时 staging 目录**（不在 install_dir 内）。

        采用 zip-slip 防护：拒绝符号/硬链接、绝对路径、含 ``..`` 的路径，并用
        ``Path.relative_to`` 严格校验目标不越界；先解压到 staging，校验通过后由
        ``_apply_staged`` 用原子 ``os.replace`` 切入 install_dir，避免原地覆盖导致的
        半成品状态。

        Returns:
            staging 目录路径（调用方负责清理）。
        """
        if not tarfile.is_tarfile(package_path):
            raise UpdateError("更新包不是有效的 tar.gz 文件")

        # staging 放在 install_dir 的父目录下，保证与 install_dir 同文件系统，
        # 使后续 os.replace 为原子操作；同时避免被备份/回滚逻辑误纳。
        staging_dir = Path(
            tempfile.mkdtemp(dir=self.install_dir.parent, prefix=".chaosops_staging_")
        )
        try:
            with tarfile.open(package_path, "r:gz") as tar:
                for member in tar.getmembers():
                    self._validate_member(member, staging_dir)
                try:
                    # Python 3.12+：data filter 进一步拒绝特殊条目/危险权限（纵深防御）
                    tar.extractall(path=staging_dir, filter="data")
                except TypeError:
                    # Python <3.12 不支持 filter 参数，已用 _validate_member 手工校验
                    tar.extractall(path=staging_dir)
        except Exception:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise
        logger.info(f"更新包已解压到临时目录: {staging_dir}")
        return staging_dir

    def _apply_staged(self, staging_dir: Path) -> None:
        """将 staging 目录内容用原子 ``os.replace`` 覆盖到 install_dir。

        逐文件递归：目录按需创建，文件用 os.replace 原子替换（同文件系统），
        避免原地解压导致的半成品文件可见。
        """
        staging_root = staging_dir.resolve()
        for src in staging_root.rglob("*"):
            rel = src.relative_to(staging_root)
            target = (self.install_dir / rel).resolve()
            # 防御性校验：目标必须落在 install_dir 内
            try:
                target.relative_to(self.install_dir.resolve())
            except ValueError as exc:
                raise UpdateError(f"安装目标越界: {rel}") from exc
            if src.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, target)
        logger.info(f"已原子切换更新内容到: {self.install_dir}")

    def _health_check(self) -> bool:
        """升级后健康检查：60 秒内成功发送一次心跳。

        实际运行中通过调用 Agent 自身的上报逻辑实现；测试环境可注入。
        """
        logger.info("开始升级后健康检查...")
        deadline = time.monotonic() + HEALTH_CHECK_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._send_heartbeat():
                logger.info("健康检查通过")
                return True
            time.sleep(5)
        logger.error("健康检查超时")
        return False

    def _send_heartbeat(self) -> bool:
        """发送一次心跳到 Server。"""
        try:
            url = f"{self.server_url}/api/v1/agent/tasks/pending"
            response = self.client.get(url, headers=self.headers, timeout=10.0)
            # 任意 2xx 都视为在线
            return response.status_code < 300
        except httpx.HTTPError as exc:
            logger.warning(f"心跳发送失败: {exc}")
            return False

    def rollback(self) -> None:
        """回滚到最近的旧版本：整体替换并删除新版本引入的多余文件。

        先删除 install_dir 中备份不存在的项（失败更新引入的残留，保留
        versions/.venv），再从最近备份整体覆盖恢复，避免残留混合版本。
        """
        backups = sorted(
            [d for d in self.versions_dir.iterdir() if d.is_dir()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not backups:
            logger.error("没有可用的备份，无法回滚")
            raise UpdateError("没有可用的备份，无法回滚")

        latest_backup = backups[0]
        logger.info(f"开始回滚到: {latest_backup}")

        keep = {"versions", ".venv"}
        backup_names = {p.name for p in latest_backup.iterdir()}

        # 1) 删除 install_dir 中备份没有的多余项（新版本引入的残留）
        for item in self.install_dir.iterdir():
            if item.name in keep:
                continue
            if item.name not in backup_names:
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    try:
                        item.unlink()
                    except FileNotFoundError:
                        pass

        # 2) 从备份整体恢复（覆盖式）
        for item in latest_backup.iterdir():
            if item.name in keep:
                continue
            target = self.install_dir / item.name
            if item.is_dir():
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)

        logger.info("回滚完成")

    def verify_signature(self, filepath: str, signature: str) -> bool:
        """离线签名校验（预留接口）。

        当前 Agent 端尚未部署签名公钥基础设施：当版本信息携带 signature 时按
        fail-closed 拒绝（返回 False），避免未签名的包被当作已签名放行。SHA256
        完整性校验始终由 ``verify_checksum`` 承担；完整签名/公钥体系为后续项。
        """
        logger.warning(
            "收到签名字段但 Agent 端尚未配置签名公钥，按 fail-closed 拒绝"
        )
        return False

    def run_update(self, version_info: Optional[dict] = None) -> bool:
        """执行完整更新流程。

        Args:
            version_info: 可选的版本信息；未提供时自动检查。
        Returns:
            更新是否成功
        """
        if version_info is None:
            version_info = self.check_for_update()
        if version_info is None:
            return False

        package_path = self.download_update(version_info)
        expected_checksum = version_info.get("checksum", "")
        if not self.verify_checksum(package_path, expected_checksum):
            logger.error("更新包校验失败，放弃升级")
            return False

        # 离线签名校验（预留接口）：仅当版本信息携带 signature 时强制校验
        signature = version_info.get("signature")
        if signature and not self.verify_signature(package_path, signature):
            logger.error("更新包签名校验失败，放弃升级")
            return False

        return self.apply_update(package_path)

    def check_and_report_task(self) -> Optional[dict]:
        """检查 Server 是否有下发的更新任务，并执行。

        Returns:
            执行的任务信息或 None
        """
        url = f"{self.server_url}/api/v1/agents/update-task"
        try:
            response = self.client.get(url, headers=self.headers)
            response.raise_for_status()
            task = response.json()
        except httpx.HTTPError as exc:
            logger.warning(f"拉取更新任务失败: {exc}")
            return None

        if task is None:
            return None

        task_id = task["task_id"]
        target_version = task["target_version"]
        logger.info(f"收到更新任务 {task_id}，目标版本 {target_version}")

        # 服务端 update-task 已直接附带 checksum 与 download_url，
        # 不再访问 admin 接口（Agent Token 调 admin 会被 401，导致 checksum 为空）
        version_info = {
            "latest": target_version,
            "download_url": task.get("download_url")
            or f"{self.server_url}/api/v1/agents/dist/chaosops-agent-{target_version}.tar.gz",
            "checksum": task.get("checksum") or "",
        }
        if not version_info["checksum"]:
            # fail-closed：无 checksum 不执行更新
            logger.error(f"更新任务 {task_id} 缺少 checksum，按 fail-closed 放弃")
            self._report_task_result(task_id, False, "缺少 checksum")
            return task

        success = self.run_update(version_info)
        self._report_task_result(task_id, success)
        return task

    def _report_task_result(self, task_id: str, success: bool, error_message: Optional[str] = None) -> None:
        """向 Server 上报更新任务结果。"""
        url = f"{self.server_url}/api/v1/agents/update-task/{task_id}/result"
        payload = {"success": success, "error_message": error_message}
        try:
            response = self.client.post(url, json=payload, headers=self.headers)
            response.raise_for_status()
            logger.info(f"更新任务 {task_id} 结果已上报: {success}")
        except httpx.HTTPError as exc:
            logger.warning(f"上报更新任务结果失败: {exc}")

    def close(self) -> None:
        """关闭 HTTP 客户端。"""
        self.client.close()


def restart_agent_service() -> None:
    """重启 Agent 服务（systemd 环境）。"""
    logger.info("尝试重启 Agent 服务...")
    try:
        subprocess.run(["systemctl", "restart", "chaosops-agent"], check=False)
    except FileNotFoundError:
        logger.warning("未找到 systemctl，跳过服务重启")
