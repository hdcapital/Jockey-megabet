"""Raw-response archive: gzipped on write, readable back, pruned by age."""

from __future__ import annotations

import gzip
import json
from datetime import date, timedelta

from app.http import prune_archive, read_archive


def test_prune_removes_only_old_day_directories(tmp_path):
    src = tmp_path / "sportsbet"
    old = src / (date.today() - timedelta(days=30)).isoformat()
    recent = src / (date.today() - timedelta(days=2)).isoformat()
    odd = src / "not-a-date"
    for d in (old, recent, odd):
        d.mkdir(parents=True)
        (d / "x.json.gz").write_bytes(b"x")
    removed = prune_archive(tmp_path, keep_days=14)
    assert removed == 1
    assert not old.exists()
    assert (recent / "x.json.gz").exists()
    assert (odd / "x.json.gz").exists(), "unrecognised directories are left alone"


def test_prune_is_a_no_op_when_disabled_or_missing(tmp_path):
    assert prune_archive(tmp_path / "missing", 14) == 0
    (tmp_path / "s" / "2000-01-01").mkdir(parents=True)
    (tmp_path / "s" / "2000-01-01" / "a").write_bytes(b"a")
    assert prune_archive(tmp_path, 0) == 0


def test_read_archive_handles_gzip_and_plain(tmp_path):
    meta = {"source": "t", "url": "u", "sha256": "abc"}
    body = b'{"hello": 1}'
    gz = tmp_path / "a.json.gz"
    with gzip.open(gz, "wb") as fh:
        fh.write(json.dumps(meta).encode() + b"\n" + body)
    plain = tmp_path / "b.json"
    plain.write_bytes(json.dumps(meta).encode() + b"\n" + body)
    for path in (gz, plain):
        got_meta, got_body = read_archive(path)
        assert got_meta == meta and got_body == body
