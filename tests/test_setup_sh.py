"""deploy/setup.sh has no other test coverage -- it's a shell script that
provisions a real box, not something this suite can run end to end. These
tests read its source text and check the shape of the systemd units it
generates, which is enough to catch the specific regression that matters
here: someone editing one of discover/scan's ExecStart lines (or the
scan unit's upload wiring) without touching its twin.
"""
import pathlib
import re

SETUP = pathlib.Path(__file__).parent.parent / "deploy" / "setup.sh"


def _read():
    return SETUP.read_text()


def _unit_block(name, text):
    """Pull one write_unit heredoc's body out by service/timer name."""
    m = re.search(r"write_unit %s <<EOF\n(.*?)\nEOF" % re.escape(name), text, re.S)
    assert m, f"no write_unit block found for {name} in deploy/setup.sh"
    return m.group(1)


def test_discover_and_scan_are_wrapped_in_the_same_flock():
    """Two sweep processes at WORKERS=20 each, from one residential IP,
    hitting apionline.homedepot.com at once looks exactly like the burst
    that gets a datacentre range refused (tools/README.md, "Where it can
    run"). discover fires at $HOUR:00 with up to a 90-minute budget and
    scan fires ten minutes later -- now that they're separate systemd
    units (so a blocked discover can't take scan down with it), nothing
    but a shared lock stops them overlapping."""
    text = _read()
    discover = _unit_block("pennyrun-discover.service", text)
    scan = _unit_block("pennyrun-scan.service", text)

    discover_exec = re.search(r"^ExecStart=(.*)$", discover, re.M).group(1)
    scan_exec = re.search(r"^ExecStart=(.*)$", scan, re.M).group(1)

    assert discover_exec.startswith("/usr/bin/flock $LOCK "), discover_exec
    assert scan_exec.startswith("/usr/bin/flock $LOCK "), scan_exec

    # A blocking flock (no -n) so the second unit waits rather than either
    # failing outright or running concurrently with the first.
    assert " -n " not in discover_exec
    assert " -n " not in scan_exec


def test_scan_gets_a_default_api_url_and_the_ingest_token():
    """A systemd unit does not inherit anyone's login environment --
    scan() only uploads when PENNYRUN_API and PENNYRUN_INGEST_TOKEN are
    both set in its own process (tests/test_sweep_upload.py), so the unit
    has to supply both itself rather than relying on the box's shell."""
    text = _read()
    scan = _unit_block("pennyrun-scan.service", text)

    assert "Environment=PENNYRUN_API=https://$HOST" in scan
    # "-" prefix: a missing/unreadable token file must not stop the scan
    # itself (and clearance.json) from running -- scan() already treats a
    # missing token as "don't upload", tested in test_sweep_upload.py.
    assert re.search(r"^EnvironmentFile=-\$INGEST_ENV$", scan, re.M)


def test_old_combined_sweep_unit_is_retired():
    """Pre-Task-9 boxes have pennyrun-sweep.{service,timer}: one unit
    running `discover scan` together through /usr/bin/python3, which
    never had curl_cffi. Left in place it's a red unit every night at the
    same minute pennyrun-scan.timer now runs, and upgrading is supposed
    to be safe to re-run, not leave a dead unit behind."""
    text = _read()
    assert ("systemctl disable --now pennyrun-sweep.timer "
            "pennyrun-sweep.service") in text
    assert ("rm -f /etc/systemd/system/pennyrun-sweep.timer "
            "/etc/systemd/system/pennyrun-sweep.service") in text


def test_api_service_is_installed_and_started():
    text = _read()
    assert "write_unit pennyrun-api.service" in text
    assert "systemctl enable --now pennyrun-api.service" in text
    api = _unit_block("pennyrun-api.service", text)
    assert "EnvironmentFile=$DB_ENV" in api
    assert "EnvironmentFile=$INGEST_ENV" in api


def test_database_and_migrations_are_provisioned():
    text = _read()
    assert "create role" in text
    assert "create database" in text
    assert "-m db.migrate" in text
    assert "-m db.seed" in text


def test_ingest_token_and_db_credential_are_never_world_or_group_readable():
    """A `> file` followed by a later `chmod 600` leaves the secret
    readable at the process's default umask in between; `(umask 077; ...)`
    around the write closes that window instead of narrowing it after the
    fact."""
    text = _read()
    assert re.search(r"\(umask 077;.*DB_ENV", text, re.S)
    assert re.search(r"\(umask 077;.*INGEST_ENV", text, re.S)
