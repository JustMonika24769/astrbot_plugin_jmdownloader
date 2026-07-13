import asyncio
import json
import os
import time
from pathlib import Path

from PIL import Image

import config_tools
import jm_service
from runtime_store import RuntimeStore


class Logger:
    def error(self, _message):
        pass


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
    store.record_domain_health("slow.example", True, 300, 200, None)
    store.record_domain_health("fast.example", True, 20, 200, None)
    store.record_domain_health("bad.example", False, 10, 503, "unavailable")
    coordinator = jm_service.DownloadCoordinator(settings, Logger(), store)

    assert coordinator._ordered_domains() == [
        "fast.example",
        "slow.example",
        "bad.example",
    ]

    store.record_event("delivery_succeeded", success=True, bytes_count=100)
    store.record_event("job_failed", success=False, reason="下载失败")
    store.record_event("request_accepted", success=True)
    metrics = store.metrics()
    assert metrics["outcomes"] == {"success": 1, "failure": 1}
    assert metrics["failure_reasons"][0] == {"reason": "下载失败", "count": 1}


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
