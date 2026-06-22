from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace


def _load_reader_module() -> ModuleType:
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "photo_library_apple_reader_under_test"
    package = ModuleType(package_name)
    package.__path__ = [str(plugin_dir)]  # type: ignore[attr-defined]
    sys.modules[package_name] = package

    spec = importlib.util.spec_from_file_location(
        f"{package_name}.apple_photos_reader",
        plugin_dir / "apple_photos_reader.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakePhotosDB:
    def __init__(self, photos):
        self._photos = list(photos)

    def photos(self, **kwargs):
        uuids = set(kwargs.get("uuid") or [])
        if uuids:
            return [photo for photo in self._photos if photo.uuid in uuids]
        return list(self._photos)


def _fake_photo(path: Path, *, uuid: str = "UUID-1", modified: datetime | None = None):
    return SimpleNamespace(
        uuid=uuid,
        path=str(path),
        path_edited=None,
        isphoto=True,
        hidden=False,
        visible=True,
        date=datetime(2024, 3, 2, 10, 30, tzinfo=timezone.utc),
        date_original=datetime(2024, 3, 2, 10, 30, tzinfo=timezone.utc),
        date_modified=modified or datetime(2024, 3, 2, 11, 0, tzinfo=timezone.utc),
        date_added=datetime(2024, 3, 2, 10, 45, tzinfo=timezone.utc),
        original_filename="IMG_0001.HEIC",
        filename="UUID-1.HEIC",
        original_filesize=1234,
        fingerprint="fingerprint-1",
        original_width=4032,
        original_height=3024,
        original_orientation=1,
        latitude=31.2,
        longitude=121.4,
        favorite=True,
        albums=["Shanghai"],
        persons=["Asuka"],
        keywords=["trip"],
        labels_normalized=["city"],
        place=SimpleNamespace(name="Shanghai"),
        exif_info=SimpleNamespace(
            camera_make="Apple",
            camera_model="iPhone 15 Pro",
            lens_model="iPhone back camera",
            focal_length=6.8,
            aperture=1.8,
            shutter_speed=1 / 120,
            iso=80,
            latitude=31.2,
            longitude=121.4,
        ),
    )


def test_scan_library_normalizes_osxphotos_photo(tmp_path: Path) -> None:
    mod = _load_reader_module()
    photo_file = tmp_path / "IMG_0001.HEIC"
    photo_file.write_bytes(b"fake image bytes")
    reader = mod.ApplePhotosReader(
        photosdb_factory=lambda _path: _FakePhotosDB([_fake_photo(photo_file)])
    )

    result = reader.scan_library("/Users/me/Pictures/Photos Library.photoslibrary", limit=10)

    assert result.total_scanned == 1
    assert len(result.items) == 1
    item = result.items[0]
    assert item["asset_local_id"] == "apple-photos:UUID-1"
    assert item["apple_photos_uuid"] == "UUID-1"
    assert item["source_backend"] == "apple_photos"
    assert item["path"] == str(photo_file)
    assert item["filename"] == "IMG_0001.HEIC"
    assert item["camera_make"] == "Apple"
    assert item["latitude"] == 31.2
    assert item["location_name"] == "Shanghai"


def test_resolve_asset_refs_uses_uuid_lookup(tmp_path: Path) -> None:
    mod = _load_reader_module()
    photo_file = tmp_path / "IMG_0001.HEIC"
    photo_file.write_bytes(b"fake image bytes")
    reader = mod.ApplePhotosReader(
        photosdb_factory=lambda _path: _FakePhotosDB([_fake_photo(photo_file)])
    )

    resolved, missing = reader.resolve_asset_refs(["apple-photos:UUID-1", "apple-photos:missing"])

    assert [item["path"] for item in resolved] == [str(photo_file)]
    assert missing == ["apple-photos:missing"]
