import os

import pytest
from fastapi.testclient import TestClient

from assetindex import move_job, purge, scan_request
from assetindex.config import Settings
from assetindex.db import connect
from assetindex.web import create_app


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "lib"
    for rel, data in (("_待删除/Old.7z", b"x" * 10), ("_待删除/Pack/a.png", b"yy"), ("_待删除/Pack/sub/b.txt", b"z"),
                      ("02_3D/Keep.zip", b"keep"), ("_待删除x/Other.zip", b"o")):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    settings = Settings(roots={"lib": root}, data_dir=tmp_path / "data")
    conn = connect(settings.db_path)
    yield conn, settings, root
    conn.close()


def test_the_set_aside_folder_is_counted(env):
    _, _, root = env

    assert purge.contents(root) == (3, 13)
    assert purge.contents(root / "nothing") == (0, 0)


def test_a_job_empties_only_the_set_aside_folder_and_asks_for_a_scan(env):
    conn, settings, root = env

    done = move_job.run(conn, settings, {"action": "purge", "root": "lib"})

    assert done.action == "purge" and done.moved == 3 and done.total == 3 and done.error is None
    assert os.listdir(root / "_待删除") == []  # the folder itself stays
    assert (root / "02_3D/Keep.zip").exists() and (root / "_待删除x/Other.zip").exists()
    assert scan_request.is_requested(settings.data_dir)


def test_a_file_that_cannot_go_is_a_problem_and_stopping_stops(env):
    conn, settings, root = env

    def refuse(path):
        if path.endswith("Old.7z"):
            raise PermissionError("read-only")
        os.remove(path)

    done = move_job.run(conn, settings, {"action": "purge", "root": "lib"}, remove=refuse)
    assert done.moved == 2 and done.problems[0]["path"] == "_待删除/Old.7z"

    stopped = move_job.run(conn, settings, {"action": "purge", "root": "lib"}, should_stop=lambda: True)
    assert stopped.stopped and (root / "_待删除/Old.7z").exists()


def test_a_link_inside_is_removed_not_followed(env, tmp_path):
    _, _, root = env
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("keep")
    try:
        os.symlink(outside, root / "_待删除" / "link", target_is_directory=True)
    except OSError:
        pytest.skip("no symlinks here")

    purge.purge(root, lambda: False, lambda done: None)

    assert (outside / "precious.txt").exists() and not os.path.lexists(root / "_待删除" / "link")


def test_parse_takes_no_extras_for_purging(env):
    _, settings, _ = env
    assert move_job.parse({"action": "purge", "root": "lib"}, settings) == ("purge", "lib", None)
    with pytest.raises(ValueError):
        move_job.parse({"action": "purge", "root": "lib", "paths": ["a"]}, settings)


def test_the_page_sees_what_would_go(env):
    _, settings, _ = env
    client = TestClient(create_app(settings))

    assert client.get("/api/organize/trash", params={"root": "lib"}).json() == {"files": 3, "size": 13, "folder": "_待删除"}
    assert client.get("/api/organize/trash", params={"root": "x"}).status_code == 422


def test_a_set_aside_folder_that_is_a_link_is_never_emptied(tmp_path):
    root, outside = tmp_path / "lib", tmp_path / "elsewhere"
    root.mkdir()
    outside.mkdir()
    (outside / "precious.txt").write_text("keep")
    try:
        os.symlink(outside, root / "_待删除", target_is_directory=True)
    except OSError:
        pytest.skip("no symlinks here")
    settings = Settings(roots={"lib": root}, data_dir=tmp_path / "data")
    conn = connect(settings.db_path)

    done = move_job.run(conn, settings, {"action": "purge", "root": "lib"})

    assert "链接" in done.error and done.moved == 0 and (outside / "precious.txt").exists()
    with pytest.raises(purge.Refused):
        purge.contents(root)
    conn.close()


def test_no_set_aside_folder_is_nothing_to_do(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()

    assert purge.contents(root) == (0, 0) and purge.purge(root, lambda: False, lambda d: None) == (0, [], False)
