from __future__ import annotations

import asyncio
import concurrent.futures
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

try:
    from .runtime_store import RuntimeStore
except ImportError:  # pragma: no cover - supports direct test imports
    from runtime_store import RuntimeStore

JM_ID_RE = re.compile(r"^(?:jm)?([0-9]+)$", re.IGNORECASE)
DEFAULT_DOMAIN = ["jmcomic-zzz.one"]
DEFAULT_DIR_RULE = "Bd_Aid_Pid"
CACHE_FORMAT_VERSION = 6
MIN_ZIP_PASSWORD_LENGTH = 8
CHAPTERS_PER_FORWARD_NODE = 50
MAX_FORWARD_NODES = 99
MAX_CHAPTER_TITLE_LENGTH = 120
MAX_ERROR_SUMMARY_LENGTH = 1000
MAX_JM_ID_DIGITS = 18
MAX_CHAPTER_SELECTION_LENGTH = 4096
MAX_CHAPTER_NUMBER_DIGITS = 9
MAX_DOMAIN_COUNT = 20
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
    user_id: str = ""


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
    created_at: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class PdfBuildResult:
    pdf_path: Path
    chapter_count: int
    pdf_pages: int
    chapter_limit: int
    page_limit: int
    source_image_bytes: int
    pdf_bytes: int


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
    user_rate_limit_requests: int
    user_rate_limit_window_seconds: int
    user_daily_download_limit: int
    user_daily_traffic_mb: int
    plugin_admin_qq_ids: frozenset[str]
    domain_health_check_enabled: bool
    domain_health_check_interval_minutes: int
    domain_health_check_timeout_seconds: int
    pdf_compression_enabled: bool
    pdf_jpeg_quality: int
    pdf_max_width: int
    cache_max_size_mb: int
    cache_expire_days: int
    cache_cleanup_interval_minutes: int
    structured_log_retention_days: int
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
            user_rate_limit_requests=max(
                0, _as_int(config.get("user_rate_limit_requests"), 5)
            ),
            user_rate_limit_window_seconds=max(
                1, _as_int(config.get("user_rate_limit_window_seconds"), 60)
            ),
            user_daily_download_limit=max(
                0, _as_int(config.get("user_daily_download_limit"), 0)
            ),
            user_daily_traffic_mb=max(
                0, _as_int(config.get("user_daily_traffic_mb"), 0)
            ),
            plugin_admin_qq_ids=frozenset(
                _parse_identifier_list(config.get("plugin_admin_qq_ids"))
            ),
            domain_health_check_enabled=bool(
                config.get("domain_health_check_enabled", True)
            ),
            domain_health_check_interval_minutes=max(
                5, _as_int(config.get("domain_health_check_interval_minutes"), 30)
            ),
            domain_health_check_timeout_seconds=max(
                2, _as_int(config.get("domain_health_check_timeout_seconds"), 10)
            ),
            pdf_compression_enabled=bool(
                config.get("pdf_compression_enabled", False)
            ),
            pdf_jpeg_quality=min(
                95, max(20, _as_int(config.get("pdf_jpeg_quality"), 75))
            ),
            pdf_max_width=max(0, _as_int(config.get("pdf_max_width"), 1600)),
            cache_max_size_mb=max(0, _as_int(config.get("cache_max_size_mb"), 0)),
            cache_expire_days=max(0, _as_int(config.get("cache_expire_days"), 0)),
            cache_cleanup_interval_minutes=max(
                5, _as_int(config.get("cache_cleanup_interval_minutes"), 60)
            ),
            structured_log_retention_days=max(
                1, _as_int(config.get("structured_log_retention_days"), 30)
            ),
            client=client,
            dir_rule=dir_rule,
            download=download,
            plugins=plugins,
        )


