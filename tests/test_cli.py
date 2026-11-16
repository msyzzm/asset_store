from assetindex.cli import main
from assetindex.db import connect


def test_missing_root_is_reported_and_other_roots_still_scanned(monkeypatch, tmp_path, asset_root):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("ASSET_ROOTS", f"gone={tmp_path / 'unmounted'};art={asset_root}")
    monkeypatch.setenv("ASSET_DATA_DIR", str(data_dir))

    exit_code = main(["scan"])

    assert exit_code == 1
    conn = connect(data_dir / "index.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM files WHERE root = 'art'").fetchone()[0] == 5
    finally:
        conn.close()


def test_unknown_root_name_returns_2(monkeypatch, tmp_path, asset_root):
    monkeypatch.setenv("ASSET_ROOTS", f"art={asset_root}")
    monkeypatch.setenv("ASSET_DATA_DIR", str(tmp_path / "data"))
    assert main(["scan", "--root", "nope"]) == 2


def _env(monkeypatch, tmp_path, asset_root):
    monkeypatch.setenv("ASSET_ROOTS", f"art={asset_root}")
    monkeypatch.setenv("ASSET_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


def test_scan_refuses_to_run_while_another_scan_holds_the_lock(monkeypatch, tmp_path, asset_root):
    from assetindex.scanlock import scan_lock

    data_dir = _env(monkeypatch, tmp_path, asset_root)
    with scan_lock(data_dir):
        assert main(["scan"]) == 3


def test_scan_prunes_orphan_thumbnails(monkeypatch, tmp_path, asset_root):
    import os

    data_dir = _env(monkeypatch, tmp_path, asset_root)
    orphan = data_dir / "thumbs" / "999" / "999999_1.webp"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"x")
    os.utime(orphan, (1, 1))

    assert main(["scan"]) == 0
    assert not orphan.exists()


def test_retry_errors_forgets_crashed_attempts(monkeypatch, tmp_path, asset_root):
    from assetindex.attempts import begin

    data_dir = _env(monkeypatch, tmp_path, asset_root)
    conn = connect(data_dir / "index.db")
    try:
        begin(conn, 42, "list")
    finally:
        conn.close()

    main(["scan", "--retry-errors"])

    conn = connect(data_dir / "index.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM scan_attempts").fetchone()[0] == 0
    finally:
        conn.close()


def test_scan_every_repeats_until_stopped(monkeypatch, tmp_path, asset_root):
    from assetindex import cli

    _env(monkeypatch, tmp_path, asset_root)
    runs, waits = [], []
    monkeypatch.setattr(cli, "_scan", lambda *a: runs.append(a) or 0)

    def fake_wait(data_dir, seconds, **kwargs):
        waits.append(seconds)
        if len(waits) == 3:
            raise KeyboardInterrupt
        return False

    monkeypatch.setattr(cli.scan_request, "wait_for_request", fake_wait)

    assert main(["scan", "--every", "60"]) == 0
    assert len(runs) == 2 and waits == [60, 60, 60]  # never scanned: a full interval first


def test_starting_up_does_not_scan_but_keeps_the_schedule(monkeypatch, tmp_path, asset_root):
    import time

    import pytest

    from assetindex import cli, scan_request
    from assetindex.scan_request import ScanStatus

    data_dir = _env(monkeypatch, tmp_path, asset_root)
    runs, waits = [], []
    monkeypatch.setattr(cli, "_scan", lambda *a: runs.append(a) or 0)

    def fake_wait(data_dir, seconds, **kwargs):
        waits.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.scan_request, "wait_for_request", fake_wait)

    # Last scan ended 20 s ago: the next one is due in 40 s, as if nothing restarted.
    scan_request.write_status(data_dir, ScanStatus(started=time.time() - 30, finished=time.time() - 20, ok=True))
    assert main(["scan", "--every", "60"]) == 0
    assert runs == [] and waits[-1] == pytest.approx(40, abs=2)

    # Overdue (the machine was off): still no scan at startup, the schedule restarts.
    scan_request.write_status(data_dir, ScanStatus(started=1.0, finished=2.0, ok=True))
    assert main(["scan", "--every", "60"]) == 0
    assert runs == [] and waits[-1] == 60


