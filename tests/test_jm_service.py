import asyncio
import json
import re
import shutil
import time
import types
import zipfile
from pathlib import Path

import pyzipper
from PIL import Image

import jm_service


class Logger:
    def __init__(self):
        self.exceptions = []
        self.errors = []

    def exception(self, message):
        self.exceptions.append(message)

    def error(self, message):
        self.errors.append(message)


class FakeEvent:
    def __init__(self, session="session"):
        self.session = session
        self.sent = []

    async def send(self, message):
        self.sent.append(message)

    def plain_result(self, message):
        return message


class FakeImage:
    def __init__(self, photo, index):
        self.from_photo = photo
        self.index = index


class FakePhoto:
    skip = False

    def __init__(self, photo_id, total_pages=30):
        self.photo_id = str(photo_id)
        self.id = self.photo_id
        self.name = f"chapter-{photo_id}"
        self.total_pages = total_pages
        self.page_arr = None

    def __iter__(self):
        return iter(
            FakeImage(self, index)
            for index in range(1, len(self.page_arr or []) + 1)
        )


class FakeAlbum(list):
    skip = False


class FakeClient:
    def __init__(self, chapter_count=5, pages=30):
        self.chapter_count = chapter_count
        self.pages = pages
        self.check_calls = 0

    def get_album_detail(self, _jm_id):
        return FakeAlbum(
            FakePhoto(index, self.pages)
            for index in range(1, self.chapter_count + 1)
        )

    def check_photo(self, photo):
        self.check_calls += 1
        photo.page_arr = list(range(photo.total_pages))


class FakeOption:
    def __init__(self, config, source_image):
        self.base_dir = Path(config["dir_rule"]["base_dir"])
        self.source_image = source_image

    def decide_image_batch_count(self, _photo):
        return 10

    def decide_photo_batch_count(self, _album):
        return 1

    def decide_image_filepath(self, image):
        return str(
            self.base_dir
            / image.from_photo.photo_id
            / f"{image.index:05}.jpg"
        )


class FakeDownloader:
    def __init__(self, option):
        self.option = option
        self.client = FakeClient()
        self.downloaded = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def before_album(self, _album):
        return None

    def after_album(self, _album):
        return None

    def before_photo(self, photo):
        photo.page_arr = list(range(photo.total_pages))

    def after_photo(self, photo):
        photo.page_arr = list(range(photo.total_pages))

    def execute_on_condition(self, iter_objs, apply, count_batch):
        del count_batch
        for item in list(iter_objs):
            apply(item)

    def download_by_photo_detail(self, photo):
        self.client.check_photo(photo)
        self.execute_on_condition(photo, self.download_by_image_detail, 1)

    def download_by_image_detail(self, image):
        image_path = Path(self.option.decide_image_filepath(image))
        image_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.option.source_image, image_path)
        self.downloaded.append((image.from_photo.photo_id, image.index))

    def raise_if_has_exception(self):
        return None


def make_settings(tmp_path, **overrides):
    config = {
        "zip_password": "secure-password",
        "max_chapters_per_download": 2,
        "max_pages_per_download": 40,
        **overrides,
    }
    return jm_service.JmSettings.from_config(config, tmp_path)


def install_fake_jmcomic(monkeypatch, source_image):
    downloaders = []

    class FakeOptionFactory:
        @staticmethod
        def construct(config):
            return FakeOption(config, source_image)

    def new_downloader(option):
        downloader = FakeDownloader(option)
        downloaders.append(downloader)
        return downloader

    fake_module = types.SimpleNamespace(
        JmOption=FakeOptionFactory,
        new_downloader=new_downloader,
    )
    monkeypatch.setattr(jm_service, "_load_jmcomic", lambda: fake_module)
    return downloaders


def test_input_and_config_helpers(tmp_path):
    settings = make_settings(
        tmp_path,
        client={"domain": ["https://Example.COM/path", "example.com", "bad host"]},
        group_file_path=" 文档\\漫画/ ",
    )
    assert settings.client["domain"] == ["example.com"]
    assert settings.group_file_path == "文档/漫画"
    assert settings.max_queue_size == 10
    assert settings.max_active_requests_per_session == 2
    assert jm_service.normalize_jm_id("JM00123") == "123"
    assert jm_service.normalize_jm_id("0") is None
    assert jm_service.normalize_jm_id("1" * 19) is None
    assert jm_service.normalize_jm_id("abc") is None
    assert jm_service.parse_chapter_selection("1-3，5", 5) == (1, 2, 3, 5)
    assert jm_service.parse_chapter_selection("5-1", 5) is None
    assert jm_service.parse_chapter_selection("9" * 10, 5) is None
    assert jm_service.format_selection((1, 2, 3, 5)) == "1-3,5"
    assert jm_service.ensure_chapter_isolated_dir_rule("Bd_Aid") == "Bd_Aid_Pid"


