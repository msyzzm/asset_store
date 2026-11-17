import io
import zipfile

import pytest
from PIL import Image

from assetindex.archive_index import index_archives
from assetindex.caption import CaptionClient, CaptionConfig
from assetindex.classify import (
    CATEGORY_PATHS,
    UNSORTED,
    build_prompt,
    classify_unit,
    clean_name,
    digest,
    parse_answer,
    picture,
    target_for,
    unit_members,
)
from assetindex.embedding import EmbedError
from assetindex.scanner import scan_root
from tests.fake_embed import FakeEmbedServer


@pytest.fixture
def server():
    with FakeEmbedServer() as fake:
        yield fake


@pytest.fixture
def library(conn, settings, asset_root):
    for i, color in enumerate([(255, 0, 0), (250, 20, 0)]):
        path = asset_root / "2D" / "Fire Pack" / "fx" / f"flame_{i}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (32, 32), color).save(path)
    Image.new("RGB", (32, 32), (255, 0, 0)).save(asset_root / "2D" / "fire.png")  # a pack of one picture
    with zipfile.ZipFile(asset_root / "2D" / "127327921.zip", "w") as z:
        z.writestr("Swords/sword_01.png", b"x")
        z.writestr("Swords/sword_02.png", b"x")
    scan_root(conn, "art", asset_root)
    index_archives(conn, "art", asset_root)
    return conn


def test_digest_tells_the_model_what_is_inside(library):
    text = digest(library, "art", "2D/127327921.zip", is_dir=False)
    assert "单个文件" in text and "png 2" in text and "Swords/sword_#" in text

    folder = digest(library, "art", "2D/Fire Pack", is_dir=True)
    assert "2 个文件" in folder and "fx/flame_#" in folder


def test_folder_picture_is_a_sheet_of_its_images(library, settings):
    jpeg = picture(library, settings, "art", "2D/Fire Pack", is_dir=True)
    image = Image.open(io.BytesIO(jpeg))
    assert image.format == "JPEG" and image.getpixel((80, 80))[0] > 200  # red flames
    assert picture(library, settings, "art", "Audio/Battle_Theme.mp3", is_dir=False) is None


def test_prompt_lists_every_category(library):
    prompt = build_prompt("facts")
    assert all(path in prompt for path in CATEGORY_PATHS)


@pytest.mark.parametrize(
    "answer, category",
    [
        ('{"category": "01_2D/VFX", "name": "x", "reason": "y"}', "01_2D/VFX"),
        ('好的 {"category": "/01_2D/UI/", "name": "x"} 完', "01_2D/UI"),
        ('{"category": "../../etc", "name": "x"}', UNSORTED),
        ('{"category": 5, "name": ["x"]}', UNSORTED),
    ],
)
def test_answers_are_validated(answer, category):
    assert parse_answer(answer).category == category


@pytest.mark.parametrize("answer", ["no json here", "{broken", "[1, 2]"])
def test_unusable_answers_raise(answer):
    with pytest.raises(ValueError):
        parse_answer(answer)


@pytest.mark.parametrize(
    "name, original, is_dir, expected",
    [
        ("itch - 火焰特效", "fx pack", True, "itch - 火焰特效"),
        ("Swords/../../x: y?", "127327921.zip", False, "Swords .. .. x y (127327921).zip"),
        ("..", "Old Pack", True, "Old Pack"),
        ("", "(12)_素材.zip", False, "(12)_素材.zip"),
        ("长" * 100, "a", True, "长" * 60),
    ],
)
def test_names_are_made_safe(name, original, is_dir, expected):
    assert clean_name(name, original, is_dir) == expected


def test_targets_never_collide():
    taken = {"01_2d/ui/icons", "01_2d/ui/icons (2)", "01_2d/ui/pack.zip"}
    assert target_for("01_2D/UI", "Icons", True, taken) == "01_2D/UI/Icons (3)"
    assert target_for("01_2D/UI", "pack.zip", False, taken) == "01_2D/UI/pack 2.zip"
    assert target_for("01_2D/UI", "v1.2 pack", True, set()) == "01_2D/UI/v1.2 pack"