def test_scan_takes_a_pending_request_once_it_holds_the_lock(monkeypatch, tmp_path, asset_root):
    from assetindex import scan_request

    data_dir = _env(monkeypatch, tmp_path, asset_root)
    scan_request.request_scan(data_dir)

    assert main(["scan"]) == 0
    assert not scan_request.is_requested(data_dir)


def test_request_survives_when_another_scan_holds_the_lock(monkeypatch, tmp_path, asset_root):
    from assetindex import cli, scan_request
    from assetindex.scanlock import scan_lock

    data_dir = _env(monkeypatch, tmp_path, asset_root)
    monkeypatch.setattr(cli, "LOCK_WAIT_SECONDS", 0)
    scan_request.request_scan(data_dir)

    with scan_lock(data_dir):
        assert main(["scan"]) == 3
    assert scan_request.is_requested(data_dir)


def test_scan_every_waits_at_least_the_minimum_gap(monkeypatch, tmp_path, asset_root):
    from assetindex import cli

    _env(monkeypatch, tmp_path, asset_root)
    monkeypatch.setattr(cli, "_scan", lambda *a: 0)
    seen = []

    def stop(data_dir, seconds, **kwargs):
        seen.append(kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.scan_request, "wait_for_request", stop)

    assert main(["scan", "--every", "60"]) == 0
    assert seen == [{"min_seconds": cli.MIN_SCAN_GAP_SECONDS}]


def test_status_write_failure_does_not_fail_the_scan(monkeypatch, tmp_path, asset_root):
    from assetindex import cli

    _env(monkeypatch, tmp_path, asset_root)

    def broken(*a):
        raise OSError("disk full")

    monkeypatch.setattr(cli.scan_request, "write_status", broken)
    assert main(["scan"]) == 0


def test_interrupted_scan_is_recorded_as_not_ok(monkeypatch, tmp_path, asset_root):
    import pytest

    from assetindex import cli, scan_request

    data_dir = _env(monkeypatch, tmp_path, asset_root)

    def boom(*a):
        raise RuntimeError("prune failed")

    monkeypatch.setattr(cli, "prune_thumbs", boom)
    with pytest.raises(RuntimeError):
        main(["scan"])

    status = scan_request.read_status(data_dir)
    assert status.running is False and status.ok is False


def test_scan_writes_status_for_the_web_ui(monkeypatch, tmp_path, asset_root):
    from assetindex import scan_request

    data_dir = _env(monkeypatch, tmp_path, asset_root)

    assert main(["scan"]) == 0

    status = scan_request.read_status(data_dir)
    assert status.running is False and status.ok is True
    assert status.started is not None and status.finished >= status.started


