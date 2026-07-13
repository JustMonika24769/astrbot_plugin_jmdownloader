from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

JM_ID_RE = re.compile(r"^(?:jm)?([0-9]+)$", re.IGNORECASE)
DEFAULT_DOMAIN = ["jmcomic-zzz.one"]
DEFAULT_DIR_RULE = "Bd_Aid_Pid"
CACHE_FORMAT_VERSION = 5
MIN_ZIP_PASSWORD_LENGTH = 8
CHAPTERS_PER_FORWARD_NODE = 50
MAX_FORWARD_NODES = 99
MAX_CHAPTER_TITLE_LENGTH = 120
MAX_ERROR_SUMMARY_LENGTH = 1000
MAX_JM_ID_DIGITS = 18
MAX_CHAPTER_SELECTION_LENGTH = 4096
MAX_CHAPTER_NUMBER_DIGITS = 9
SENSITIVE_ERROR_RE = re.compile(
    r"(?i)\b(cookies?|authorization|proxy-authorization|avs|remember|password|token)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
URL_USERINFO_RE = re.compile(
    r"(?i)([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^/@\s]+)@"
)
DEFAULT_DOWNLOAD = {
    "cache": True,
    "image": {"suffix": ".jpg"},
    "threading": {"image": 15, "photo": 2},
}


@dataclass(frozen=True)
class SubmitResult:
    message: str


@dataclass(frozen=True)
class ChapterSelectionResult:
    message: str
    accepted: bool


@dataclass
class Requester:
    event: Any
    session: str


@dataclass(frozen=True)
class ChapterInfo:
    number: int
    photo_id: str
    title: str


@dataclass
class PendingSelection:
    jm_id: str
    chapters: tuple[ChapterInfo, ...]
    requester: Requester
    created_at: float


@dataclass
class Job:
    jm_id: str
    selection: tuple[int, ...] | None = None
    requesters: list[Requester] = field(default_factory=list)
    task: asyncio.Task | None = None


@dataclass(frozen=True)
class PdfBuildResult:
    pdf_path: Path
    chapter_count: int
    pdf_pages: int
    chapter_limit: int
    page_limit: int


@dataclass
class JmSettings:
    data_dir: Path
    archive_dir: Path
    download_root: Path
    zip_password: str
    duplicate_cooldown_seconds: int
    max_concurrent_downloads: int
    max_queue_size: int
    max_active_requests_per_session: int
    max_chapters_per_download: int
    max_pages_per_download: int
    chapter_selection_timeout_seconds: int
    client: dict[str, Any]
    dir_rule: dict[str, Any]
    download: dict[str, Any]
    plugins: dict[str, Any]

    @classmethod
    def from_config(cls, config: dict[str, Any], data_dir: Path) -> JmSettings:
        data_dir = data_dir.expanduser().resolve()
        data_dir.mkdir(parents=True, exist_ok=True)

        client = _merge_dict(
            {
                "domain": list(DEFAULT_DOMAIN),
                "impl": "html",
                "retry_times": 5,
                "cookies": "",
                "proxies": "{}",
                "impersonate": "chrome110",
            },
            _mapping(config.get("client")),
        )
        client["domain"] = _parse_domains(client.get("domain"))
        client["cookies"] = _parse_key_value_mapping(client.get("cookies"))
        client["proxies"] = _parse_key_value_mapping(client.get("proxies"))

        dir_rule = _merge_dict(
            {
                "base_dir": "",
                "rule": DEFAULT_DIR_RULE,
                "normalize_zh": None,
            },
            _mapping(config.get("dir_rule")),
        )
        download = _merge_dict(DEFAULT_DOWNLOAD, _mapping(config.get("download")))
        download["image"] = _merge_dict(
            DEFAULT_DOWNLOAD["image"], _mapping(download.get("image"))
        )
        download["threading"] = _merge_dict(
            DEFAULT_DOWNLOAD["threading"], _mapping(download.get("threading"))
        )

        plugins = _mapping(config.get("plugins"))
        archive_dir = _resolve_user_path(
            config.get("archive_dir", ""), data_dir / "archives"
        )
        download_root = _resolve_user_path(
            dir_rule.get("base_dir", ""), data_dir / "downloads"
        )
        archive_dir.mkdir(parents=True, exist_ok=True)
        download_root.mkdir(parents=True, exist_ok=True)

        return cls(
            data_dir=data_dir,
            archive_dir=archive_dir,
            download_root=download_root,
            zip_password=str(config.get("zip_password", "") or ""),
            duplicate_cooldown_seconds=max(
                0, _as_int(config.get("duplicate_cooldown_seconds"), 30)
            ),
            max_concurrent_downloads=max(
                1, _as_int(config.get("max_concurrent_downloads"), 1)
            ),
            max_queue_size=max(1, _as_int(config.get("max_queue_size"), 10)),
            max_active_requests_per_session=max(
                1, _as_int(config.get("max_active_requests_per_session"), 2)
            ),
            max_chapters_per_download=max(
                0, _as_int(config.get("max_chapters_per_download"), 20)
            ),
            max_pages_per_download=max(
                0, _as_int(config.get("max_pages_per_download"), 80)
            ),
            chapter_selection_timeout_seconds=max(
                60, _as_int(config.get("chapter_selection_timeout_seconds"), 600)
            ),
            client=client,
            dir_rule=dir_rule,
            download=download,
            plugins=plugins,
        )


class DownloadCoordinator:
    def __init__(self, settings: JmSettings, logger: Any):
        self.settings = settings
        self.logger = logger
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_downloads)
        self._jobs: dict[str, Job] = {}
        self._pending_selections: dict[str, PendingSelection] = {}
        self._last_delivery: dict[tuple[str, str], float] = {}

    async def submit(self, event: Any, raw_jm_id: str) -> SubmitResult:
        jm_id = normalize_jm_id(raw_jm_id)
        if jm_id is None:
            return SubmitResult("用法：/本子 <jm号>，例如 /本子 jm123456 或 /本子 123456")
        if not self.settings.zip_password:
            return SubmitResult("插件尚未配置压缩包密码，请先在插件配置页填写。")
        if len(self.settings.zip_password) < MIN_ZIP_PASSWORD_LENGTH:
            return SubmitResult(
                f"压缩包密码至少需要 {MIN_ZIP_PASSWORD_LENGTH} 个字符，请在插件配置页修改。"
            )

        session = str(getattr(event, "session", "") or "")
        requester = Requester(event=event, session=session)
        async with self._lock:
            self._prune_state_locked()
            self._pending_selections.pop(session, None)
            job_key = self.job_key(jm_id)
            job = self._jobs.get(job_key)
            if job is not None and job.task is not None and not job.task.done():
                if not any(item.session == session for item in job.requesters):
                    job.requesters.append(requester)
                return SubmitResult(f"JM{jm_id} 正在处理中，完成后会自动发送，请不要重复提交。")

            archive_path = self.archive_path(jm_id)
            if self._is_recently_delivered(session, job_key):
                return SubmitResult(f"JM{jm_id} 刚刚已经发送过，已跳过短时间内的重复请求。")

            admission_error = self._admission_error_locked(session)
            if admission_error is not None:
                return SubmitResult(admission_error)

            job = Job(jm_id=jm_id, requesters=[requester])
            if self.is_valid_cache(archive_path):
                job.task = asyncio.create_task(self._deliver_cached(job, archive_path))
                self._jobs[job_key] = job
                return SubmitResult(f"已找到 JM{jm_id} 的缓存，正在发送压缩包。")

            job.task = asyncio.create_task(self._prepare_and_download(job))
            self._jobs[job_key] = job
            return SubmitResult(f"已收到 JM{jm_id}，正在检查章节数并准备下载。")

    async def select_chapters(
        self, event: Any, raw_selection: str
    ) -> ChapterSelectionResult | None:
        session = str(getattr(event, "session", "") or "")
        async with self._lock:
            pending = self._pending_selections.get(session)
            if pending is None:
                return None

            if (
                time.monotonic() - pending.created_at
                > self.settings.chapter_selection_timeout_seconds
            ):
                self._pending_selections.pop(session, None)
                return ChapterSelectionResult(
                    f"JM{pending.jm_id} 的章节选择已过期，请重新发送 /本子 {pending.jm_id}。",
                    accepted=False,
                )
            self._prune_state_locked(preserve_session=session)

            selection = parse_chapter_selection(raw_selection, len(pending.chapters))
            if selection is None:
                return ChapterSelectionResult(
                    "章节格式不正确，请回复类似 1-3,5 的章节编号。",
                    accepted=False,
                )

            limit = self.settings.max_chapters_per_download
            if limit > 0 and len(selection) > limit:
                return ChapterSelectionResult(
                    f"本次最多选择 {limit} 章，请重新发送更小的章节范围。",
                    accepted=False,
                )

            job = Job(
                jm_id=pending.jm_id,
                selection=selection,
                requesters=[pending.requester],
            )
            job_key = self.job_key(job.jm_id, job.selection)
            active_job = self._jobs.get(job_key)
            if (
                active_job is not None
                and active_job.task is not None
                and not active_job.task.done()
            ):
                if not any(item.session == session for item in active_job.requesters):
                    active_job.requesters.append(pending.requester)
                self._pending_selections.pop(session, None)
                return ChapterSelectionResult(
                    f"JM{job.jm_id} 的相同章节范围正在处理中，完成后会自动发送。",
                    accepted=True,
                )

            archive_path = self.archive_path(job.jm_id, job.selection)
            if self._is_recently_delivered(session, job_key):
                self._pending_selections.pop(session, None)
                return ChapterSelectionResult(
                    f"JM{job.jm_id} 所选章节刚刚已经发送过，已跳过重复请求。",
                    accepted=True,
                )

            admission_error = self._admission_error_locked(session)
            if admission_error is not None:
                return ChapterSelectionResult(admission_error, accepted=False)

            self._pending_selections.pop(session, None)
            if self.is_valid_cache(archive_path):
                job.task = asyncio.create_task(self._deliver_cached(job, archive_path))
                self._jobs[job_key] = job
                return ChapterSelectionResult(
                    f"已找到 JM{job.jm_id} 所选章节的缓存，正在发送压缩包。",
                    accepted=True,
                )

            job.task = asyncio.create_task(self._download_and_deliver(job))
            self._jobs[job_key] = job
            selected_text = format_selection(selection)
            page_limit = self.settings.max_pages_per_download
            page_notice = f"，最多下载前 {page_limit} 页" if page_limit > 0 else ""
            return ChapterSelectionResult(
                message=(
                    f"已选择 JM{job.jm_id} 第 {selected_text} 章{page_notice}，"
                    "开始下载并制作 PDF 压缩包。"
                ),
                accepted=True,
            )

    async def cancel_selection(self, event: Any) -> str | None:
        session = str(getattr(event, "session", "") or "")
        async with self._lock:
            pending = self._pending_selections.pop(session, None)
            self._prune_state_locked()
        if pending is None:
            return None
        return f"已取消 JM{pending.jm_id} 的章节选择。"

    async def close(self):
        tasks = [job.task for job in self._jobs.values() if job.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._jobs.clear()
        self._pending_selections.clear()

    def job_key(self, jm_id: str, selection: tuple[int, ...] | None = None) -> str:
        if selection is None:
            return jm_id
        return f"{jm_id}:{format_selection(selection)}"

    def archive_path(
        self, jm_id: str, selection: tuple[int, ...] | None = None
    ) -> Path:
        if selection is None:
            return self.settings.archive_dir / f"JM{jm_id}.zip"
        selection_key = format_selection(selection).replace(",", "_")
        if len(selection_key) > 80:
            selection_key = hashlib.sha256(
                selection_key.encode("ascii")
            ).hexdigest()[:16]
        return self.settings.archive_dir / f"JM{jm_id}-chapters-{selection_key}.zip"

    def working_root(
        self, jm_id: str, selection: tuple[int, ...] | None = None
    ) -> Path:
        if selection is None:
            return self.settings.download_root / f".job-{jm_id}"
        selection_hash = hashlib.sha256(
            format_selection(selection).encode("ascii")
        ).hexdigest()[:16]
        return self.settings.download_root / f".job-{jm_id}-{selection_hash}"

    def is_valid_cache(self, archive_path: Path) -> bool:
        if not archive_path.is_file() or archive_path.stat().st_size == 0:
            return False

        manifest = self._read_cache_manifest(archive_path)
        if manifest is None:
            return False
        identity = self._cache_identity(manifest, archive_path)
        if identity is None:
            return False
        jm_id, selection = identity
        if not self._cache_limits_are_valid(manifest, selection):
            return False
        return self._archive_accepts_password(archive_path, f"{jm_id}.pdf")

    def _read_cache_manifest(self, archive_path: Path) -> dict[str, Any] | None:
        manifest_path = self.manifest_path(archive_path)
        if not manifest_path.is_file():
            return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return manifest if isinstance(manifest, dict) else None

    def _cache_identity(
        self, manifest: dict[str, Any], archive_path: Path
    ) -> tuple[str, tuple[int, ...] | None] | None:
        if manifest.get("archive") != archive_path.name:
            return None
        jm_id = normalize_jm_id(manifest.get("jm_id"))
        if jm_id is None:
            return None
        try:
            selection = _parse_manifest_selection(manifest.get("selection"))
        except ValueError:
            return None
        if archive_path != self.archive_path(jm_id, selection):
            return None
        return jm_id, selection

    def _cache_limits_are_valid(
        self, manifest: dict[str, Any], selection: tuple[int, ...] | None
    ) -> bool:
        if (
            manifest.get("max_pages_per_download")
            != self.settings.max_pages_per_download
        ):
            return False
        if (
            manifest.get("max_chapters_per_download")
            != self.settings.max_chapters_per_download
        ):
            return False
        if manifest.get("cache_format_version") != CACHE_FORMAT_VERSION:
            return False
        chapter_count = _as_int(manifest.get("chapter_count"), 0)
        pdf_pages = _as_int(manifest.get("pdf_pages"), 0)
        if chapter_count <= 0 or pdf_pages <= 0:
            return False
        chapter_limit = self.settings.max_chapters_per_download
        if chapter_limit > 0 and chapter_count > chapter_limit:
            return False
        if selection is not None and chapter_count > len(selection):
            return False
        page_limit = self.settings.max_pages_per_download
        return not (page_limit > 0 and pdf_pages > page_limit)

    def _archive_accepts_password(
        self, archive_path: Path, expected_pdf_name: str | None = None
    ) -> bool:
        """确认缓存确实加密，并且当前配置密码可以读取其内容。"""
        try:
            import pyzipper

            with pyzipper.AESZipFile(archive_path, "r") as archive:
                infos = archive.infolist()
                if len(infos) != 1:
                    return False
                info = infos[0]
                member_name = Path(info.filename)
                if (
                    info.is_dir()
                    or member_name.name != info.filename
                    or member_name.suffix.lower() != ".pdf"
                    or (
                        expected_pdf_name is not None
                        and info.filename != expected_pdf_name
                    )
                    or not (info.flag_bits & 0x1)
                ):
                    return False
                archive.setpassword(self.settings.zip_password.encode("utf-8"))
                with archive.open(info) as member:
                    if member.read(5) != b"%PDF-":
                        return False
            return True
        except Exception:
            return False

    async def _deliver_cached(self, job: Job, archive_path: Path):
        try:
            await self._send_to_requesters(job, archive_path)
        finally:
            await self._remove_job(self.job_key(job.jm_id, job.selection))

    async def _prepare_and_download(self, job: Job):
        try:
            if self.settings.max_chapters_per_download <= 0:
                await self._download_and_deliver(job)
                return

            chapters = await asyncio.to_thread(self.inspect_album, job.jm_id)
            if len(chapters) <= self.settings.max_chapters_per_download:
                await self._download_and_deliver(job)
                return

            pending_items: list[PendingSelection] = []
            async with self._lock:
                self._jobs.pop(self.job_key(job.jm_id, job.selection), None)
                for requester in job.requesters:
                    pending = PendingSelection(
                        jm_id=job.jm_id,
                        chapters=tuple(chapters),
                        requester=requester,
                        created_at=time.monotonic(),
                    )
                    self._pending_selections[requester.session] = pending
                    pending_items.append(pending)

            for pending in pending_items:
                await self._send_selection_prompt(pending)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log_error(f"JM{job.jm_id} 下载失败", exc)
            await self._notify_error(
                job,
                f"JM{job.jm_id} 处理失败，请联系管理员查看 AstrBot 日志。",
            )
        finally:
            await self._remove_job(self.job_key(job.jm_id, job.selection))

    async def _download_and_deliver(self, job: Job):
        archive_path = self.archive_path(job.jm_id, job.selection)
        try:
            async with self._semaphore:
                await asyncio.to_thread(
                    self.build_archive, job.jm_id, archive_path, job.selection
                )
            await self._send_to_requesters(job, archive_path)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log_error(f"JM{job.jm_id} 下载失败", exc)
            await self._notify_error(
                job,
                f"JM{job.jm_id} 处理失败，请联系管理员查看 AstrBot 日志。",
            )
        finally:
            await self._remove_job(self.job_key(job.jm_id, job.selection))

    async def _send_selection_prompt(self, pending: PendingSelection):
        limit = self.settings.max_chapters_per_download
        intro_lines = [
            f"JM{pending.jm_id} 共 {len(pending.chapters)} 章，超过单次下载上限 {limit} 章。",
            (
                f"请在 {self.settings.chapter_selection_timeout_seconds // 60} 分钟内"
                "回复章节编号，例如：1-3,5。"
            ),
            "回复“取消”可以放弃本次选择。",
            "可选章节：",
        ]
        page_limit = self.settings.max_pages_per_download
        if page_limit > 0:
            intro_lines.insert(
                2,
                f"所选章节将按编号顺序下载，累计最多 {page_limit} 页，达到上限后停止。",
            )
        chapter_lines = [
            f"{chapter.number}. {_truncate_text(chapter.title or chapter.photo_id)}"
            for chapter in pending.chapters
        ]
        max_chapter_lines = CHAPTERS_PER_FORWARD_NODE * (MAX_FORWARD_NODES - 1)
        if len(chapter_lines) > max_chapter_lines:
            intro_lines.append(
                f"章节过多，仅展示前 {max_chapter_lines} 章；仍可直接输入后续章节编号。"
            )
            chapter_lines = chapter_lines[:max_chapter_lines]
        lines = [*intro_lines, *chapter_lines]
        try:
            event = pending.requester.event
            if event.get_platform_name() == "aiocqhttp":
                try:
                    await self._send_selection_forward(
                        event, intro_lines, chapter_lines
                    )
                except Exception as exc:
                    self._log_error(
                        f"发送 JM{pending.jm_id} 合并转发失败，改用普通消息",
                        exc,
                    )
                    await event.send(event.plain_result("\n".join(lines)))
            else:
                await event.send(event.plain_result("\n".join(lines)))
        except Exception as exc:
            self._log_error(f"发送 JM{pending.jm_id} 章节选择提示失败", exc)

    async def _send_selection_forward(
        self, event: Any, intro_lines: list[str], chapter_lines: list[str]
    ):
        from astrbot.api.event import MessageChain
        from astrbot.api.message_components import Node, Nodes, Plain

        node_kwargs = {
            "uin": event.get_self_id() or "0",
            "name": "JM 漫画下载器",
        }
        nodes = [
            Node(content=[Plain("\n".join(intro_lines))], **node_kwargs),
        ]
        chapter_chunk_size = CHAPTERS_PER_FORWARD_NODE
        for offset in range(0, len(chapter_lines), chapter_chunk_size):
            chapter_chunk = chapter_lines[offset : offset + chapter_chunk_size]
            nodes.append(
                Node(content=[Plain("\n".join(chapter_chunk))], **node_kwargs)
            )
        await event.send(MessageChain([Nodes(nodes)]))

    async def _send_to_requesters(self, job: Job, archive_path: Path):
        for requester in list(job.requesters):
            try:
                await self._send_file_compat(requester.event, archive_path)
            except Exception as exc:
                self._log_error(f"发送 JM{job.jm_id} 文件失败", exc)
                with suppress(Exception):
                    await requester.event.send(
                        requester.event.plain_result(
                            f"JM{job.jm_id} 文件发送失败，请联系管理员查看日志。"
                        )
                    )
                continue
            self._last_delivery[
                (requester.session, self.job_key(job.jm_id, job.selection))
            ] = time.monotonic()

    async def _send_file_compat(self, event: Any, archive_path: Path):
        from astrbot.api.event import MessageChain
        from astrbot.api.message_components import File

        await event.send(
            MessageChain([File(name=archive_path.name, file=str(archive_path))])
        )

    async def _notify_error(self, job: Job, message: str):
        for requester in list(job.requesters):
            try:
                await requester.event.send(requester.event.plain_result(message))
            except Exception as exc:
                self._log_error(f"发送 JM{job.jm_id} 错误提示失败", exc)

    def _log_error(self, context: str, exc: Exception):
        summary = URL_USERINFO_RE.sub(r"\1***:***@", str(exc))
        summary = SENSITIVE_ERROR_RE.sub(r"\1\2***", summary)
        sensitive_values = [self.settings.zip_password]
        sensitive_values.extend(self.settings.client.get("cookies", {}).values())
        sensitive_values.extend(self.settings.client.get("proxies", {}).values())
        for value in sensitive_values:
            secret = str(value or "")
            if len(secret) >= 4:
                summary = summary.replace(secret, "***")
        summary = _truncate_text(summary, MAX_ERROR_SUMMARY_LENGTH)
        self.logger.error(f"{context}: {type(exc).__name__}: {summary}")

    async def _remove_job(self, jm_id: str):
        async with self._lock:
            self._jobs.pop(jm_id, None)

    def _admission_error_locked(self, session: str) -> str | None:
        active_jobs = [
            job
            for job in self._jobs.values()
            if job.task is not None and not job.task.done()
        ]
        if len(active_jobs) >= self.settings.max_queue_size:
            return (
                f"当前任务队列已满（{self.settings.max_queue_size} 个），"
                "请稍后再试。"
            )
        session_jobs = sum(
            any(requester.session == session for requester in job.requesters)
            for job in active_jobs
        )
        if session_jobs >= self.settings.max_active_requests_per_session:
            return (
                "当前会话已有过多任务，最多同时保留 "
                f"{self.settings.max_active_requests_per_session} 个请求。"
            )
        return None

    def _prune_state_locked(self, preserve_session: str | None = None):
        now = time.monotonic()
        timeout = self.settings.chapter_selection_timeout_seconds
        expired_sessions = [
            session
            for session, pending in self._pending_selections.items()
            if session != preserve_session and now - pending.created_at > timeout
        ]
        for session in expired_sessions:
            self._pending_selections.pop(session, None)

        cooldown = self.settings.duplicate_cooldown_seconds
        if cooldown <= 0:
            self._last_delivery.clear()
            return
        expired_deliveries = [
            key
            for key, delivered_at in self._last_delivery.items()
            if now - delivered_at >= cooldown
        ]
        for key in expired_deliveries:
            self._last_delivery.pop(key, None)

    def _is_recently_delivered(self, session: str, job_key: str) -> bool:
        if self.settings.duplicate_cooldown_seconds <= 0:
            return False
        delivered_at = self._last_delivery.get((session, job_key))
        if delivered_at is None:
            return False
        return (
            time.monotonic() - delivered_at
            < self.settings.duplicate_cooldown_seconds
        )

    def manifest_path(self, archive_path: Path) -> Path:
        return archive_path.with_suffix(archive_path.suffix + ".json")

    def inspect_album(self, jm_id: str) -> list[ChapterInfo]:
        jmcomic = _load_jmcomic()
        inspect_root = self.settings.download_root / f".inspect-{jm_id}"
        inspect_root.mkdir(parents=True, exist_ok=True)
        try:
            option = jmcomic.JmOption.construct(
                self._build_option_config(inspect_root)
            )
            album = option.build_jm_client().get_album_detail(jm_id)
            return [
                ChapterInfo(
                    number=index,
                    photo_id=str(getattr(photo, "id", "")),
                    title=str(getattr(photo, "name", "") or "").strip(),
                )
                for index, photo in enumerate(album, 1)
            ]
        finally:
            shutil.rmtree(inspect_root, ignore_errors=True)

    def build_archive(
        self,
        jm_id: str,
        archive_path: Path,
        selection: tuple[int, ...] | None = None,
    ):
        """同步执行 JM 下载、PDF 合并和 AES ZIP，供 to_thread 调用。"""
        jmcomic = _load_jmcomic()
        job_root = self.working_root(jm_id, selection)
        pdf_dir = job_root / "pdf"
        work_archive = archive_path.with_suffix(".partial.zip")
        work_manifest = self.manifest_path(work_archive)
        shutil.rmtree(job_root, ignore_errors=True)
        pdf_dir.mkdir(parents=True, exist_ok=True)
        work_archive.unlink(missing_ok=True)
        work_manifest.unlink(missing_ok=True)

        try:
            result = self._download_to_pdf(
                jmcomic,
                job_root,
                pdf_dir,
                jm_id,
                selection,
            )
            self._write_encrypted_archive(result.pdf_path, work_archive)
            if not self._archive_accepts_password(
                work_archive, result.pdf_path.name
            ):
                raise RuntimeError("生成的 ZIP 未通过密码校验，已停止发送。")

            work_manifest.write_text(
                json.dumps(
                    {
                        "jm_id": jm_id,
                        "selection": format_selection(selection) if selection else None,
                        "chapter_count": result.chapter_count,
                        "pdf_pages": result.pdf_pages,
                        "max_chapters_per_download": result.chapter_limit,
                        "max_pages_per_download": result.page_limit,
                        "cache_format_version": CACHE_FORMAT_VERSION,
                        "archive": archive_path.name,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            work_archive.replace(archive_path)
            work_manifest.replace(self.manifest_path(archive_path))
        finally:
            work_archive.unlink(missing_ok=True)
            work_manifest.unlink(missing_ok=True)
            shutil.rmtree(job_root, ignore_errors=True)

    def _download_to_pdf(
        self,
        jmcomic: Any,
        job_root: Path,
        pdf_dir: Path,
        jm_id: str,
        selection: tuple[int, ...] | None,
    ) -> PdfBuildResult:
        option = jmcomic.JmOption.construct(self._build_option_config(job_root))
        page_limit = self.settings.max_pages_per_download
        chapter_limit = self.settings.max_chapters_per_download
        with jmcomic.new_downloader(option) as downloader:
            album = downloader.client.get_album_detail(jm_id)
            chapter_numbers = selection or tuple(range(1, len(album) + 1))
            self._validate_chapter_numbers(
                jm_id,
                len(album),
                chapter_numbers,
                selection,
                chapter_limit,
            )
            downloader.before_album(album)
            if album.skip:
                raise RuntimeError("JM 下载被 before_album 插件跳过。")

            downloaded_photos = self._download_photos(
                downloader,
                option,
                album,
                chapter_numbers,
                selection,
                page_limit,
            )
            downloader.raise_if_has_exception()
            pdf_path = pdf_dir / f"{jm_id}.pdf"
            pdf_pages = self._write_pdf_from_photos(
                option,
                downloaded_photos,
                pdf_path,
                page_limit,
            )
            downloader.after_album(album)
            downloader.raise_if_has_exception()

        return PdfBuildResult(
            pdf_path=pdf_path,
            chapter_count=len(downloaded_photos),
            pdf_pages=pdf_pages,
            chapter_limit=chapter_limit,
            page_limit=page_limit,
        )

    def _validate_chapter_numbers(
        self,
        jm_id: str,
        album_size: int,
        chapter_numbers: tuple[int, ...],
        selection: tuple[int, ...] | None,
        chapter_limit: int,
    ):
        if any(number < 1 or number > album_size for number in chapter_numbers):
            raise RuntimeError("所选章节编号超出本子实际章节范围。")
        if chapter_limit <= 0 or len(chapter_numbers) <= chapter_limit:
            return
        if selection is None:
            raise RuntimeError(
                f"JM{jm_id} 共 {album_size} 章，超过单次下载上限 "
                f"{chapter_limit} 章，必须先选择章节。"
            )
        raise RuntimeError(
            f"本次选择了 {len(chapter_numbers)} 章，超过上限 {chapter_limit} 章。"
        )

    def _download_photos(
        self,
        downloader: Any,
        option: Any,
        album: Any,
        chapter_numbers: tuple[int, ...],
        selection: tuple[int, ...] | None,
        page_limit: int,
    ) -> list[Any]:
        if selection is None and page_limit <= 0:
            photos = [album[number - 1] for number in chapter_numbers]
            downloader.execute_on_condition(
                iter_objs=photos,
                apply=downloader.download_by_photo_detail,
                count_batch=option.decide_photo_batch_count(album),
            )
            return photos

        photos: list[Any] = []
        downloaded_pages = 0
        for chapter_number in chapter_numbers:
            photo = album[chapter_number - 1]
            if page_limit > 0:
                downloader.client.check_photo(photo)
                remaining_pages = page_limit - downloaded_pages
                if remaining_pages <= 0:
                    break
                photo.page_arr = list((photo.page_arr or [])[:remaining_pages])
                if not photo.page_arr:
                    continue
                self._download_limited_photo(downloader, photo)
            else:
                downloader.download_by_photo_detail(photo)
            if not photo.page_arr:
                continue
            photos.append(photo)
            downloaded_pages += len(photo.page_arr)
        return photos

    def _write_encrypted_archive(self, pdf_path: Path, archive_path: Path):
        import pyzipper

        archive_path.parent.mkdir(parents=True, exist_ok=True)
        with pyzipper.AESZipFile(
            archive_path,
            "w",
            compression=pyzipper.ZIP_DEFLATED,
        ) as archive:
            archive.setencryption(pyzipper.WZ_AES, nbits=256)
            archive.setpassword(self.settings.zip_password.encode("utf-8"))
            archive.write(pdf_path, arcname=pdf_path.name)

    def _download_limited_photo(self, downloader: Any, photo: Any):
        limited_page_arr = list(photo.page_arr or [])
        downloader.before_photo(photo)
        photo.page_arr = limited_page_arr
        if photo.skip:
            return
        downloader.execute_on_condition(
            iter_objs=photo,
            apply=downloader.download_by_image_detail,
            count_batch=downloader.option.decide_image_batch_count(photo),
        )
        downloader.after_photo(photo)
        photo.page_arr = limited_page_arr

    def _write_pdf_from_photos(
        self,
        option: Any,
        photos: list[Any],
        pdf_path: Path,
        page_limit: int,
    ) -> int:
        import img2pdf

        image_paths: list[Path] = []
        seen_paths: set[Path] = set()
        for photo in photos:
            for image in photo:
                image_path = Path(option.decide_image_filepath(image)).resolve()
                if image_path in seen_paths:
                    raise RuntimeError(
                        f"检测到重复的 PDF 图片路径：{image_path}，请检查 dir_rule。"
                    )
                if not image_path.is_file() or image_path.stat().st_size == 0:
                    raise RuntimeError(f"下载图片不存在或为空：{image_path}")
                seen_paths.add(image_path)
                image_paths.append(image_path)

        if not image_paths:
            raise RuntimeError("没有可用于生成 PDF 的下载图片。")
        if page_limit > 0 and len(image_paths) > page_limit:
            raise RuntimeError(
                f"PDF 输入图片为 {len(image_paths)} 页，超过配置上限 {page_limit} 页。"
            )

        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        with pdf_path.open("wb") as output:
            img2pdf.convert([str(path) for path in image_paths], outputstream=output)
        if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
            raise RuntimeError("img2pdf 未能生成有效 PDF。")
        return len(image_paths)

    def _build_option_config(self, job_root: Path):
        plugins = json.loads(json.dumps(self.settings.plugins))
        plugins.setdefault("valid", "log")
        after_album = plugins.get("after_album")
        if not isinstance(after_album, list):
            after_album = []
        after_album = [
            item
            for item in after_album
            if not (
                isinstance(item, dict)
                and str(item.get("plugin", "")).lower() == "img2pdf"
            )
        ]
        plugins["after_album"] = after_album
        configured_rule = str(
            self.settings.dir_rule.get("rule") or DEFAULT_DIR_RULE
        ).strip()
        safe_rule = ensure_chapter_isolated_dir_rule(configured_rule)

        return {
            "version": "2.1",
            "log": False,
            "dir_rule": {
                "base_dir": str(job_root),
                "rule": safe_rule,
                "normalize_zh": self.settings.dir_rule.get("normalize_zh") or None,
            },
            "client": {
                "cache": None,
                "domain": self.settings.client["domain"],
                "impl": self.settings.client["impl"],
                "retry_times": _as_int(self.settings.client.get("retry_times"), 5),
                "postman": {
                    "meta_data": {
                        "headers": None,
                        "impersonate": self.settings.client.get(
                            "impersonate", "chrome110"
                        ),
                        "proxies": self.settings.client.get("proxies", {}),
                        "cookies": self.settings.client.get("cookies", {}),
                    },
                    "type": "cffi",
                },
            },
            "download": self.settings.download,
            "plugins": plugins,
        }


def ensure_chapter_isolated_dir_rule(rule: str) -> str:
    normalized = str(rule or DEFAULT_DIR_RULE).strip() or DEFAULT_DIR_RULE
    if re.search(r"(?<![A-Za-z0-9])P(?:id|photo_id)(?![A-Za-z0-9])", normalized):
        return normalized
    separator = "/" if "/" in normalized else "_"
    return f"{normalized}{separator}Pid"


def normalize_jm_id(value: Any) -> str | None:
    match = JM_ID_RE.fullmatch(str(value or "").strip())
    if match is None:
        return None
    digits = match.group(1)
    if len(digits) > MAX_JM_ID_DIGITS:
        return None
    normalized = digits.lstrip("0") or "0"
    return normalized if normalized != "0" else None


def parse_chapter_selection(value: str, total: int) -> tuple[int, ...] | None:
    if total <= 0:
        return None
    normalized = (
        str(value or "")
        .replace("，", ",")
        .replace("～", "-")
        .replace("－", "-")
        .replace("~", "-")
    )
    normalized = re.sub(r"\s+", "", normalized)
    if not normalized or len(normalized) > MAX_CHAPTER_SELECTION_LENGTH:
        return None

    selected: set[int] = set()
    for token in normalized.split(","):
        if not token:
            return None
        if "-" in token:
            bounds = token.split("-")
            if (
                len(bounds) != 2
                or not all(bound.isdigit() for bound in bounds)
                or any(len(bound) > MAX_CHAPTER_NUMBER_DIGITS for bound in bounds)
            ):
                return None
            start, end = (int(bound) for bound in bounds)
            if start > end or start < 1 or end > total:
                return None
            selected.update(range(start, end + 1))
        else:
            if not token.isdigit() or len(token) > MAX_CHAPTER_NUMBER_DIGITS:
                return None
            number = int(token)
            if number < 1 or number > total:
                return None
            selected.add(number)

    return tuple(sorted(selected)) or None


def format_selection(selection: tuple[int, ...] | list[int] | set[int]) -> str:
    numbers = sorted(set(selection))
    if not numbers:
        return ""

    blocks: list[str] = []
    start = previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        blocks.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = number
    blocks.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(blocks)


def _load_jmcomic():
    try:
        import jmcomic
    except ImportError as exc:
        raise RuntimeError(
            "未找到已安装的 jmcomic 依赖，请先安装插件 requirements.txt。"
        ) from exc
    return jmcomic


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        import yaml

        parsed = yaml.safe_load(value)
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(base))
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_dict(result[key], value)
        else:
            result[key] = value
    return result


def _parse_domains(value: Any) -> list[str]:
    if isinstance(value, str):
        values = re.split(r"[,;\s]+", value.strip())
    elif isinstance(value, list):
        values = value
    else:
        values = []
    domains: list[str] = []
    for item in values:
        text = str(item).strip()
        if not text:
            continue
        try:
            parsed = urlsplit(text if "://" in text else f"//{text}")
            hostname = parsed.hostname
            port = parsed.port
        except ValueError:
            continue
        if not hostname:
            continue
        try:
            hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError:
            continue
        if (
            not re.fullmatch(r"[a-z0-9.-]+", hostname)
            or ".." in hostname
            or hostname.startswith((".", "-"))
            or hostname.endswith((".", "-"))
        ):
            continue
        domain = f"{hostname}:{port}" if port is not None else hostname
        if domain not in domains:
            domains.append(domain)
    return domains or list(DEFAULT_DOMAIN)


def _parse_key_value_mapping(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(key): str(item) for key, item in value.items()}
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return {str(key): str(item) for key, item in parsed.items()}
    except ValueError:
        pass
    result: dict[str, str] = {}
    for item in re.split(r"[;\n]+", value):
        if "=" not in item:
            continue
        key, item_value = item.split("=", 1)
        if key.strip():
            result[key.strip()] = item_value.strip()
    return result


def _resolve_user_path(value: Any, fallback: Path) -> Path:
    text = str(value or "").strip()
    if not text:
        return fallback
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = fallback.parent / path
    return path.resolve()


def _as_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _parse_manifest_selection(value: Any) -> tuple[int, ...] | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("缓存章节范围格式无效")
    numbers = [int(item) for item in re.findall(r"\d+", value)]
    if not numbers:
        raise ValueError("缓存章节范围缺少编号")
    selection = parse_chapter_selection(value, max(numbers))
    if selection is None or format_selection(selection) != value:
        raise ValueError("缓存章节范围不是规范格式")
    return selection


def _truncate_text(value: Any, limit: int = MAX_CHAPTER_TITLE_LENGTH) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"