@pytest.mark.parametrize(
    "name, original, expected",
    [
        ("Pack", "old.tar.gz", "Pack.tar.gz"),
        ("Pack.zip", "123.zip", "Pack (123).zip"),
        ("Pack.ZIP", "a.zip", "Pack.zip"),
    ],
)
def test_extensions_are_kept_once(name, original, expected):
    assert clean_name(name, original, is_dir=False) == expected


def test_numbered_target_keeps_a_compound_extension():
    assert target_for("01_2D/UI", "x.tar.gz", False, {"01_2d/ui/x.tar.gz"}) == "01_2D/UI/x 2.tar.gz"


def test_engine_effects_have_a_3d_category_and_download_sites_are_no_source():
    from assetindex.classify import CATEGORY_PATHS, strip_junk_source

    assert "02_3D/VFX" in CATEGORY_PATHS and "02_3D/VFX" in build_prompt("facts")
    assert strip_junk_source("UnrealDojo - Sword Animset Pro") == "Sword Animset Pro"


def test_classify_unit_end_to_end(library, settings, server):
    client = CaptionClient(CaptionConfig(base_url=server.base_url, model="m"))

    folder = classify_unit(client, library, settings, "art", "2D/Fire Pack", True)
    archive = classify_unit(client, library, settings, "art", "2D/127327921.zip", False)

    assert (folder.category, folder.name) == ("01_2D/VFX", "Fake - 火焰特效")
    assert archive.category == "03_Audio/SFX"  # the zip's contents aren't real images: no sheet
    assert archive.name == "Fake - 音效 (127327921).zip"
    body = server.requests[-1]["body"]
    assert body["reasoning_effort"] == "none" and "Swords/sword_#" in body["messages"][0]["content"][0]["text"]


def _caption(conn, rel_path):
    row = conn.execute(
        "SELECT c.text, c.model FROM captions c JOIN files f ON f.id = c.file_id WHERE f.rel_path = ?", (rel_path,)
    ).fetchone()
    return tuple(row) if row else None


def test_a_one_file_pack_gets_its_description_in_the_same_question(library, settings, server):
    client = CaptionClient(CaptionConfig(base_url=server.base_url, model="m"))

    proposal = classify_unit(client, library, settings, "art", "2D/fire.png", False)

    assert proposal.category == "01_2D/VFX"
    assert '"caption"' in server.requests[-1]["body"]["messages"][0]["content"][0]["text"]
    assert _caption(library, "2D/fire.png") == ("一团红色的火焰\n火焰、红色", "m")
    assert len(server.requests) == 1  # one question, not two


def test_no_description_asked_for_folders_or_cards_that_have_one(library, settings, server):
    client = CaptionClient(CaptionConfig(base_url=server.base_url, model="m"))
    classify_unit(client, library, settings, "art", "2D/Fire Pack", True)
    assert '"caption"' not in server.requests[-1]["body"]["messages"][0]["content"][0]["text"]

    classify_unit(client, library, settings, "art", "2D/fire.png", False)
    classify_unit(client, library, settings, "art", "2D/fire.png", False)  # described by the first call
    assert '"caption"' not in server.requests[-1]["body"]["messages"][0]["content"][0]["text"]


def test_description_in_the_answer_is_optional_and_cleaned(library):
    answer = parse_answer('{"category": "01_2D/VFX", "name": "x", "reason": "r", "caption": " 火焰 <think>…</think>", "keywords": "红"}')
    assert answer.caption == "火焰\n红"
    assert parse_answer('{"category": "01_2D/VFX", "name": "x", "reason": "r"}').caption == ""


