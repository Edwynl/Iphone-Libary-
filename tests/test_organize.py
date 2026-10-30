from pathlib import Path
from PIL import Image
from ai_photos.catalog import PhotoCatalog
from ai_photos.organize import virtual_albums


def test_virtual_albums_only_group_and_do_not_move(tmp_path: Path):
    root = tmp_path / 'family'
    root.mkdir()
    original = root / 'mountain.png'
    Image.new('RGB', (30, 30), 'white').save(original)
    cat = PhotoCatalog(tmp_path / 'state')
    cat.add_folder(str(root))
    cat.scan()
    data = virtual_albums(cat)
    assert data['non_destructive']
    assert data['timeline'][0]['count'] == 1
    assert data['source_folders'][0]['count'] == 1
    assert original.is_file()