def test_group_file_path_upload_uses_resolved_folder_id(tmp_path):
    async def scenario():
        settings = make_settings(tmp_path, group_file_path="文档/漫画")
        coordinator = jm_service.DownloadCoordinator(settings, Logger())
        archive = settings.archive_dir / "JM123.zip"
        archive.write_bytes(b"archive")

        class Bot:
            def __init__(self):
                self.calls = []

            async def call_action(self, action, **params):
                self.calls.append((action, params))
                if action == "get_group_root_files":
                    return {
                        "files": [],
                        "folders": [
                            {"folder_name": "文档", "folder_id": "root-docs"}
                        ],
                    }
                if action == "get_group_files_by_folder":
                    return {
                        "data": {
                            "files": [],
                            "folders": [
                                {"name": "漫画", "id": "nested-comics"}
                            ],
                        }
                    }
                return None

        class GroupEvent:
            def __init__(self):
                self.bot = Bot()

            def get_platform_name(self):
                return "aiocqhttp"

            def get_group_id(self):
                return "123456"

            def get_self_id(self):
                return "987654"

        event = GroupEvent()
        await coordinator._send_file_compat(event, archive)

        assert [action for action, _params in event.bot.calls] == [
            "get_group_root_files",
            "get_group_files_by_folder",
            "upload_group_file",
        ]
        nested_params = event.bot.calls[1][1]
        assert nested_params["folder_id"] == "root-docs"
        upload_params = event.bot.calls[2][1]
        assert upload_params == {
            "group_id": "123456",
            "file": str(archive),
            "name": "JM123.zip",
            "folder": "nested-comics",
            "self_id": "987654",
        }

    asyncio.run(scenario())


def test_group_file_path_missing_folder_stops_upload(tmp_path):
    async def scenario():
        settings = make_settings(tmp_path, group_file_path="文档")
        coordinator = jm_service.DownloadCoordinator(settings, Logger())
        archive = settings.archive_dir / "JM123.zip"
        archive.write_bytes(b"archive")

        class Bot:
            def __init__(self):
                self.calls = []

            async def call_action(self, action, **params):
                self.calls.append((action, params))
                return {"files": [], "folders": []}

        class GroupEvent:
            def __init__(self):
                self.bot = Bot()

            def get_platform_name(self):
                return "aiocqhttp"

            def get_group_id(self):
                return "123456"

            def get_self_id(self):
                return ""

        event = GroupEvent()
        try:
            await coordinator._send_file_compat(event, archive)
        except RuntimeError as exc:
            assert "不存在群文件夹“文档”" in str(exc)
        else:
            raise AssertionError("missing group folder did not stop the upload")
        assert [action for action, _params in event.bot.calls] == [
            "get_group_root_files"
        ]

    asyncio.run(scenario())


def test_queue_limits_and_state_pruning(tmp_path):
    async def scenario():
        settings = make_settings(
            tmp_path,
            max_queue_size=1,
            max_active_requests_per_session=1,
            duplicate_cooldown_seconds=1,
            chapter_selection_timeout_seconds=60,
        )
        coordinator = jm_service.DownloadCoordinator(settings, Logger())

        async def wait_forever():
            await asyncio.Event().wait()

        task = asyncio.create_task(wait_forever())
        coordinator._jobs["1"] = jm_service.Job(
            "1",
            requesters=[jm_service.Requester(FakeEvent("s1"), "s1")],
            task=task,
        )
        assert "队列已满" in coordinator._admission_error_locked("s2")

        old = time.monotonic() - 120
        coordinator._pending_selections["expired"] = jm_service.PendingSelection(
            "1",
            (),
            jm_service.Requester(FakeEvent("expired"), "expired"),
            old,
        )
        coordinator._last_delivery[("s1", "1")] = old
        coordinator._prune_state_locked()
        assert "expired" not in coordinator._pending_selections
        assert not coordinator._last_delivery
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_per_session_limit_and_short_password(tmp_path):
    async def scenario():
        settings = make_settings(
            tmp_path,
            max_queue_size=10,
            max_active_requests_per_session=1,
        )
        coordinator = jm_service.DownloadCoordinator(settings, Logger())

        async def wait_forever():
            await asyncio.Event().wait()

        task = asyncio.create_task(wait_forever())
        coordinator._jobs["1"] = jm_service.Job(
            "1",
            requesters=[jm_service.Requester(FakeEvent("s1"), "s1")],
            task=task,
        )
        assert "当前会话" in coordinator._admission_error_locked("s1")
        assert coordinator._admission_error_locked("s2") is None
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        settings.zip_password = "short"
        result = await coordinator.submit(FakeEvent(), "123")
        assert "至少需要 8 个字符" in result.message

    asyncio.run(scenario())


