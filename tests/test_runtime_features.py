import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path

from PIL import Image

import config_tools
import jm_service
from runtime_store import RuntimeStore


class Logger:
    def __init__(self):
        self.warnings = []
        self.errors = []

    def error(self, _message):
        self.errors.append(_message)

    def warning(self, message):
        self.warnings.append(message)


class Event:
    def __init__(self, session="session", user_id="10001"):
        self.session = session
        self.user_id = user_id
        self.sent = []

    def get_sender_id(self):
        return self.user_id

    async def send(self, message):
        self.sent.append(message)

    def plain_result(self, message):
        return message


def make_settings(tmp_path: Path, **overrides):
    config = {
        "zip_password": "secure-password",
        "domain_health_check_enabled": False,
        **overrides,
    }
    return jm_service.JmSettings.from_config(config, tmp_path)


def test_rate_daily_quota_and_delivery_rollback(tmp_path):
    store = RuntimeStore(tmp_path / "runtime.sqlite3")

    assert store.check_and_record_rate("u1", 2, 60).allowed
    assert store.check_and_record_rate("u1", 2, 60).allowed
    rejected = store.check_and_record_rate("u1", 2, 60)
    assert not rejected.allowed
    assert "请求过于频繁" in rejected.message

    assert store.reserve_delivery("u1", 600, 2, 1000).allowed
    traffic_rejected = store.reserve_delivery("u1", 500, 2, 1000)
    assert not traffic_rejected.allowed
    assert "流量配额不足" in traffic_rejected.message
    store.release_delivery("u1", 600)
    assert store.usage_for("u1") == {"downloads": 0, "bytes_sent": 0}

    assert store.reserve_delivery("u1", 100, 1, 0).allowed
    count_rejected = store.reserve_delivery("u1", 100, 1, 0)
    assert not count_rejected.allowed
    assert "下载次数" in count_rejected.message


def test_structured_metrics_and_domain_ordering(tmp_path):
    settings = make_settings(
        tmp_path,
        domain_health_check_enabled=True,
        client={"domain": ["slow.example", "fast.example", "bad.example"]},
    )
    store = RuntimeStore(tmp_path / "runtime.sqlite3")
    store.record_domain_health(
        "slow.example", True, 300, 200, None, "current.example", 1
    )
    store.record_domain_health("fast.example", True, 20, 200, None)
    store.record_domain_health("bad.example", False, 10, 503, "unavailable")
    coordinator = jm_service.DownloadCoordinator(settings, Logger(), store)

    assert coordinator._ordered_domains() == [
        "fast.example",
        "slow.example",
        "bad.example",
    ]
    slow_health = next(
        item for item in store.domain_health() if item["domain"] == "slow.example"
    )
    assert slow_health["final_domain"] == "current.example"
    assert slow_health["redirect_count"] == 1

    store.record_event("delivery_succeeded", success=True, bytes_count=100)
    store.record_event("job_failed", success=False, reason="下载失败")
    store.record_event("request_accepted", success=True)
    metrics = store.metrics()
    assert metrics["outcomes"] == {"success": 1, "failure": 1}
    assert metrics["failure_reasons"][0] == {"reason": "下载失败", "count": 1}


def test_domain_health_schema_migrates_from_v1_1(tmp_path):
    database = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE domain_health (
                domain TEXT PRIMARY KEY,
                healthy INTEGER NOT NULL,
                latency_ms INTEGER,
                status_code INTEGER,
                checked_at REAL NOT NULL,
                error TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO domain_health VALUES (?, ?, ?, ?, ?, ?)",
            ("old.example", 1, 25, 200, time.time(), None),
        )

    store = RuntimeStore(database)
    old = store.domain_health()[0]
    assert old["domain"] == "old.example"
    assert old["final_domain"] is None
    assert old["redirect_count"] == 0

    store.record_domain_health(
        "old.example", True, 20, 200, None, "new.example", 2
    )
    migrated = store.domain_health()[0]
    assert migrated["final_domain"] == "new.example"
    assert migrated["redirect_count"] == 2


def test_pdf_image_compression(tmp_path):
    settings = make_settings(
        tmp_path,
        pdf_compression_enabled=True,
        pdf_jpeg_quality=45,
        pdf_max_width=400,
    )
    coordinator = jm_service.DownloadCoordinator(settings, Logger())
    source = tmp_path / "large.png"
    Image.new("RGBA", (1200, 900), (220, 30, 80, 160)).save(source)

    compressed = coordinator._compress_pdf_images(
        [source], tmp_path / "compressed"
    )
    assert len(compressed) == 1
    with Image.open(compressed[0]) as image:
        assert image.format == "JPEG"
        assert image.mode == "RGB"
        assert image.size == (400, 300)
    assert compressed[0].stat().st_size < source.stat().st_size