class DownloadCoordinator:
    def __init__(
        self,
        settings: JmSettings,
        logger: Any,
        store: RuntimeStore | None = None,
    ):
        self.settings = settings
        self.logger = logger
        self.store = store or RuntimeStore(settings.data_dir / "runtime.sqlite3")
        self._lock = asyncio.Lock()
        self._health_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_downloads)
        self._jobs: dict[str, Job] = {}
        self._pending_selections: dict[str, PendingSelection] = {}
        self._last_delivery: dict[tuple[str, str], float] = {}
        self._background_tasks: list[asyncio.Task] = []
        self._started = False

    async def start(self):
        if self._started:
            return
        self._started = True
        self.store.prune(self.settings.structured_log_retention_days)
        self._background_tasks.append(asyncio.create_task(self._cache_cleanup_loop()))
        if self.settings.domain_health_check_enabled:
            self._background_tasks.append(
                asyncio.create_task(self._domain_health_loop())
            )

    def _submission_error(self, jm_id: str | None) -> str | None:
        if jm_id is None:
            return "用法：/本子 <jm号>，例如 /本子 jm123456 或 /本子 123456"
        if not self.settings.zip_password:
            return "插件尚未配置压缩包密码，请先在插件配置页填写。"
        if len(self.settings.zip_password) < MIN_ZIP_PASSWORD_LENGTH:
            return (
                f"压缩包密码至少需要 {MIN_ZIP_PASSWORD_LENGTH} 个字符，"
                "请在插件配置页修改。"
            )
        return None

    def _rate_limit_error(self, requester: Requester, jm_id: str) -> str | None:
        result = self.store.check_and_record_rate(
            requester.user_id,
            self.settings.user_rate_limit_requests,
            self.settings.user_rate_limit_window_seconds,
        )
        if result.allowed:
            return None
        self.store.record_event(
            "request_rejected",
            level="warning",
            success=False,
            jm_id=jm_id,
            user_id=requester.user_id,
            session=requester.session,
            reason="用户速率限制",
        )
        return result.message

    def _validated_selection(
        self, raw_selection: str, total_chapters: int
    ) -> tuple[tuple[int, ...] | None, str | None]:
        selection = parse_chapter_selection(raw_selection, total_chapters)
        if selection is None:
            return None, "章节格式不正确，请回复类似 1-3,5 的章节编号。"
        limit = self.settings.max_chapters_per_download
        if limit > 0 and len(selection) > limit:
            return None, f"本次最多选择 {limit} 章，请重新发送更小的章节范围。"
        return selection, None

    async def submit(self, event: Any, raw_jm_id: str) -> SubmitResult:
        await self.start()
        jm_id = normalize_jm_id(raw_jm_id)
        config_error = self._submission_error(jm_id)
        if config_error is not None:
            return SubmitResult(config_error)

        session = str(getattr(event, "session", "") or "")
        user_id = _event_user_id(event, session)
        requester = Requester(event=event, session=session, user_id=user_id)
        rate_error = self._rate_limit_error(requester, jm_id)
        if rate_error is not None:
            return SubmitResult(rate_error)
        async with self._lock:
            self._prune_state_locked()
            self._pending_selections.pop(session, None)
            job_key = self.job_key(jm_id)
            job = self._jobs.get(job_key)
            if job is not None and job.task is not None and not job.task.done():
                if not any(item.session == session for item in job.requesters):
                    quota_error = self._user_quota_error_locked(user_id)
                    if quota_error is not None:
                        return SubmitResult(quota_error)
                    job.requesters.append(requester)
                return SubmitResult(f"JM{jm_id} 正在处理中，完成后会自动发送，请不要重复提交。")

            archive_path = self.archive_path(jm_id)
            if self._is_recently_delivered(session, job_key):
                return SubmitResult(f"JM{jm_id} 刚刚已经发送过，已跳过短时间内的重复请求。")

            admission_error = self._admission_error_locked(session, user_id)
            if admission_error is not None:
                self._record_request_rejection(requester, jm_id, admission_error)
                return SubmitResult(admission_error)

            job = Job(jm_id=jm_id, requesters=[requester])
            if self.is_valid_cache(archive_path):
                job.task = asyncio.create_task(self._deliver_cached(job, archive_path))
                self._jobs[job_key] = job
                self._record_request_event(requester, jm_id, "cache")
                return SubmitResult(f"已找到 JM{jm_id} 的缓存，正在发送压缩包。")

            job.task = asyncio.create_task(self._prepare_and_download(job))
            self._jobs[job_key] = job
            self._record_request_event(requester, jm_id, "download")
            return SubmitResult(f"已收到 JM{jm_id}，正在检查章节数并准备下载。")

    async def select_chapters(
        self, event: Any, raw_selection: str
    ) -> ChapterSelectionResult | None:
        await self.start()
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

            selection, selection_error = self._validated_selection(
                raw_selection, len(pending.chapters)
            )
            if selection_error is not None:
                return ChapterSelectionResult(selection_error, accepted=False)
            assert selection is not None

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
                    quota_error = self._user_quota_error_locked(
                        pending.requester.user_id
                    )
                    if quota_error is not None:
                        return ChapterSelectionResult(quota_error, accepted=False)
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

            admission_error = self._admission_error_locked(
                session, pending.requester.user_id
            )
            if admission_error is not None:
                self._record_request_rejection(
                    pending.requester, pending.jm_id, admission_error
                )
                return ChapterSelectionResult(admission_error, accepted=False)

            self._pending_selections.pop(session, None)
            if self.is_valid_cache(archive_path):
                job.task = asyncio.create_task(self._deliver_cached(job, archive_path))
                self._jobs[job_key] = job
                self._record_request_event(
                    pending.requester, job.jm_id, "selected_cache"
                )
                return ChapterSelectionResult(
                    f"已找到 JM{job.jm_id} 所选章节的缓存，正在发送压缩包。",
                    accepted=True,
                )

            job.task = asyncio.create_task(self._download_and_deliver(job))
            self._jobs[job_key] = job
            self._record_request_event(
                pending.requester, job.jm_id, "selected_download"
            )
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
        background_tasks = list(self._background_tasks)
        for task in background_tasks:
            task.cancel()
        tasks = [job.task for job in self._jobs.values() if job.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        self._jobs.clear()
        self._pending_selections.clear()
        self._background_tasks.clear()
        self._started = False

    async def cancel_jobs(self, target: str) -> list[str]:
        normalized = str(target or "").strip().lower()
        cancel_all = normalized in {"all", "全部", "*"}
        jm_id = None if cancel_all else normalize_jm_id(normalized)
        if not cancel_all and jm_id is None:
            return []

        async with self._lock:
            matched = [
                (key, job)
                for key, job in self._jobs.items()
                if cancel_all or job.jm_id == jm_id
            ]
            pending_sessions = [
                session
                for session, pending in self._pending_selections.items()
                if cancel_all or pending.jm_id == jm_id
            ]
            for session in pending_sessions:
                self._pending_selections.pop(session, None)
            for _, job in matched:
                if job.task is not None:
                    job.task.cancel()

        for _, job in matched:
            await self._notify_error(job, f"JM{job.jm_id} 任务已被管理员取消。")
        tasks = [job.task for _, job in matched if job.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        cancelled = [key for key, _ in matched]
        cancelled.extend(f"pending:{session}" for session in pending_sessions)
        self.store.record_event(
            "admin_cancel",
            level="warning",
            success=True,
            jm_id=jm_id,
            details={"target": normalized, "cancelled": cancelled},
        )
        return cancelled

    async def snapshot(self) -> dict[str, Any]:
        await self.start()
        async with self._lock:
            jobs = [
                {
                    "key": key,
                    "jm_id": job.jm_id,
                    "selection": format_selection(job.selection or ()),
                    "requester_count": len(job.requesters),
                    "user_ids": sorted(
                        {item.user_id for item in job.requesters if item.user_id}
                    ),
                    "age_seconds": round(time.monotonic() - job.created_at, 1),
                }
                for key, job in self._jobs.items()
                if job.task is not None and not job.task.done()
            ]
            pending_count = len(self._pending_selections)
        cache = await asyncio.to_thread(self.cache_stats)
        return {
            "jobs": jobs,
            "active_jobs": len(jobs),
            "pending_selections": pending_count,
            "cache": cache,
            "domains": self.store.domain_health(),
            "metrics": self.store.metrics(),
        }

    async def run_domain_health_check(
        self, domains: list[str] | None = None
    ) -> list[dict[str, Any]]:
        async with self._health_lock:
            await asyncio.to_thread(self._run_domain_health_check_sync, domains)
        return self.store.domain_health()

    async def discover_domains(self) -> dict[str, Any]:
        async with self._health_lock:
            discovered = await asyncio.to_thread(self._discover_domains_sync)
            domains = [item["domain"] for item in discovered["candidates"]]
            await asyncio.to_thread(self._run_domain_health_check_sync, domains)
        health = {item["domain"]: item for item in self.store.domain_health()}
        for candidate in discovered["candidates"]:
            candidate.update(health.get(candidate["domain"], {}))
        return discovered

    async def login_once(
        self, username: str, password: str, domain: str
    ) -> dict[str, Any]:
        normalized_domain = _normalize_domain(domain)
        if normalized_domain is None:
            raise ValueError("请选择有效的登录域名。")
        clean_username = str(username or "").strip()
        if not clean_username or not password:
            raise ValueError("账号和密码不能为空。")
        if len(clean_username) > 128 or len(password) > 256:
            raise ValueError("账号或密码长度超出限制。")
        async with self._health_lock:
            return await asyncio.to_thread(
                self._login_once_sync,
                clean_username,
                password,
                normalized_domain,
            )

    async def run_cache_cleanup(self) -> dict[str, Any]:
        async with self._lock:
            protected = {
                self.archive_path(job.jm_id, job.selection).resolve()
                for job in self._jobs.values()
                if job.task is not None and not job.task.done()
            }
        result = await asyncio.to_thread(self._cleanup_cache_sync, protected)
        self.store.record_event(
            "cache_cleanup",
            success=True,
            bytes_count=result["removed_bytes"],
            details=result,
        )
        return result

    async def _domain_health_loop(self):
        while True:
            try:
                await self.run_domain_health_check()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log_error("域名健康检查失败", exc)
            await asyncio.sleep(
                self.settings.domain_health_check_interval_minutes * 60
            )

    async def _cache_cleanup_loop(self):
        while True:
            try:
                await self.run_cache_cleanup()
                self.store.prune(self.settings.structured_log_retention_days)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log_error("缓存清理失败", exc)
            await asyncio.sleep(self.settings.cache_cleanup_interval_minutes * 60)

    def _run_domain_health_check_sync(self, domains: list[str] | None = None):
        jmcomic = _load_jmcomic()
        health_root = self.settings.download_root / ".domain-health"
        shutil.rmtree(health_root, ignore_errors=True)
        health_root.mkdir(parents=True, exist_ok=True)
        try:
            domains = (domains or self.settings.client["domain"])[:MAX_DOMAIN_COUNT]
            if not domains:
                return
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(4, len(domains))
            ) as executor:
                futures = [
                    executor.submit(
                        self._check_domain_health_sync,
                        jmcomic,
                        health_root / hashlib.sha256(domain.encode()).hexdigest()[:12],
                        domain,
                    )
                    for domain in domains
                ]
                for future in futures:
                    (
                        domain,
                        healthy,
                        latency_ms,
                        status_code,
                        error,
                        final_domain,
                        redirect_count,
                    ) = future.result()
                    self.store.record_domain_health(
                        domain,
                        healthy,
                        latency_ms,
                        status_code,
                        error,
                        final_domain,
                        redirect_count,
                    )
                    self.store.record_event(
                        "domain_health",
                        level="info" if healthy else "warning",
                        success=healthy,
                        reason=None if healthy else "域名不可用",
                        duration_ms=latency_ms,
                        details={
                            "domain": domain,
                            "final_domain": final_domain,
                            "status_code": status_code,
                            "redirect_count": redirect_count,
                        },
                    )
        finally:
            shutil.rmtree(health_root, ignore_errors=True)

    def _check_domain_health_sync(
        self, jmcomic: Any, work_root: Path, domain: str
    ) -> tuple[str, bool, int, int | None, str | None, str | None, int]:
        started_at = time.monotonic()
        healthy = False
        status_code = None
        error = None
        final_domain = None
        redirect_count = 0
        try:
            work_root.mkdir(parents=True, exist_ok=True)
            option = jmcomic.JmOption.construct(
                self._build_option_config(
                    work_root,
                    domains=[domain],
                    use_health_order=False,
                )
            )
            client = option.build_jm_client()
            response = client.get(
                "/", timeout=self.settings.domain_health_check_timeout_seconds
            )
            status_code = int(getattr(response, "status_code", 0) or 0)
            content = getattr(response, "content", b"")
            final_domain = _domain_from_url(getattr(response, "url", None))
            redirect_count = int(getattr(response, "redirect_count", 0) or 0)
            healthy = 200 <= status_code < 500 and bool(content)
            if not healthy:
                error = f"HTTP {status_code} 或响应为空"
        except Exception as exc:
            error = self._sanitize_error(exc)
        latency_ms = int((time.monotonic() - started_at) * 1000)
        return (
            domain,
            healthy,
            latency_ms,
            status_code,
            error,
            final_domain,
            redirect_count,
        )

    def _discover_domains_sync(self) -> dict[str, Any]:
        jmcomic = _load_jmcomic()
        work_root = self.settings.download_root / ".domain-discovery"
        shutil.rmtree(work_root, ignore_errors=True)
        work_root.mkdir(parents=True, exist_ok=True)
        sources: dict[str, set[str]] = {}
        errors: list[dict[str, str]] = []

        def add(values: Any, source: str):
            if isinstance(values, str):
                values = [values]
            for value in values or []:
                domain = _normalize_domain(value)
                if domain is None or domain.startswith("jm365"):
                    continue
                sources.setdefault(domain, set()).add(source)

        add(self.settings.client["domain"], "configured")
        try:
            jmcomic.JmModuleConfig.DOMAIN_HTML = None
            jmcomic.JmModuleConfig.DOMAIN_HTML_LIST = None
            option = jmcomic.JmOption.construct(
                self._build_option_config(work_root, use_health_order=False)
            )
            client = option.build_jm_client(impl="html")
            discovery_methods = (
                ("redirect", client.get_html_domain),
                ("publish", client.get_html_domain_all),
                ("github", client.get_html_domain_all_via_github),
            )
            for source, method in discovery_methods:
                try:
                    add(method(), source)
                except Exception as exc:
                    errors.append(
                        {"source": source, "error": self._sanitize_error(exc)}
                    )
        except Exception as exc:
            errors.append(
                {"source": "initialization", "error": self._sanitize_error(exc)}
            )
        finally:
            shutil.rmtree(work_root, ignore_errors=True)

        ordered = sorted(
            sources,
            key=lambda domain: (
                "redirect" not in sources[domain],
                "configured" not in sources[domain],
                domain,
            ),
        )[:MAX_DOMAIN_COUNT]
        return {
            "candidates": [
                {"domain": domain, "sources": sorted(sources[domain])}
                for domain in ordered
            ],
            "errors": errors,
        }

    def _login_once_sync(
        self, username: str, password: str, domain: str
    ) -> dict[str, Any]:
        jmcomic = _load_jmcomic()
        work_root = self.settings.download_root / ".one-time-login"
        shutil.rmtree(work_root, ignore_errors=True)
        work_root.mkdir(parents=True, exist_ok=True)
        try:
            option = jmcomic.JmOption.construct(
                self._build_option_config(
                    work_root,
                    domains=[domain],
                    use_health_order=False,
                    cookies_override={},
                )
            )
            client = option.build_jm_client(impl="html")
            response = client.login(username, password)
            fresh_cookies = dict(client.get_meta_data("cookies", None) or {})
            auth_keys = {"AVS", "remember", "remember_id", "yuo1"}
            if not fresh_cookies or not auth_keys.intersection(fresh_cookies):
                raise RuntimeError("登录响应没有返回有效的登录 Cookie。")
            merged = {
                key: value
                for key, value in self.settings.client.get("cookies", {}).items()
                if key not in auth_keys
            }
            merged.update({str(key): str(value) for key, value in fresh_cookies.items()})
            final_domain = _domain_from_url(getattr(response, "url", None)) or domain
            self.store.record_event(
                "cookie_login",
                success=True,
                details={
                    "domain": domain,
                    "final_domain": final_domain,
                    "cookie_names": sorted(fresh_cookies),
                },
            )
            return {
                "cookies": merged,
                "cookie_names": sorted(fresh_cookies),
                "domain": domain,
                "final_domain": final_domain,
            }
        except Exception as exc:
            summary = self._sanitize_error(exc)
            for secret in (username, password):
                if secret:
                    summary = summary.replace(secret, "***")
            self.logger.warning(
                f"一次性 JM 登录失败：domain={domain}, "
                f"error={type(exc).__name__}: {summary}"
            )
            self.store.record_event(
                "cookie_login",
                level="warning",
                success=False,
                reason="一次性登录失败",
                details={"domain": domain, "exception_type": type(exc).__name__},
            )
            raise RuntimeError("登录失败，请检查账号、密码、域名和网络状态。") from exc
        finally:
            shutil.rmtree(work_root, ignore_errors=True)

    def _ordered_domains(self) -> list[str]:
        configured = list(self.settings.client["domain"])
        if not self.settings.domain_health_check_enabled:
            return configured
        records = {item["domain"]: item for item in self.store.domain_health()}
        stale_after = self.settings.domain_health_check_interval_minutes * 120
        now = time.time()

        def rank(domain: str) -> tuple[int, int, int]:
            item = records.get(domain)
            original_index = configured.index(domain)
            if item is None or now - item["checked_at"] > stale_after:
                return 1, original_index, original_index
            if item["healthy"]:
                return 0, int(item["latency_ms"] or 0), original_index
            return 2, original_index, original_index

        return sorted(configured, key=rank)

    def cache_stats(self) -> dict[str, Any]:
        archives = [
            path
            for path in self.settings.archive_dir.glob("JM*.zip")
            if path.is_file() and not path.name.endswith(".partial.zip")
        ]
        sizes = [path.stat().st_size for path in archives]
        return {
            "files": len(archives),
            "bytes": sum(sizes),
            "max_bytes": self.settings.cache_max_size_mb * 1024 * 1024,
            "expire_days": self.settings.cache_expire_days,
        }

    def _cleanup_cache_sync(self, protected: set[Path]) -> dict[str, Any]:
        now = time.time()
        all_archives = [
            path
            for path in self.settings.archive_dir.glob("JM*.zip")
            if path.is_file()
            and not path.name.endswith(".partial.zip")
        ]
        archives = [path for path in all_archives if path.resolve() not in protected]
        removed: list[str] = []
        removed_bytes = 0

        def remove_archive(path: Path):
            nonlocal removed_bytes
            size = path.stat().st_size if path.exists() else 0
            path.unlink(missing_ok=True)
            self.manifest_path(path).unlink(missing_ok=True)
            removed.append(path.name)
            removed_bytes += size

        expire_days = self.settings.cache_expire_days
        if expire_days > 0:
            cutoff = now - expire_days * 86400
            for path in list(archives):
                if path.stat().st_mtime < cutoff:
                    remove_archive(path)
                    archives.remove(path)

        max_bytes = self.settings.cache_max_size_mb * 1024 * 1024
        total_bytes = self.cache_stats()["bytes"]
        if max_bytes > 0 and total_bytes > max_bytes:
            for path in sorted(archives, key=lambda item: item.stat().st_mtime):
                if total_bytes <= max_bytes:
                    break
                size = path.stat().st_size
                remove_archive(path)
                total_bytes -= size
        return {
            "removed_files": removed,
            "removed_count": len(removed),
            "removed_bytes": removed_bytes,
            "remaining_bytes": self.cache_stats()["bytes"],
        }

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
        compression_enabled = self.settings.pdf_compression_enabled
        if manifest.get("pdf_compression_enabled") != compression_enabled:
            return False
        if compression_enabled and (
            manifest.get("pdf_jpeg_quality") != self.settings.pdf_jpeg_quality
            or manifest.get("pdf_max_width") != self.settings.pdf_max_width
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
            self._record_job_failure(job, "章节检查失败", exc)
            await self._notify_error(
                job,
                f"JM{job.jm_id} 处理失败，请联系管理员查看 AstrBot 日志。",
            )
        finally:
            await self._remove_job(self.job_key(job.jm_id, job.selection))

    async def _download_and_deliver(self, job: Job):
        archive_path = self.archive_path(job.jm_id, job.selection)
        started_at = time.monotonic()
        try:
            async with self._semaphore:
                result = await asyncio.to_thread(
                    self.build_archive, job.jm_id, archive_path, job.selection
                )
            self.store.record_event(
                "archive_created",
                success=True,
                jm_id=job.jm_id,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                bytes_count=archive_path.stat().st_size,
                details={
                    "chapters": result.chapter_count,
                    "pages": result.pdf_pages,
                    "source_image_bytes": result.source_image_bytes,
                    "pdf_bytes": result.pdf_bytes,
                    "compressed": self.settings.pdf_compression_enabled,
                },
            )
            await self._send_to_requesters(job, archive_path)
        except asyncio.CancelledError:
            self.store.record_event(
                "job_cancelled",
                level="warning",
                success=False,
                jm_id=job.jm_id,
                reason="任务被取消",
            )
            raise
        except Exception as exc:
            self._log_error(f"JM{job.jm_id} 下载失败", exc)
            self._record_job_failure(job, "下载或打包失败", exc)
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
        archive_size = await asyncio.to_thread(lambda: archive_path.stat().st_size)
        delivered = False
        for requester in list(job.requesters):
            quota = self.store.reserve_delivery(
                requester.user_id,
                archive_size,
                self.settings.user_daily_download_limit,
                self.settings.user_daily_traffic_mb * 1024 * 1024,
            )
            if not quota.allowed:
                self.store.record_event(
                    "delivery_rejected",
                    level="warning",
                    success=False,
                    jm_id=job.jm_id,
                    user_id=requester.user_id,
                    session=requester.session,
                    reason="用户每日配额",
                    bytes_count=archive_size,
                )
                with suppress(Exception):
                    await requester.event.send(
                        requester.event.plain_result(quota.message)
                    )
                continue
            try:
                await self._send_file_compat(requester.event, archive_path)
            except Exception as exc:
                self.store.release_delivery(requester.user_id, archive_size)
                self._log_error(f"发送 JM{job.jm_id} 文件失败", exc)
                self.store.record_event(
                    "delivery_failed",
                    level="error",
                    success=False,
                    jm_id=job.jm_id,
                    user_id=requester.user_id,
                    session=requester.session,
                    reason="平台文件发送失败",
                    bytes_count=archive_size,
                    details={"error": self._sanitize_error(exc)},
                )
                with suppress(Exception):
                    await requester.event.send(
                        requester.event.plain_result(
                            f"JM{job.jm_id} 文件发送失败，请联系管理员查看日志。"
                        )
                    )
                continue
            self.store.record_event(
                "delivery_succeeded",
                success=True,
                jm_id=job.jm_id,
                user_id=requester.user_id,
                session=requester.session,
                bytes_count=archive_size,
            )
            delivered = True
            self._last_delivery[
                (requester.session, self.job_key(job.jm_id, job.selection))
            ] = time.monotonic()
        if delivered:
            await asyncio.to_thread(self._touch_cache_files, archive_path)

    def _touch_cache_files(self, archive_path: Path):
        archive_path.touch(exist_ok=True)
        manifest = self.manifest_path(archive_path)
        if manifest.exists():
            manifest.touch(exist_ok=True)

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
        summary = self._sanitize_error(exc)
        self.logger.error(f"{context}: {type(exc).__name__}: {summary}")

    def _sanitize_error(self, exc: Exception) -> str:
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
        return summary

    def _record_job_failure(self, job: Job, reason: str, exc: Exception):
        for requester in job.requesters or [Requester(None, "", "")]:
            self.store.record_event(
                "job_failed",
                level="error",
                success=False,
                jm_id=job.jm_id,
                user_id=requester.user_id or None,
                session=requester.session or None,
                reason=reason,
                details={
                    "exception_type": type(exc).__name__,
                    "error": self._sanitize_error(exc),
                },
            )

    def _record_request_event(self, requester: Requester, jm_id: str, mode: str):
        self.store.record_event(
            "request_accepted",
            success=True,
            jm_id=jm_id,
            user_id=requester.user_id,
            session=requester.session,
            details={"mode": mode},
        )

    def _record_request_rejection(
        self, requester: Requester, jm_id: str, message: str
    ):
        self.store.record_event(
            "request_rejected",
            level="warning",
            success=False,
            jm_id=jm_id,
            user_id=requester.user_id,
            session=requester.session,
            reason="任务准入限制",
            details={"message": message},
        )

    async def _remove_job(self, jm_id: str):
        async with self._lock:
            self._jobs.pop(jm_id, None)

    def _admission_error_locked(self, session: str, user_id: str = "") -> str | None:
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
        return self._user_quota_error_locked(user_id, active_jobs)

    def _user_quota_error_locked(
        self, user_id: str, active_jobs: list[Job] | None = None
    ) -> str | None:
        if not user_id:
            return None
        usage = self.store.usage_for(user_id)
        jobs = active_jobs if active_jobs is not None else [
            job
            for job in self._jobs.values()
            if job.task is not None and not job.task.done()
        ]
        in_flight = sum(
            requester.user_id == user_id
            for job in jobs
            for requester in job.requesters
        )
        daily_limit = self.settings.user_daily_download_limit
        if daily_limit > 0 and usage["downloads"] + in_flight >= daily_limit:
            return f"今天最多下载 {daily_limit} 次，请明天再试。"
        byte_limit = self.settings.user_daily_traffic_mb * 1024 * 1024
        if byte_limit > 0 and usage["bytes_sent"] >= byte_limit:
            return (
                f"今天的流量配额已用完（{self.settings.user_daily_traffic_mb} MB），"
                "请明天再试。"
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
            last_error = None
            for index, domain in enumerate(self._ordered_domains()):
                try:
                    option = jmcomic.JmOption.construct(
                        self._build_option_config(
                            inspect_root / f"attempt-{index}", domains=[domain]
                        )
                    )
                    album = option.build_jm_client().get_album_detail(jm_id)
                    if index > 0:
                        self._record_domain_fallback_success(jm_id, domain, "inspect")
                    return [
                        ChapterInfo(
                            number=chapter_index,
                            photo_id=str(getattr(photo, "id", "")),
                            title=str(getattr(photo, "name", "") or "").strip(),
                        )
                        for chapter_index, photo in enumerate(album, 1)
                    ]
                except Exception as exc:
                    if not self._is_retryable_domain_error(exc):
                        raise
                    last_error = exc
                    self._record_domain_attempt_failure(
                        jm_id, domain, "inspect", exc
                    )
            if last_error is not None:
                raise last_error
            raise RuntimeError("没有可用于章节检查的 JM 域名。")
        finally:
            shutil.rmtree(inspect_root, ignore_errors=True)

    def build_archive(
        self,
        jm_id: str,
        archive_path: Path,
        selection: tuple[int, ...] | None = None,
    ) -> PdfBuildResult:
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
                        "pdf_compression_enabled": self.settings.pdf_compression_enabled,
                        "pdf_jpeg_quality": self.settings.pdf_jpeg_quality,
                        "pdf_max_width": self.settings.pdf_max_width,
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
            return result
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
        last_error = None
        for index, domain in enumerate(self._ordered_domains()):
            attempt_root = job_root / f"attempt-{index}"
            shutil.rmtree(pdf_dir, ignore_errors=True)
            pdf_dir.mkdir(parents=True, exist_ok=True)
            try:
                result = self._download_to_pdf_on_domain(
                    jmcomic,
                    attempt_root,
                    pdf_dir,
                    jm_id,
                    selection,
                    domain,
                )
                if index > 0:
                    self._record_domain_fallback_success(jm_id, domain, "download")
                return result
            except Exception as exc:
                if not self._is_retryable_domain_error(exc):
                    raise
                last_error = exc
                self._record_domain_attempt_failure(jm_id, domain, "download", exc)
                shutil.rmtree(attempt_root, ignore_errors=True)
        if last_error is not None:
            raise last_error
        raise RuntimeError("没有可用于下载的 JM 域名。")

    def _download_to_pdf_on_domain(
        self,
        jmcomic: Any,
        attempt_root: Path,
        pdf_dir: Path,
        jm_id: str,
        selection: tuple[int, ...] | None,
        domain: str,
    ) -> PdfBuildResult:
        option = jmcomic.JmOption.construct(
            self._build_option_config(attempt_root, domains=[domain])
        )
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
            pdf_pages, source_image_bytes = self._write_pdf_from_photos(
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
            source_image_bytes=source_image_bytes,
            pdf_bytes=pdf_path.stat().st_size,
        )

    @staticmethod
    def _is_retryable_domain_error(exc: Exception) -> bool:
        return type(exc).__name__ in {
            "MissingAlbumPhotoException",
            "RequestRetryAllFailException",
        }

    def _record_domain_attempt_failure(
        self, jm_id: str, domain: str, stage: str, exc: Exception
    ):
        details = self._response_diagnostic(exc)
        details.update(
            {
                "candidate_domain": domain,
                "stage": stage,
                "exception_type": type(exc).__name__,
            }
        )
        self.logger.warning(
            f"JM{jm_id} {stage} 域名尝试失败，将切换候选域名："
            f"candidate={domain}, final={details.get('final_domain') or '-'}, "
            f"status={details.get('status_code') or '-'}, "
            f"redirects={details.get('redirect_count') or 0}, "
            f"error={type(exc).__name__}"
        )
        self.store.record_event(
            "domain_album_retry",
            level="warning",
            success=False,
            jm_id=jm_id,
            reason="候选域名无法访问该本子",
            details=details,
        )

    def _record_domain_fallback_success(self, jm_id: str, domain: str, stage: str):
        self.store.record_event(
            "domain_fallback_succeeded",
            success=True,
            jm_id=jm_id,
            details={"domain": domain, "stage": stage},
        )

    @staticmethod
    def _response_diagnostic(exc: Exception) -> dict[str, Any]:
        response = getattr(exc, "resp", None)
        if response is None:
            context = getattr(exc, "context", {})
            response = context.get("resp") if isinstance(context, dict) else None
        raw_url = getattr(response, "url", None)
        return {
            "final_domain": _domain_from_url(raw_url),
            "final_path": _safe_url_path(raw_url),
            "status_code": getattr(response, "status_code", None),
            "redirect_count": int(getattr(response, "redirect_count", 0) or 0),
        }

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
    ) -> tuple[int, int]:
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

        source_image_bytes = sum(path.stat().st_size for path in image_paths)
        if self.settings.pdf_compression_enabled:
            image_paths = self._compress_pdf_images(
                image_paths, pdf_path.parent / "compressed-images"
            )

        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        with pdf_path.open("wb") as output:
            img2pdf.convert([str(path) for path in image_paths], outputstream=output)
        if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
            raise RuntimeError("img2pdf 未能生成有效 PDF。")
        return len(image_paths), source_image_bytes

    def _compress_pdf_images(
        self, image_paths: list[Path], output_dir: Path
    ) -> list[Path]:
        from PIL import Image, ImageOps

        output_dir.mkdir(parents=True, exist_ok=True)
        compressed: list[Path] = []
        for index, source in enumerate(image_paths, 1):
            target = output_dir / f"{index:06}.jpg"
            with Image.open(source) as opened:
                image = ImageOps.exif_transpose(opened)
                max_width = self.settings.pdf_max_width
                if max_width > 0 and image.width > max_width:
                    height = max(1, round(image.height * max_width / image.width))
                    image = image.resize(
                        (max_width, height), Image.Resampling.LANCZOS
                    )
                if image.mode in {"RGBA", "LA"} or (
                    image.mode == "P" and "transparency" in image.info
                ):
                    rgba = image.convert("RGBA")
                    background = Image.new("RGB", rgba.size, "white")
                    background.paste(rgba, mask=rgba.getchannel("A"))
                    image = background
                elif image.mode != "RGB":
                    image = image.convert("RGB")
                image.save(
                    target,
                    format="JPEG",
                    quality=self.settings.pdf_jpeg_quality,
                    optimize=True,
                )
            if not target.is_file() or target.stat().st_size == 0:
                raise RuntimeError(f"PDF 压缩图片生成失败：{source.name}")
            compressed.append(target)
        return compressed

    def _build_option_config(
        self,
        job_root: Path,
        domains: list[str] | None = None,
        *,
        use_health_order: bool = True,
        cookies_override: dict[str, str] | None = None,
    ):
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
                "domain": (
                    domains
                    if domains is not None
                    else (
                        self._ordered_domains()
                        if use_health_order
                        else self.settings.client["domain"]
                    )
                ),
                "impl": self.settings.client["impl"],
                "retry_times": _as_int(self.settings.client.get("retry_times"), 5),
                "postman": {
                    "meta_data": {
                        "headers": None,
                        "impersonate": self.settings.client.get(
                            "impersonate", "chrome110"
                        ),
                        "proxies": self.settings.client.get("proxies", {}),
                        "cookies": (
                            cookies_override
                            if cookies_override is not None
                            else self.settings.client.get("cookies", {})
                        ),
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
        domain = _normalize_domain(item)
        if domain is None:
            continue
        if domain not in domains:
            domains.append(domain)
        if len(domains) >= MAX_DOMAIN_COUNT:
            break
    return domains or list(DEFAULT_DOMAIN)


def _normalize_domain(value: Any) -> str | None:
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = urlsplit(text if "://" in text else f"//{text}")
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not hostname:
        return None
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if (
        not re.fullmatch(r"[a-z0-9.-]+", hostname)
        or ".." in hostname
        or hostname.startswith((".", "-"))
        or hostname.endswith((".", "-"))
    ):
        return None
    return f"{hostname}:{port}" if port is not None else hostname


def _domain_from_url(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = urlsplit(text)
        if not parsed.hostname:
            return None
        hostname = parsed.hostname.encode("idna").decode("ascii").lower()
        return f"{hostname}:{parsed.port}" if parsed.port is not None else hostname
    except (UnicodeError, ValueError):
        return None


def _safe_url_path(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = urlsplit(text)
        return parsed.path[:300] or "/"
    except ValueError:
        return None


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


def _parse_identifier_list(value: Any) -> list[str]:
    if isinstance(value, list):
        values = value
    elif isinstance(value, str):
        values = re.split(r"[,;\s]+", value)
    else:
        values = []
    result: list[str] = []
    for item in values:
        identifier = str(item or "").strip()
        if identifier and identifier.isdigit() and identifier not in result:
            result.append(identifier)
    return result


def _event_user_id(event: Any, session: str) -> str:
    getter = getattr(event, "get_sender_id", None)
    if callable(getter):
        with suppress(Exception):
            sender_id = str(getter() or "").strip()
            if sender_id:
                return sender_id
    return f"session:{session}" if session else "unknown"


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
