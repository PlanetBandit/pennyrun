import json
import pathlib

from tools import sweep, upload

FIX = pathlib.Path(__file__).parent / "fixtures"


def _scan_fixture(tmp_path, monkeypatch):
    """Wire scan() to a temp POOL/STORES/HOT/OUT/PRICES and a product fixture
    that always comes back on clearance, with no shipped-copy fallback."""
    stores = tmp_path / "stores.json"
    stores.write_text(json.dumps([["2502", "Test Store"]]))
    pool = tmp_path / "pool.json"
    pool.write_text(json.dumps(
        {"pool": [["Black Mineral Surface Roll Low Slope Roofing", "204767783", "Building"]]}))
    hot = tmp_path / "hot.json"
    out = tmp_path / "clearance.json"
    prices = tmp_path / "prices.json"

    monkeypatch.setattr(sweep, "STORES", str(stores))
    monkeypatch.setattr(sweep, "POOL", str(pool))
    monkeypatch.setattr(sweep, "HOT", str(hot))
    monkeypatch.setattr(sweep, "OUT", str(out))
    monkeypatch.setattr(sweep, "PRICES", str(prices))
    monkeypatch.setattr(sweep, "SEED", str(tmp_path))  # no shipped-copy fallback

    product = json.loads((FIX / "products_ok.json").read_text())["data"]["products"]

    def fake_call(ids, sid, lite=False):
        return product

    monkeypatch.setattr(sweep, "call", fake_call)
    return out


def test_scan_does_not_upload_without_api_and_token(tmp_path, monkeypatch):
    _scan_fixture(tmp_path, monkeypatch)
    monkeypatch.delenv("PENNYRUN_API", raising=False)
    monkeypatch.delenv("PENNYRUN_INGEST_TOKEN", raising=False)

    def fail_send(rows, base, token):
        raise AssertionError("upload.send must not be called without both env vars")

    monkeypatch.setattr("tools.upload.send", fail_send)
    sweep.scan()  # must not raise


def test_scan_uploads_when_api_and_token_are_set(tmp_path, monkeypatch):
    out = _scan_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("PENNYRUN_API", "http://x")
    monkeypatch.setenv("PENNYRUN_INGEST_TOKEN", "t")

    sent = {}

    def fake_send(rows, base, token):
        sent["rows"] = rows
        sent["base"] = base
        sent["token"] = token
        return {"accepted": len(rows), "rejected": 0}

    monkeypatch.setattr("tools.upload.send", fake_send)
    sweep.scan()

    assert sent["base"] == "http://x"
    assert sent["token"] == "t"
    assert len(sent["rows"]) == 1
    assert out.exists(), "clearance.json must still be written"


def test_scan_upload_failure_does_not_fail_the_scan(tmp_path, monkeypatch):
    """Collection succeeding and delivery to the droplet failing are
    different events -- an upload exception must not propagate out of
    scan() and clearance.json must already be on disk."""
    out = _scan_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("PENNYRUN_API", "http://x")
    monkeypatch.setenv("PENNYRUN_INGEST_TOKEN", "t")

    def broken_send(rows, base, token):
        raise OSError("droplet unreachable")

    monkeypatch.setattr("tools.upload.send", broken_send)
    sweep.scan()  # must not raise
    assert out.exists()


def test_scan_surfaces_partial_progress_on_a_mid_batch_failure(tmp_path, monkeypatch, capsys):
    """A human re-running the scan after a partial upload failure needs to
    know some rows already landed (permanently -- observation is append-
    only) rather than assuming the whole batch needs resending blind."""
    out = _scan_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("PENNYRUN_API", "http://x")
    monkeypatch.setenv("PENNYRUN_INGEST_TOKEN", "t")

    def partial_send(rows, base, token):
        raise upload.UploadError({"accepted": 7, "rejected": 1}, RuntimeError("timed out"))

    monkeypatch.setattr("tools.upload.send", partial_send)
    sweep.scan()  # must not raise

    assert out.exists()
    output = capsys.readouterr().out
    assert "7 accepted" in output
    assert "1 rejected" in output