def test_cache_expiry_quota_and_active_file_protection(tmp_path):
    settings = make_settings(
        tmp_path,
        cache_expire_days=1,
        cache_max_size_mb=1,
    )
    coordinator = jm_service.DownloadCoordinator(settings, Logger())
    expired = settings.archive_dir / "JM1.zip"
    protected = settings.archive_dir / "JM2.zip"
    oversized = settings.archive_dir / "JM3.zip"
    expired.write_bytes(b"x" * 128)
    protected.write_bytes(b"p" * 700_000)
    oversized.write_bytes(b"o" * 700_000)
    old = time.time() - 3 * 86400
    os.utime(expired, (old, old))

    result = coordinator._cleanup_cache_sync({protected.resolve()})
    assert not expired.exists()
    assert protected.exists()
    assert not oversized.exists()
    assert result["removed_count"] == 2


def test_admin_cancel_notifies_requesters(tmp_path):
    async def scenario():
        settings = make_settings(tmp_path)
        coordinator = jm_service.DownloadCoordinator(settings, Logger())
        event = Event()

        async def wait_forever():
            await asyncio.Event().wait()

        task = asyncio.create_task(wait_forever())
        coordinator._jobs["123"] = jm_service.Job(
            "123",
            requesters=[jm_service.Requester(event, event.session, event.user_id)],
            task=task,
        )
        cancelled = await coordinator.cancel_jobs("123")
        assert cancelled == ["123"]
        assert task.cancelled()
        assert event.sent == ["JM123 任务已被管理员取消。"]

    asyncio.run(scenario())


def test_delivery_enforces_daily_limit_and_rolls_back_failure(tmp_path):
    async def scenario():
        settings = make_settings(tmp_path, user_daily_download_limit=1)
        store = RuntimeStore(tmp_path / "runtime.sqlite3")
        coordinator = jm_service.DownloadCoordinator(settings, Logger(), store)
        archive = settings.archive_dir / "JM123.zip"
        archive.write_bytes(b"z" * 100)
        first = Event("s1", "user-a")
        second = Event("s2", "user-a")
        sent = []

        async def succeed(event, path):
            sent.append((event.session, path.name))

        coordinator._send_file_compat = succeed
        job = jm_service.Job(
            "123",
            requesters=[
                jm_service.Requester(first, first.session, first.user_id),
                jm_service.Requester(second, second.session, second.user_id),
            ],
        )
        await coordinator._send_to_requesters(job, archive)
        assert sent == [("s1", "JM123.zip")]
        assert "下载次数已达到上限" in second.sent[0]
        assert store.usage_for("user-a") == {"downloads": 1, "bytes_sent": 100}

        failing = Event("s3", "user-b")

        async def fail(_event, _path):
            raise RuntimeError("platform unavailable")

        coordinator._send_file_compat = fail
        failed_job = jm_service.Job(
            "123",
            requesters=[
                jm_service.Requester(failing, failing.session, failing.user_id)
            ],
        )
        await coordinator._send_to_requesters(failed_job, archive)
        assert store.usage_for("user-b") == {"downloads": 0, "bytes_sent": 0}
        assert "文件发送失败" in failing.sent[0]

    asyncio.run(scenario())


def test_config_export_validation_and_secret_preservation(tmp_path):
    schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
    current = {
        "zip_password": "secure-password",
        "plugin_admin_qq_ids": ["10001"],
        "client": {
            "domain": ["jm.example"],
            "cookies": '{"AVS":"secret-cookie"}',
            "proxies": "{}",
        },
    }
    safe_export = config_tools.export_config(
        current, "v1.1.0", include_secrets=False
    )
    assert "zip_password" not in safe_export["config"]
    assert "cookies" not in safe_export["config"]["client"]

    safe_export["config"]["max_pages_per_download"] = 120
    result = config_tools.validate_config_document(safe_export, current, schema)
    assert result.valid
    assert result.config["zip_password"] == "secure-password"
    assert result.config["client"]["cookies"] == '{"AVS":"secret-cookie"}'
    assert result.config["max_pages_per_download"] == 120

    invalid = config_tools.validate_config_document(
        {"zip_password": "short", "client": {"domain": []}}, current, schema
    )
    assert not invalid.valid
    assert any("zip_password" in error for error in invalid.errors)
    assert any("client.domain" in error for error in invalid.errors)


def test_album_inspection_retries_semantic_missing_on_next_domain(
    tmp_path, monkeypatch
):
    class Response:
        url = "https://real-bad.example/error/album_missing"
        status_code = 200
        redirect_count = 1

    class MissingAlbumPhotoException(Exception):
        def __init__(self):
            super().__init__("hardcoded https://18comic.vip/album/123/")
            self.resp = Response()

    class Photo:
        id = "1"
        name = "chapter"

    attempts = []

    class Client:
        def __init__(self, domain):
            self.domain = domain

        def get_album_detail(self, _jm_id):
            attempts.append(self.domain)
            if self.domain == "bad.example":
                raise MissingAlbumPhotoException()
            return [Photo()]

    class Option:
        def __init__(self, config):
            self.domain = config["client"]["domain"][0]

        def build_jm_client(self, **_kwargs):
            return Client(self.domain)

    fake_jmcomic = type(
        "FakeJmcomic",
        (),
        {"JmOption": type("Factory", (), {"construct": staticmethod(Option)})},
    )
    monkeypatch.setattr(jm_service, "_load_jmcomic", lambda: fake_jmcomic)
    settings = make_settings(
        tmp_path,
        client={"domain": ["bad.example", "good.example"]},
    )
    logger = Logger()
    coordinator = jm_service.DownloadCoordinator(settings, logger)

    chapters = coordinator.inspect_album("123")
    assert attempts == ["bad.example", "good.example"]
    assert chapters[0].title == "chapter"
    assert "final=real-bad.example" in logger.warnings[0]
    retry_event = next(
        event
        for event in coordinator.store.list_events()
        if event["event_type"] == "domain_album_retry"
    )
    assert retry_event["details"]["final_domain"] == "real-bad.example"
    assert retry_event["details"]["final_path"] == "/error/album_missing"


