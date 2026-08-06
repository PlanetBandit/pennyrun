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

    out, refused, total = in_batches_call(ids, fn)
    for chunk in seen_chunks:
        assert len(chunk) == len(set(chunk)), "fn got a chunk with a duplicate id"
    assert sorted(out) == ["1", "2", "3"]
    assert refused == 0


def test_in_batches_never_exceeds_batch_cap_per_chunk():
    ids = [str(i) for i in range(40)]

    def fn(chunk):
        assert len(chunk) <= sweep.BATCH
        return chunk

    in_batches_call(ids, fn)


def test_in_batches_counts_refusals_without_losing_other_chunks():
    """A refused chunk used to vanish into an empty list, indistinguishable
    from eight stores with nothing on clearance. Now it's counted, and the
    chunks that succeeded still come back."""
    ids = [str(i) for i in range(32)]  # two chunks of 16

    def fn(chunk):
        if chunk[0] == "0":
            raise hdclient.Refused("HTTP 206: Generic Errors API")
        return chunk

    out, refused, total = in_batches_call(ids, fn)
    assert refused == 1
    assert total == 2
    assert sorted(out) == [str(i) for i in range(16, 32)]


def test_in_batches_counts_unreachable_as_refused_too():
    ids = ["1"]

    def fn(chunk):
        raise hdclient.Unreachable("TimeoutError")

    out, refused, total = in_batches_call(ids, fn)
    assert out == []
    assert refused == 1
    assert total == 1


def in_batches_call(ids, fn):
    return sweep.in_batches(ids, fn)


def test_discover_dies_loudly_when_a_shard_fetch_is_refused(monkeypatch, tmp_path):
    """A shard that comes back as an Akamai challenge page must not read as
    an empty slice of the catalogue -- the old code would parse zero <loc>
    entries out of the challenge HTML and quietly move on. discover() must
    stop and say so instead."""
    stores = tmp_path / "stores.json"
    stores.write_text(json.dumps([["2502", "Test Store"]]))
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"shard": 0, "offset": 0, "store": 0}))

    monkeypatch.setattr(sweep, "STORES", str(stores))
    monkeypatch.setattr(sweep, "CURSOR", str(cursor))
    monkeypatch.setattr(sweep, "HOT", str(tmp_path / "hot.json"))
    monkeypatch.setattr(sweep, "SEED", str(tmp_path))  # no shipped-copy fallback

    index_xml = ("<sitemapindex><sitemap>"
                 "<loc>https://www.homedepot.com/sitemap/P/PIP-0.xml</loc>"
                 "</sitemap></sitemapindex>")

    def fake_get(url, timeout=60, expect_xml=True):
        if url == sweep.SITEMAP:
            return index_xml
        raise hdclient.Refused("HTTP 200 with a non-XML body -- a challenge page")

    monkeypatch.setattr(sweep, "get", fake_get)

    with pytest.raises(SystemExit):
        sweep.discover()
