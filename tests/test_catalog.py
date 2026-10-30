from __future__ import annotations

from pathlib import Path
import numpy as np
import pytest
from PIL import Image

from ai_photos.catalog import PhotoCatalog


class FakeEncoder:
    model_id = "fake-rgb-v1"
    installed = True

    def embed_images(self, paths):
        result = []
        for path in paths:
            with Image.open(path) as img:
                pixels = np.asarray(img.convert("RGB"), dtype=np.float32).mean(axis=(0, 1))
            result.append(pixels / np.linalg.norm(pixels))
        return result

    def embed_text(self, text):
        color = text.lower()
        values = np.array({"red": [1, 0, 0], "green": [0, 1, 0],
                           "blue": [0, 0, 1]}.get(color, [1, 1, 1]), dtype=np.float32)
        return values / np.linalg.norm(values)


@pytest.fixture
def photos(tmp_path: Path):
    library = tmp_path / "My Photos"
    library.mkdir()
    Image.new("RGB", (80, 50), "red").save(library / "雪山_red.jpg")
    Image.new("RGB", (50, 80), "blue").save(library / "blue.png")
    (library / "not-a-photo.txt").write_text("ignore", encoding="utf-8")
    return library


def test_scan_and_browse_and_incremental(tmp_path, photos):
    cat = PhotoCatalog(tmp_path / "state")
    assert cat.add_folder(str(photos))["added"] is True
    assert cat.add_folder(str(photos))["added"] is False
    first = cat.scan()
    assert first["seen"] == 2 and first["updated"] == 2
    assert first["embedded"] == 0
    items = cat.browse()
    assert len(items) == 2
    assert all(Path(item["thumb_path"]).is_file() for item in items)
    assert all(item["path"].startswith(str(photos)) for item in items)
    again = cat.scan()
    assert again["seen"] == 2 and again["updated"] == 0
    assert cat.stats()["photos"] == 2


def test_semantic_search_and_reindex(tmp_path, photos):
    cat = PhotoCatalog(tmp_path / "state")
    cat.add_folder(str(photos))
    encoder = FakeEncoder()
    result = cat.scan(encoder)
    assert result["embedded"] == 2
    found = cat.search("red", encoder=encoder)
    assert found["mode"] == "semantic"
    assert found["photos"][0]["name"] == "雪山_red.jpg"
    assert cat.search("blue", encoder=encoder)["photos"][0]["name"] == "blue.png"
    assert cat.scan(encoder)["embedded"] == 0
    Image.new("RGB", (81, 51), "green").save(photos / "雪山_red.jpg")
    result = cat.scan(encoder)
    assert result["updated"] == 1 and result["embedded"] == 1
    assert cat.search("green", encoder=encoder)["photos"][0]["name"] == "雪山_red.jpg"


def test_filename_mode_and_escape(tmp_path, photos):
    cat = PhotoCatalog(tmp_path / "state")
    cat.add_folder(str(photos))
    cat.scan()
    assert cat.search("雪山")["photos"][0]["name"] == "雪山_red.jpg"
    assert cat.search("blue")["mode"] == "filename"
    assert cat.search("%") ["photos"] == []
    assert cat.search("_red")["photos"][0]["name"] == "雪山_red.jpg"
    assert len(cat.search("")["photos"]) == 2


def test_changed_and_deleted_and_corrupt(tmp_path, photos):
    bad = photos / "bad.jpg"
    bad.write_text("bad jpeg", encoding="utf-8")
    cat = PhotoCatalog(tmp_path / "state")
    cat.add_folder(str(photos))
    result = cat.scan()
    assert result["seen"] == 3 and result["errors"] == 1
    assert cat.stats()["errors"] == 1
    (photos / "blue.png").unlink()
    assert cat.scan()["removed"] == 1
    assert cat.stats()["photos"] == 2


def test_offline_root_does_not_erase(tmp_path, photos):
    cat = PhotoCatalog(tmp_path / "state")
    cat.add_folder(str(photos))
    cat.scan()
    photos.rename(photos.with_name("removed-drive"))
    assert cat.scan()["removed"] == 0
    assert cat.stats()["photos"] == 2


def test_overlap_prevention(tmp_path, photos):
    child = photos / "nested"
    child.mkdir()
    cat = PhotoCatalog(tmp_path / "state")
    cat.add_folder(str(photos))
    with pytest.raises(ValueError, match="Overlapping"):
        cat.add_folder(str(child))


def test_search_fallback_on_model_error(tmp_path, photos):
    cat = PhotoCatalog(tmp_path / "state")
    cat.add_folder(str(photos))
    cat.scan()

    class BrokenEncoder:
        model_id = "broken"
        def embed_text(self, text):
            raise RuntimeError("GPU unavailable")

    result = cat.search("red", encoder=BrokenEncoder())
    assert result["mode"] == "filename"
    assert "GPU unavailable" in result["warning"]


def test_path_stays_original_and_private(tmp_path, photos):
    cat = PhotoCatalog(tmp_path / "state")
    src = photos / "blue.png"
    before = src.read_bytes()
    cat.add_folder(str(photos))
    cat.scan()
    assert src.read_bytes() == before
    assert all(str(cat.home) not in item["path"] for item in cat.browse())


def test_scan_does_not_follow_symlinked_directory(tmp_path, photos):
    external = tmp_path / "external"
    external.mkdir()
    Image.new("RGB", (10, 10), "green").save(external / "private.jpg")
    link = photos / "redirect"
    try:
        link.symlink_to(external, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks unavailable")
    cat = PhotoCatalog(tmp_path / "state")
    cat.add_folder(str(photos))
    assert cat.scan()["seen"] == 2


def test_thumb_cache_regenerated_for_modified_photo(tmp_path, photos):
    cat = PhotoCatalog(tmp_path / "state")
    cat.add_folder(str(photos))
    cat.scan()
    first = next(i for i in cat.browse() if i["name"] == "blue.png")
    Image.new("RGB", (51, 81), "blue").save(photos / "blue.png")
    cat.scan()
    second = next(i for i in cat.browse() if i["name"] == "blue.png")
    assert first["thumb_path"] != second["thumb_path"]
    assert Path(second["thumb_path"]).is_file()


def test_remove_folder_forgets_index_without_deleting_photos(tmp_path, photos):
    cat = PhotoCatalog(tmp_path / "state")
    cat.add_folder(str(photos))
    cat.scan(encoder=FakeEncoder())
    assert cat.stats(FakeEncoder.model_id)["embeddings"] == 2
    original = (photos / "blue.png").read_bytes()
    result = cat.remove_folder(str(photos))
    assert result == {"path": str(photos), "removed": True, "photos_forgotten": 2}
    assert cat.stats()["photos"] == 0
    assert cat.folders() == []
    assert (photos / "blue.png").read_bytes() == original
    assert cat.remove_folder(str(photos))["removed"] is False


def test_scanning_user_root_never_indexes_its_own_cache(tmp_path, photos):
    cat = PhotoCatalog(photos / "WindowsAIPhotos")
    cat.add_folder(str(photos))
    assert cat.scan()["seen"] == 2
    # The thumbs written during the first scan are .jpg files under the root.
    # A subsequent scan must not treat them as new original photographs.
    assert cat.scan()["seen"] == 2
    assert cat.stats()["photos"] == 2
    with pytest.raises(ValueError, match="internal cache"):
        cat.add_folder(str(cat.home))