def test_download_pipeline_retries_semantic_missing(tmp_path):
    class MissingAlbumPhotoException(Exception):
        pass

    settings = make_settings(
        tmp_path,
        client={"domain": ["bad.example", "good.example"]},
    )
    coordinator = jm_service.DownloadCoordinator(settings, Logger())
    calls = []

    def download_on_domain(
        _jmcomic, _attempt_root, pdf_dir, _jm_id, _selection, domain
    ):
        calls.append(domain)
        if domain == "bad.example":
            raise MissingAlbumPhotoException("missing")
        pdf_path = pdf_dir / "123.pdf"
        pdf_path.write_bytes(b"%PDF-test")
        return jm_service.PdfBuildResult(
            pdf_path=pdf_path,
            chapter_count=1,
            pdf_pages=1,
            chapter_limit=20,
            page_limit=80,
            source_image_bytes=10,
            pdf_bytes=9,
        )

    coordinator._download_to_pdf_on_domain = download_on_domain
    result = coordinator._download_to_pdf(
        object(), tmp_path / "job", tmp_path / "pdf", "123", None
    )
    assert calls == ["bad.example", "good.example"]
    assert result.pdf_path.read_bytes() == b"%PDF-test"


def test_one_time_login_returns_cookies_without_retaining_password(
    tmp_path, monkeypatch
):
    captured_configs = []

    class Response:
        url = "https://current.example/login"

    class Client:
        def login(self, username, password):
            assert username == "account"
            assert password == "temporary-password"
            return Response()

        def get_meta_data(self, key, default=None):
            if key == "cookies":
                return {"AVS": "new-avs", "remember": "new-remember"}
            return default

    class Option:
        def build_jm_client(self, **_kwargs):
            return Client()

    class Factory:
        @staticmethod
        def construct(config):
            captured_configs.append(config)
            return Option()

    fake_jmcomic = type("FakeJmcomic", (), {"JmOption": Factory})
    monkeypatch.setattr(jm_service, "_load_jmcomic", lambda: fake_jmcomic)
    settings = make_settings(
        tmp_path,
        client={
            "domain": ["current.example"],
            "cookies": '{"AVS":"old-avs","cf_clearance":"keep"}',
        },
    )
    coordinator = jm_service.DownloadCoordinator(settings, Logger())

    result = asyncio.run(
        coordinator.login_once(
            "account", "temporary-password", "current.example"
        )
    )
    assert captured_configs[0]["client"]["postman"]["meta_data"]["cookies"] == {}
    assert result["cookies"] == {
        "cf_clearance": "keep",
        "AVS": "new-avs",
        "remember": "new-remember",
    }
    assert "password" not in result
    assert "username" not in result


def test_domain_discovery_combines_upstream_sources(tmp_path, monkeypatch):
    class Client:
        def get_html_domain(self):
            return "redirect.example"

        def get_html_domain_all(self):
            return ["publish.example", "redirect.example", "jm365.example/app"]

        def get_html_domain_all_via_github(self):
            return {"github.example", "publish.example"}

    class Option:
        def build_jm_client(self, **_kwargs):
            return Client()

    class ModuleConfig:
        DOMAIN_HTML = "cached.example"
        DOMAIN_HTML_LIST = ["cached.example"]

    fake_jmcomic = type(
        "FakeJmcomic",
        (),
        {
            "JmOption": type(
                "Factory", (), {"construct": staticmethod(lambda _config: Option())}
            ),
            "JmModuleConfig": ModuleConfig,
        },
    )
    monkeypatch.setattr(jm_service, "_load_jmcomic", lambda: fake_jmcomic)
    settings = make_settings(
        tmp_path,
        client={"domain": ["configured.example"]},
    )
    coordinator = jm_service.DownloadCoordinator(settings, Logger())

    result = coordinator._discover_domains_sync()
    domains = [item["domain"] for item in result["candidates"]]
    assert domains == [
        "redirect.example",
        "configured.example",
        "github.example",
        "publish.example",
    ]
    publish = next(
        item for item in result["candidates"] if item["domain"] == "publish.example"
    )
    assert publish["sources"] == ["github", "publish"]
    assert result["errors"] == []
    assert ModuleConfig.DOMAIN_HTML is None
    assert ModuleConfig.DOMAIN_HTML_LIST is None