def test_check_command_reports_failures_with_nonzero_exit(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ASSET_ROOTS", f"gone={tmp_path / 'missing'}")
    monkeypatch.setenv("ASSET_DATA_DIR", str(tmp_path / "data"))

    assert main(["check"]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "root gone readable" in out


def test_sigterm_unwinds_so_attempts_are_not_counted_as_crashes():
    import signal

    import pytest

    from assetindex import cli

    previous = signal.getsignal(signal.SIGTERM)
    try:
        cli._exit_on_sigterm()
        handler = signal.getsignal(signal.SIGTERM)
        with pytest.raises(SystemExit):
            handler(signal.SIGTERM, None)
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_scan_every_survives_a_failed_round(monkeypatch, tmp_path, asset_root):
    from assetindex import cli

    _env(monkeypatch, tmp_path, asset_root)
    calls = []

    def flaky(*a):
        calls.append(a)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        return 0

    def stop_on_third_wait(data_dir, seconds, _count=[], **kwargs):
        _count.append(seconds)
        if len(_count) == 3:
            raise KeyboardInterrupt
        return False

    monkeypatch.setattr(cli, "_scan", flaky)
    monkeypatch.setattr(cli.scan_request, "wait_for_request", stop_on_third_wait)

    assert main(["scan", "--every", "60"]) == 0
    assert len(calls) == 2


def test_scan_every_rejects_non_positive_interval(monkeypatch, tmp_path, asset_root):
    import pytest

    _env(monkeypatch, tmp_path, asset_root)
    with pytest.raises(SystemExit):
        main(["scan", "--every", "0"])



def test_scan_embeds_cards_when_semantic_search_is_set_up(monkeypatch, tmp_path, asset_root):
    from assetindex import embedding
    from assetindex.embedding import EmbedConfig
    from tests.fake_embed import FakeEmbedServer

    data_dir = _env(monkeypatch, tmp_path, asset_root)
    with FakeEmbedServer() as server:
        embedding.save_config(data_dir, EmbedConfig(enabled=True, base_url=server.base_url, model="m"))
        assert main(["scan"]) == 0

        server.fail_status = 500  # API down: the scan itself still succeeds
        (asset_root / "new.txt").write_text("x")
        assert main(["scan"]) == 0

    conn = connect(data_dir / "index.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM embeddings WHERE vector IS NOT NULL").fetchone()[0] == 5
    finally:
        conn.close()



def test_unexpected_embedding_failure_does_not_fail_the_scan(monkeypatch, tmp_path, asset_root):
    from assetindex import cli, embedding, scan_request
    from assetindex.embedding import EmbedConfig

    data_dir = _env(monkeypatch, tmp_path, asset_root)
    embedding.save_config(data_dir, EmbedConfig(enabled=True, base_url="http://127.0.0.1:9/v1", model="m"))

    def boom(*a, **kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(cli, "embed_pending", boom)
    assert main(["scan"]) == 0
    assert scan_request.read_status(data_dir).ok is True


def test_stop_from_the_web_ui_ends_the_scan_and_skips_later_stages(monkeypatch, tmp_path, asset_root):
    from assetindex import cli, scan_request

    data_dir = _env(monkeypatch, tmp_path, asset_root)
    real_index = cli.index_archives

    def stop_while_listing(*a, **kw):
        scan_request.request_stop(data_dir)  # pressed while the archives are listed
        return real_index(*a, **kw)

    def must_not_run(*a, **kw):
        raise AssertionError("ran after the stop")

    monkeypatch.setattr(cli, "index_archives", stop_while_listing)
    monkeypatch.setattr(cli, "build_previews", must_not_run)
    monkeypatch.setattr(cli, "prune_thumbs", must_not_run)

    assert main(["scan"]) == 0

    status = scan_request.read_status(data_dir)
    assert status.running is False and status.stopped is True and status.ok is False
    assert not scan_request.stop_requested(data_dir)  # used up: the next scan runs normally


def test_stop_during_embedding_is_not_mistaken_for_an_embedding_failure(monkeypatch, tmp_path, asset_root):
    from assetindex import cli, embedding, scan_request
    from assetindex.embedding import EmbedConfig
    from assetindex.scan_request import ScanStopped

    data_dir = _env(monkeypatch, tmp_path, asset_root)
    embedding.save_config(data_dir, EmbedConfig(enabled=True, base_url="http://127.0.0.1:9/v1", model="m"))

    def stopped(*a, **kw):
        raise ScanStopped("stopped while embedding")

    monkeypatch.setattr(cli, "embed_pending", stopped)
    assert main(["scan"]) == 0
    assert scan_request.read_status(data_dir).stopped is True


def test_stop_pressed_while_idle_does_not_cancel_the_next_scan(monkeypatch, tmp_path, asset_root):
    from assetindex import scan_request

    data_dir = _env(monkeypatch, tmp_path, asset_root)
    scan_request.request_stop(data_dir)

    assert main(["scan"]) == 0

    status = scan_request.read_status(data_dir)
    assert status.ok is True and status.stopped is False


def test_scan_status_shows_which_step_it_is_on(monkeypatch, tmp_path, asset_root):
    from assetindex import cli

    _env(monkeypatch, tmp_path, asset_root)
    statuses = []
    monkeypatch.setattr(cli, "_set_status", lambda data_dir, status: statuses.append(status))

    assert main(["scan"]) == 0

    running = [s for s in statuses if s.running]
    assert {s.phase for s in running} >= {"walk", "archives", "previews"}
    assert all(s.root == "art" for s in running if s.phase in ("walk", "archives", "previews"))
    assert statuses[-1].running is False and statuses[-1].phase is None
