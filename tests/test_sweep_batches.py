import json

import pytest

from tools import hdclient, sweep


def test_in_batches_deduplicates_ids_before_chunking():
    """Real sitemap slices can repeat an id across shards, and
    hdclient.products() raises ValueError on a batch containing a
    duplicate -- so in_batches must never hand fn a chunk with repeats,
    even when the input list has them."""
    ids = ["1", "2", "1", "3", "2"]
    seen_chunks = []

    def fn(chunk):
        seen_chunks.append(chunk)
        return chunk

    run = sweep.in_batches(ids, fn)
    for chunk in seen_chunks:
        assert len(chunk) == len(set(chunk)), "fn got a chunk with a duplicate id"
    assert sorted(run.out) == ["1", "2", "3"]
    assert run.refused == 0
    assert run.unreachable == 0


def test_in_batches_never_exceeds_batch_cap_per_chunk():
    ids = [str(i) for i in range(40)]

    def fn(chunk):
        assert len(chunk) <= sweep.BATCH
        return chunk

    sweep.in_batches(ids, fn)


def test_in_batches_counts_refusals_without_losing_other_chunks():
    """A refused chunk used to vanish into an empty list, indistinguishable
    from eight stores with nothing on clearance. Now it's counted, and the
    chunks that succeeded still come back."""
    ids = [str(i) for i in range(32)]  # two chunks of 16

    def fn(chunk):
        if chunk[0] == "0":
            raise hdclient.Refused("HTTP 206: Generic Errors API")
        return chunk

    run = sweep.in_batches(ids, fn)
    assert run.refused == 1
    assert run.unreachable == 0
    assert run.chunks == 2
    assert sorted(run.out) == [str(i) for i in range(16, 32)]
    assert run.first_refused == "HTTP 206: Generic Errors API"


def test_in_batches_counts_refused_and_unreachable_separately():
    """'They said no' and 'we never got an answer' need different responses
    at 5am -- they must not collapse into one number."""
    ids = [str(i) for i in range(48)]  # three chunks of 16

    def fn(chunk):
        if chunk[0] == "0":
            raise hdclient.Refused("HTTP 206")
        if chunk[0] == "16":
            raise hdclient.Unreachable("TimeoutError")
        return chunk

    run = sweep.in_batches(ids, fn)
    assert run.refused == 1
    assert run.unreachable == 1
    assert run.chunks == 3
    assert sorted(run.out) == [str(i) for i in range(32, 48)]
    assert run.first_refused == "HTTP 206"
    assert run.first_unreachable == "TimeoutError"


def test_in_batches_priced_counts_only_successful_chunks():
    """priced is what actually got sent and answered, not the pre-dedup
    length of the input window -- a refused chunk's ids were not priced."""
    ids = [str(i) for i in range(32)]  # two chunks of 16

    def fn(chunk):
        if chunk[0] == "0":
            raise hdclient.Refused("HTTP 206")
        return chunk

    run = sweep.in_batches(ids, fn)
    assert run.priced == 16


def _discover_fixture(tmp_path, monkeypatch, shard_fetch):
    """Wire discover() to a temp STORES/CURSOR/HOT and a controllable shard
    fetch, with no shipped-copy fallback that could mask a skipped write."""
    stores = tmp_path / "stores.json"
    stores.write_text(json.dumps([["2502", "Test Store"]]))
    cursor = tmp_path / "cursor.json"
    cursor_body = json.dumps({"shard": 0, "offset": 0, "store": 0})
    cursor.write_text(cursor_body)
    hot = tmp_path / "hot.json"

    monkeypatch.setattr(sweep, "STORES", str(stores))
    monkeypatch.setattr(sweep, "CURSOR", str(cursor))
    monkeypatch.setattr(sweep, "HOT", str(hot))
    monkeypatch.setattr(sweep, "SEED", str(tmp_path))  # no shipped-copy fallback

    index_xml = ("<sitemapindex><sitemap>"
                 "<loc>https://www.homedepot.com/sitemap/P/PIP-0.xml</loc>"
                 "</sitemap></sitemapindex>")

    def fake_get(url, timeout=60, expect_xml=True):
        if url == sweep.SITEMAP:
            return index_xml
        return shard_fetch(url)

    monkeypatch.setattr(sweep, "get", fake_get)
    return cursor, cursor_body, hot


def test_discover_dies_loudly_when_a_shard_fetch_is_refused(monkeypatch, tmp_path):
    """A shard that comes back as an Akamai challenge page must not read as
    an empty slice of the catalogue -- the old code would parse zero <loc>
    entries out of the challenge HTML and quietly move on. discover() must
    stop and say so instead."""
    def shard_fetch(url):
        raise hdclient.Refused("HTTP 200 with a non-XML body -- a challenge page")

    _discover_fixture(tmp_path, monkeypatch, shard_fetch)

    with pytest.raises(SystemExit):
        sweep.discover()


def test_discover_total_refusal_leaves_hot_and_cursor_untouched(monkeypatch, tmp_path):
    """A run where every chunk in the slice was refused must not write HOT
    (which would age out entries a working run would have refreshed) or
    CURSOR (which would burn the slice for weeks by advancing past products
    that were never actually priced). die() alone isn't proof of that --
    only the files being untouched is."""
    shard_xml = "".join(
        "<url><loc>https://www.homedepot.com/p/x/%d</loc></url>" % (200000000 + i)
        for i in range(20))

    def shard_fetch(url):
        return "<urlset>" + shard_xml + "</urlset>"

    cursor, cursor_before, hot = _discover_fixture(tmp_path, monkeypatch, shard_fetch)

    def dying_call(ids, sid, lite=False):
        raise hdclient.Refused("HTTP 206: Generic Errors API")

    monkeypatch.setattr(sweep, "call", dying_call)

    assert not hot.exists()
    with pytest.raises(SystemExit):
        sweep.discover()

    assert not hot.exists(), "HOT must not be written on a fully-refused slice"
    assert cursor.read_text() == cursor_before, "CURSOR must not advance on a fully-refused slice"


def test_discover_partial_refusal_below_threshold_still_completes(monkeypatch, tmp_path):
    """A handful of bad chunks in a slice of many must not stop the whole
    run -- only a slice that's mostly refused is untrustworthy."""
    shard_xml = "".join(
        "<url><loc>https://www.homedepot.com/p/x/%d</loc></url>" % (200000000 + i)
        for i in range(64))  # four chunks of 16

    def shard_fetch(url):
        return "<urlset>" + shard_xml + "</urlset>"

    cursor, cursor_before, hot = _discover_fixture(tmp_path, monkeypatch, shard_fetch)

    calls = {"n": 0}

    def mostly_ok_call(ids, sid, lite=False):
        calls["n"] += 1
        if calls["n"] == 1:
            raise hdclient.Refused("HTTP 206")
        return []

    monkeypatch.setattr(sweep, "call", mostly_ok_call)

    sweep.discover()  # must not raise

    assert hot.exists()
    assert cursor.read_text() != cursor_before, "a mostly-good slice should still advance"