def test_build_archive_enforces_limits_and_secure_cache(tmp_path, monkeypatch):
    source_image = tmp_path / "source.jpg"
    Image.new("RGB", (8, 8), "white").save(source_image, "JPEG", quality=70)
    settings = make_settings(tmp_path)
    coordinator = jm_service.DownloadCoordinator(settings, Logger())
    downloaders = install_fake_jmcomic(monkeypatch, source_image)

    full_archive = settings.archive_dir / "JM123.zip"
    try:
        coordinator.build_archive("123", full_archive)
    except RuntimeError as exc:
        assert "必须先选择章节" in str(exc)
    else:
        raise AssertionError("chapter limit was bypassed")

    archive_path = coordinator.archive_path("123", (1, 2))
    coordinator.build_archive("123", archive_path, (1, 2))
    manifest_path = coordinator.manifest_path(archive_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["chapter_count"] == 2
    assert manifest["pdf_pages"] == 40
    assert manifest["cache_format_version"] == jm_service.CACHE_FORMAT_VERSION
    assert "password_sha256" not in manifest
    assert coordinator.is_valid_cache(archive_path)
    assert len(downloaders[-1].downloaded) == 40

    with pyzipper.AESZipFile(archive_path, "r") as archive:
        archive.setpassword(settings.zip_password.encode())
        infos = archive.infolist()
        assert len(infos) == 1
        pdf_data = archive.read(infos[0])
    assert len(re.findall(rb"/Type\s*/Page\b", pdf_data)) == 40

    manifest["selection"] = "2"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert not coordinator.is_valid_cache(archive_path)
    manifest["selection"] = "1-2"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert coordinator.is_valid_cache(archive_path)

    settings.zip_password = "different-password"
    assert not coordinator.is_valid_cache(archive_path)


def test_cache_rejects_unencrypted_or_multiple_members(tmp_path):
    settings = make_settings(tmp_path)
    coordinator = jm_service.DownloadCoordinator(settings, Logger())

    archive_path = settings.archive_dir / "JM123.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("123.pdf", b"%PDF-test")
    coordinator.manifest_path(archive_path).write_text(
        json.dumps(
            {
                "jm_id": "123",
                "archive": archive_path.name,
                "chapter_count": 1,
                "pdf_pages": 1,
                "max_chapters_per_download": settings.max_chapters_per_download,
                "max_pages_per_download": settings.max_pages_per_download,
                "cache_format_version": jm_service.CACHE_FORMAT_VERSION,
            }
        ),
        encoding="utf-8",
    )
    assert not coordinator.is_valid_cache(archive_path)

    with pyzipper.AESZipFile(
        archive_path,
        "w",
        compression=pyzipper.ZIP_DEFLATED,
    ) as archive:
        archive.setencryption(pyzipper.WZ_AES, nbits=256)
        archive.setpassword(settings.zip_password.encode())
        archive.writestr("123.pdf", b"%PDF-one")
        archive.writestr("extra.pdf", b"%PDF-two")
    assert not coordinator.is_valid_cache(archive_path)


def test_download_errors_are_not_disclosed_to_chat(tmp_path):
    async def scenario():
        settings = make_settings(tmp_path)
        logger = Logger()
        coordinator = jm_service.DownloadCoordinator(settings, logger)
        event = FakeEvent()
        job = jm_service.Job(
            "123",
            requesters=[jm_service.Requester(event, event.session)],
        )

        def fail(_jm_id):
            raise RuntimeError("cookie=super-secret")

        coordinator.inspect_album = fail
        await coordinator._prepare_and_download(job)
        assert event.sent
        assert "super-secret" not in event.sent[0]
        assert "查看 AstrBot 日志" in event.sent[0]
        assert logger.errors
        assert "super-secret" not in logger.errors[0]
        assert "cookie=***" in logger.errors[0]

    asyncio.run(scenario())


def test_empty_album_response_is_reported_as_suspected_expired_cookie(tmp_path):
    async def scenario():
        settings = make_settings(
            tmp_path,
            client={
                "domain": ["jm.example"],
                "cookies": '{"AVS":"super-secret"}',
            },
        )
        coordinator = jm_service.DownloadCoordinator(settings, Logger())
        event = FakeEvent()
        job = jm_service.Job(
            "350234",
            requesters=[jm_service.Requester(event, event.session)],
        )

        class RegularNotMatchException(Exception):
            error_text = "[]"

        assert coordinator._is_retryable_domain_error(
            RegularNotMatchException("album_id parse failed")
        )

        def fail(_jm_id):
            raise RegularNotMatchException("album_id parse failed")

        coordinator.inspect_album = fail
        await coordinator._prepare_and_download(job)

        assert "Cookie 可能已失效" in event.sent[0]
        assert "super-secret" not in event.sent[0]
        failure = next(
            item
            for item in coordinator.store.list_events()
            if item["event_type"] == "job_failed"
        )
        assert failure["reason"] == "疑似 Cookie 失效"
        assert failure["details"]["failure_code"] == "cookie_suspected_expired"

    asyncio.run(scenario())


def test_failure_diagnosis_distinguishes_access_network_and_filesystem(tmp_path):
    coordinator = jm_service.DownloadCoordinator(make_settings(tmp_path), Logger())

    class MissingAlbumPhotoException(Exception):
        pass

    class RequestRetryAllFailException(Exception):
        pass

    access = coordinator._diagnose_failure(MissingAlbumPhotoException(), "inspect")
    network = coordinator._diagnose_failure(RequestRetryAllFailException(), "inspect")
    filesystem = coordinator._diagnose_failure(OSError("disk full"), "download")

    assert access.reason == "本子不存在或访问受限"
    assert network.reason == "JM 网络请求失败"
    assert filesystem.reason == "文件系统读写失败"