def test_an_archive_without_a_sheet_is_shown_by_its_preview(library, settings, server, asset_root):
    (asset_root / "2D" / "Ice Bundle.rar").write_bytes(b"not really a rar")
    Image.new("RGB", (32, 32), (0, 0, 255)).save(asset_root / "2D" / "Ice Bundle.png")
    scan_root(library, "art", asset_root)
    client = CaptionClient(CaptionConfig(base_url=server.base_url, model="m"))

    assert "带 1 个同名的预览图" in digest(library, "art", "2D/Ice Bundle.rar", is_dir=False)
    classify_unit(client, library, settings, "art", "2D/Ice Bundle.rar", False)

    content = server.requests[-1]["body"]["messages"][0]["content"]
    assert any(part.get("type") == "image_url" for part in content)  # the preview went along
    assert _caption(library, "2D/Ice Bundle.png") == ("一块蓝色的冰晶\n火焰、红色", "m")  # described on its own card


def test_a_preview_in_a_preview_folder_is_the_archive_picture(library, settings, asset_root):
    (asset_root / "2D" / "Frost.rar").write_bytes(b"not really a rar")
    (asset_root / "2D" / "JPEG").mkdir()
    Image.new("RGB", (32, 32), (0, 0, 255)).save(asset_root / "2D" / "JPEG" / "Frost.jpg")
    scan_root(library, "art", asset_root)

    assert unit_members(library, "art", "2D/Frost.rar") == ["2D/Frost.rar", "2D/JPEG/Frost.jpg"]
    jpeg = picture(library, settings, "art", "2D/Frost.rar", is_dir=False)
    assert Image.open(io.BytesIO(jpeg)).getpixel((10, 10))[2] > 200  # the blue preview

    with library:  # the user split the preview folder: its picture is a unit of its own now
        library.execute("INSERT INTO unit_splits (root, path) VALUES ('art', '2D/JPEG')")
    assert unit_members(library, "art", "2D/Frost.rar") == ["2D/Frost.rar"]


@pytest.mark.parametrize(
    "suggested, kept",
    [
        ("UE - Military Drone 军用无人机", "Military Drone 军用无人机"),
        ("ansucai - Cute Hamsters 仓鼠", "Cute Hamsters 仓鼠"),
        ("UE角色 - Landon 角色", "Landon 角色"),
        ("虚幻4资源站 - TRANSPORT MECH", "TRANSPORT MECH"),
        ("Brushify - Forest Pack", "Brushify - Forest Pack"),  # a real publisher stays
        ("素与时素材 - 像素风黑猫", "素与时素材 - 像素风黑猫"),
    ],
)
def test_names_drop_sources_that_name_no_author(suggested, kept):
    assert clean_name(suggested, "x.7z", is_dir=False) == kept + ".7z"


def test_unusable_answer_is_an_item_problem(library, settings, server):
    server.chat_reply = "我不知道"
    client = CaptionClient(CaptionConfig(base_url=server.base_url, model="m"))
    with pytest.raises(EmbedError) as err:
        classify_unit(client, library, settings, "art", "2D/Fire Pack", True)
    assert err.value.item_problem


@pytest.mark.parametrize(
    "name, taken, expected",
    [
        ("X.zip", {"c/x.rar"}, "C/X 2.zip"),  # the preview "X.png" would go with the rar
        ("A 3D.7z", {"c/a - 3d.zip"}, "C/A 3D 2.7z"),  # separators do not tell previews apart
        ("X.zip", {"c/x (2).rar"}, "C/X 3.zip"),  # "X (2).png" previews "X"; "X 2.png" would preview "X (2).rar"
        ("X.rar", {"c/x (2).rar", "c/x.zip"}, "C/X 3.rar"),
        ("(1).zip", {"c/(2).png"}, "C/(1).zip"),  # names of only separators go with nothing
        ("X.zip", {"c/x.zip", "c/x 2.7z"}, "C/X 3.zip"),
        ("X.zip", {"d/x.rar", "c/sub/x.rar"}, "C/X.zip"),  # other folders do not matter
        ("X.zip", {"c/x.part1.rar"}, "C/X 2.zip"),  # volumes are named after the pack
        ("X.zip", {"c/x"}, "C/X.zip"),  # a folder takes no previews
        ("X.zip", {"c/x.png"}, "C/X 2.zip"),  # a lone picture would become its preview
    ],
)
def test_file_packs_get_names_their_previews_cannot_mix_up(name, taken, expected):
    assert target_for("C", name, False, taken) == expected
