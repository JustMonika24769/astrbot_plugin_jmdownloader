import asyncio
import json
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import error_response, file_response, json_response, request

from .config_tools import export_config, validate_config_document
from .jm_service import DownloadCoordinator, JmSettings
from .runtime_store import RuntimeStore

PLUGIN_NAME = "astrbot_plugin_jmdownloader"
PLUGIN_VERSION = "v1.1.0"
MAX_CONFIG_IMPORT_BYTES = 256 * 1024


class _DeleteFileAfterResponse:
    def __init__(self, path: Path):
        self.path = path

    async def __call__(self):
        await asyncio.to_thread(self.path.unlink, missing_ok=True)

CHAPTER_SELECTION_PATTERN = (
    r"^\s*\d+(?:\s*[-~～－]\s*\d+)?"
    r"(?:\s*[,，]\s*\d+(?:\s*[-~～－]\s*\d+)?)*\s*$"
)
CANCEL_SELECTION_PATTERN = r"^\s*(?:取消|cancel)\s*$"


class JmDownloaderPlugin(Star):
    """通过 JM 车号下载本子并发送加密压缩包。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.plugin_data_dir = Path(StarTools.get_data_dir(self.name))
        self.plugin_root = Path(__file__).resolve().parent
        self.config_schema = json.loads(
            (self.plugin_root / "_conf_schema.json").read_text(encoding="utf-8")
        )
        self.store = RuntimeStore(self.plugin_data_dir / "runtime.sqlite3")
        self.settings = JmSettings.from_config(config, self.plugin_data_dir)
        self.coordinator = DownloadCoordinator(self.settings, logger, self.store)
        self._register_web_apis()

    async def terminate(self):
        await self.coordinator.close()

    def _register_web_apis(self):
        routes = (
            ("overview", self.web_overview, ["GET"], "JM downloader overview"),
            ("events", self.web_events, ["GET"], "JM downloader events"),
            ("jobs/cancel", self.web_cancel_job, ["POST"], "Cancel JM job"),
            (
                "domains/check",
                self.web_check_domains,
                ["POST"],
                "Check JM domains",
            ),
            (
                "cache/cleanup",
                self.web_cleanup_cache,
                ["POST"],
                "Clean JM cache",
            ),
            (
                "config/export",
                self.web_export_config,
                ["GET"],
                "Export JM config",
            ),
            (
                "config/validate",
                self.web_validate_config,
                ["POST"],
                "Validate JM config",
            ),
            (
                "config/import",
                self.web_import_config,
                ["POST"],
                "Import JM config",
            ),
        )
        for path, handler, methods, description in routes:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{path}", handler, methods, description
            )

    @filter.command("本子")
    async def download_album(self, event: AstrMessageEvent, jm_id: str = ""):
        """下载 JM 本子，参数支持 jm123456 或 123456。"""
        result = await self.coordinator.submit(event, jm_id)
        yield event.plain_result(result.message)

    @filter.command("本子取消")
    async def cancel_download(self, event: AstrMessageEvent, target: str = ""):
        """管理员取消指定 JM 号或全部下载任务。"""
        if not self._is_download_admin(event):
            yield event.plain_result("只有 AstrBot 管理员或插件配置管理员可以取消任务。")
            return
        if not target:
            yield event.plain_result("用法：/本子取消 <jm号|全部>")
            return
        cancelled = await self.coordinator.cancel_jobs(target)
        if not cancelled:
            yield event.plain_result("没有找到符合条件的活动任务或章节选择。")
            return
        yield event.plain_result(f"已取消 {len(cancelled)} 个任务或待选记录。")

    def _is_download_admin(self, event: AstrMessageEvent) -> bool:
        sender_id = str(event.get_sender_id() or "").strip()
        astrbot_admins = {
            str(item).strip()
            for item in self.context.get_config().get("admins_id", [])
        }
        return bool(
            event.is_admin()
            or sender_id in astrbot_admins
            or sender_id in self.settings.plugin_admin_qq_ids
        )

    async def web_overview(self):
        snapshot = await self.coordinator.snapshot()
        snapshot["limits"] = {
            "daily_downloads": self.settings.user_daily_download_limit,
            "daily_traffic_bytes": self.settings.user_daily_traffic_mb
            * 1024
            * 1024,
            "rate_requests": self.settings.user_rate_limit_requests,
            "rate_window_seconds": self.settings.user_rate_limit_window_seconds,
        }
        snapshot["version"] = PLUGIN_VERSION
        return json_response(snapshot)

    async def web_events(self):
        limit = request.query.get("limit", 50, type=int)
        return json_response({"events": self.store.list_events(limit)})

    async def web_cancel_job(self):
        payload = await request.json(default={})
        target = str(payload.get("target", "") if isinstance(payload, dict) else "")
        if not target:
            return error_response("请提供需要取消的 JM 号或“全部”。", status_code=400)
        cancelled = await self.coordinator.cancel_jobs(target)
        if not cancelled:
            return error_response("没有找到符合条件的活动任务。", status_code=404)
        return json_response({"cancelled": cancelled})

    async def web_check_domains(self):
        domains = await self.coordinator.run_domain_health_check()
        return json_response({"domains": domains})

    async def web_cleanup_cache(self):
        return json_response(await self.coordinator.run_cache_cleanup())

    async def web_export_config(self):
        include_secrets = str(
            request.query.get("include_secrets", "false")
        ).lower() in {"1", "true", "yes"}
        document = export_config(
            dict(self.config), PLUGIN_VERSION, include_secrets=include_secrets
        )
        export_dir = self.plugin_data_dir / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        export_path = export_dir / f"config-{time.time_ns()}.json"
        export_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._prune_config_exports(export_dir)
        response = file_response(
            export_path,
            filename=f"jmdownloader-config-{PLUGIN_VERSION}.json",
            content_type="application/json",
        )
        response.background = _DeleteFileAfterResponse(export_path)
        return response

    async def web_validate_config(self):
        try:
            document = await self._read_config_document()
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        result = validate_config_document(
            document, dict(self.config), self.config_schema
        )
        return json_response(
            {
                "valid": result.valid,
                "errors": list(result.errors),
                "warnings": list(result.warnings),
            }
        )

    async def web_import_config(self):
        try:
            document = await self._read_config_document()
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        result = validate_config_document(
            document, dict(self.config), self.config_schema
        )
        if not result.valid:
            return error_response(
                "配置验证失败：" + "；".join(result.errors), status_code=400
            )
        snapshot = await self.coordinator.snapshot()
        if snapshot["active_jobs"] > 0:
            return error_response("存在活动任务，请完成或取消任务后再导入配置。", status_code=409)
        await self.coordinator.close()
        self.config.clear()
        self.config.update(result.config)
        self.config.save_config()
        self.settings = JmSettings.from_config(self.config, self.plugin_data_dir)
        self.coordinator = DownloadCoordinator(self.settings, logger, self.store)
        await self.coordinator.start()
        self.store.record_event(
            "config_import",
            success=True,
            details={"warnings": list(result.warnings), "username": request.username},
        )
        return json_response(
            {"imported": True, "warnings": list(result.warnings)}
        )

    @staticmethod
    def _prune_config_exports(export_dir: Path):
        exports = sorted(
            export_dir.glob("config-*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in exports[5:]:
            path.unlink(missing_ok=True)

    @staticmethod
    async def _read_config_document():
        body = await request.body()
        if len(body) > MAX_CONFIG_IMPORT_BYTES:
            raise ValueError("配置文件不能超过 256 KB。")
        try:
            return json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("请求内容不是有效的 UTF-8 JSON 配置。") from exc

    @filter.regex(CHAPTER_SELECTION_PATTERN)
    async def select_chapters(self, event: AstrMessageEvent):
        """处理章节范围选择，例如 1-3,5。"""
        result = await self.coordinator.select_chapters(
            event, event.get_message_str()
        )
        if result is not None:
            if result.accepted:
                await self._recall_selection_message(event)
            yield event.plain_result(result.message)

    async def _recall_selection_message(self, event: AstrMessageEvent):
        message_id = _get_recall_message_id(event)
        if not message_id:
            logger.warning("章节选择已受理，但当前事件没有消息 ID，无法撤回。")
            return

        try:
            if event.get_platform_name() == "aiocqhttp":
                bot = getattr(event, "bot", None)
                if bot is None:
                    raise RuntimeError("未找到 aiocqhttp bot 实例")
                skip_reason = _onebot_recall_skip_reason(event)
                if skip_reason:
                    logger.info(f"章节选择消息未撤回：{skip_reason}")
                    return
                await bot.call_action("delete_msg", message_id=message_id)
                return

            raw_message = getattr(event.message_obj, "raw_message", None)
            delete_message = getattr(raw_message, "delete", None)
            if callable(delete_message):
                await delete_message()
                return

            logger.warning(
                f"章节选择已受理，但平台 {event.get_platform_name()} 不支持插件撤回消息。"
            )
        except Exception as exc:
            error_text = str(exc)
            if "decode failed" in error_text or "retcode=1200" in error_text:
                logger.warning(
                    "撤回章节选择消息失败：NapCat 返回权限相关错误。请确认机器人是"
                    "群管理员，且选择消息不是由群主或其他管理员发送；下载任务不受影响。"
                )
            else:
                logger.warning(f"撤回章节选择消息失败，不影响下载任务：{exc}")

    @filter.regex(CANCEL_SELECTION_PATTERN)
    async def cancel_chapter_selection(self, event: AstrMessageEvent):
        """取消等待中的章节选择。"""
        result = await self.coordinator.cancel_selection(event)
        if result is not None:
            yield event.plain_result(result)


def _get_recall_message_id(event: AstrMessageEvent) -> int | str | None:
    message_obj = getattr(event, "message_obj", None)
    raw_message = getattr(message_obj, "raw_message", None)
    raw_message_id = None
    if hasattr(raw_message, "get"):
        raw_message_id = raw_message.get("message_id")
    if raw_message_id is None:
        raw_message_id = getattr(raw_message, "message_id", None)

    message_id = raw_message_id or getattr(message_obj, "message_id", None)
    if isinstance(message_id, int):
        return message_id
    normalized = str(message_id or "").strip()
    if not normalized:
        return None
    return int(normalized) if normalized.isdigit() else normalized


def _onebot_recall_skip_reason(event: AstrMessageEvent) -> str | None:
    if not event.get_group_id():
        return "QQ 不允许机器人撤回对方发送的私聊消息。"

    raw_message = getattr(event.message_obj, "raw_message", None)
    sender = raw_message.get("sender", {}) if hasattr(raw_message, "get") else {}
    role = str(sender.get("role", "") or "").lower()
    if role == "owner":
        return "选择消息由群主发送，机器人无权撤回。"
    if role == "admin":
        return "选择消息由群管理员发送，同级管理员无权撤回。"
    return None
