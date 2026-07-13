from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .jm_service import DownloadCoordinator, JmSettings

CHAPTER_SELECTION_PATTERN = (
    r"^\s*\d+(?:\s*[-~～－]\s*\d+)?"
    r"(?:\s*[,，]\s*\d+(?:\s*[-~～－]\s*\d+)?)*\s*$"
)
CANCEL_SELECTION_PATTERN = r"^\s*(?:取消|cancel)\s*$"


class JmDownloaderPlugin(Star):
    """通过 JM 车号下载本子并发送加密压缩包。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.plugin_data_dir = Path(StarTools.get_data_dir(self.name))
        self.settings = JmSettings.from_config(config, self.plugin_data_dir)
        self.coordinator = DownloadCoordinator(self.settings, logger)

    async def terminate(self):
        await self.coordinator.close()

    @filter.command("本子")
    async def download_album(self, event: AstrMessageEvent, jm_id: str = ""):
        """下载 JM 本子，参数支持 jm123456 或 123456。"""
        result = await self.coordinator.submit(event, jm_id)
        yield event.plain_result(result.message)

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
