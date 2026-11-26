"""Saved archive passwords: tried on locked wrappers when a lift names none; never shown."""
import pytest
from fastapi.testclient import TestClient

from assetindex import archive_passwords
from assetindex.config import Settings
from assetindex.web import create_app

PAGE = {"X-Requested-With": "fetch"}


def test_nothing_saved_is_no_passwords(tmp_path):
    assert archive_passwords.load(tmp_path) == ()


def test_saved_passwords_come_back_in_order(tmp_path):
    archive_passwords.save(tmp_path, ("b", "a"))

    assert archive_passwords.load(tmp_path) == ("b", "a")


def test_a_broken_file_is_no_passwords(tmp_path):
    (tmp_path / archive_passwords.CONFIG_NAME).write_text("{not json", encoding="utf-8")

    assert archive_passwords.load(tmp_path) == ()


def test_add_appends_new_ones_and_skips_known_ones():
    assert archive_passwords.apply_update(("a",), {"add": ["b", "a", "b"]}) == ("a", "b")


def test_passwords_replaces_the_whole_list():
    assert archive_passwords.apply_update(("a", "b"), {"passwords": ["c"]}) == ("c",)
    assert archive_passwords.apply_update(("a",), {"passwords": []}) == ()


@pytest.mark.parametrize("update", [
    {},
    {"add": "a"},
    {"add": [""]},
    {"add": [1]},
    {"add": ["x" * (archive_passwords.MAX_LENGTH + 1)]},
    {"add": [f"p{i}" for i in range(archive_passwords.MAX_COUNT + 1)]},
    {"add": ["a"], "passwords": ["b"]},
    {"show": True},
])
def test_a_bad_update_is_refused(update):
    with pytest.raises(ValueError):
        archive_passwords.apply_update((), update)


def test_the_api_only_ever_tells_how_many(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    settings = Settings(roots={"lib": root}, data_dir=tmp_path / "data")
    client = TestClient(create_app(settings))

    assert client.put("/api/settings/passwords", json={"add": ["secret"]}).status_code == 403
    saved = client.put("/api/settings/passwords", headers=PAGE, json={"add": ["secret", "other"]})
    assert saved.status_code == 200 and saved.json() == {"count": 2}
    assert client.get("/api/settings/passwords").json() == {"count": 2}
    assert "secret" not in client.get("/api/settings/passwords").text
    assert client.put("/api/settings/passwords", headers=PAGE, json={"add": [""]}).status_code == 422
    assert archive_passwords.load(settings.data_dir) == ("secret", "other")
