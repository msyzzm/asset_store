from assetindex import scan_request
from assetindex.scan_request import ScanStatus


def test_request_is_seen_then_taken_once(tmp_path):
    assert not scan_request.is_requested(tmp_path)

    scan_request.request_scan(tmp_path)
    scan_request.request_scan(tmp_path)  # pressing twice is still one request

    assert scan_request.is_requested(tmp_path)
    assert scan_request.take_request(tmp_path) is True
    assert scan_request.take_request(tmp_path) is False
    assert not scan_request.is_requested(tmp_path)


def test_status_round_trips(tmp_path):
    status = ScanStatus(running=False, started=100.0, finished=160.5, ok=True)

    scan_request.write_status(tmp_path, status)

    assert scan_request.read_status(tmp_path) == status


def test_stop_drops_a_pending_request_and_can_be_cleared(tmp_path):
    scan_request.request_scan(tmp_path)

    scan_request.request_stop(tmp_path)

    assert not scan_request.is_requested(tmp_path)
    assert scan_request.stop_requested(tmp_path)
    scan_request.clear_stop(tmp_path)
    assert not scan_request.stop_requested(tmp_path)


def test_stopped_status_round_trips(tmp_path):
    status = ScanStatus(started=1.0, finished=2.0, ok=False, stopped=True)

    scan_request.write_status(tmp_path, status)

    assert scan_request.read_status(tmp_path) == status


def test_missing_or_corrupt_status_reads_as_never_scanned(tmp_path):
    assert scan_request.read_status(tmp_path) == ScanStatus()

    (tmp_path / scan_request.STATUS_NAME).write_text("{not json", encoding="utf-8")
    assert scan_request.read_status(tmp_path) == ScanStatus()

    (tmp_path / scan_request.STATUS_NAME).write_text('{"running": "yes", "started": []}', encoding="utf-8")
    assert scan_request.read_status(tmp_path) == ScanStatus()


class _FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_wait_returns_false_after_full_interval_without_request(tmp_path):
    clock = _FakeClock()

    assert scan_request.wait_for_request(tmp_path, 12, sleep=clock.sleep, clock=clock.time) is False
    assert clock.sleeps == [5, 5, 2]


def test_wait_returns_early_when_requested(tmp_path):
    clock = _FakeClock()

    def sleep(seconds):
        clock.sleep(seconds)
        if len(clock.sleeps) == 2:
            scan_request.request_scan(tmp_path)

    assert scan_request.wait_for_request(tmp_path, 3600, sleep=sleep, clock=clock.time) is True
    assert clock.sleeps == [5, 5]


def test_wait_holds_an_early_request_until_the_minimum_gap(tmp_path):
    clock = _FakeClock()
    scan_request.request_scan(tmp_path)

    assert scan_request.wait_for_request(tmp_path, 3600, min_seconds=12, sleep=clock.sleep, clock=clock.time) is True
    assert clock.now == 12


def test_progress_round_trips(tmp_path):
    status = ScanStatus(running=True, started=1.0, root="nas", phase="archives", done=3, total=10)

    scan_request.write_status(tmp_path, status)

    assert scan_request.read_status(tmp_path) == status
