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


def test_lock_wait_time_is_budgeted_on_top_of_run_time_not_instead_of_it():
    """systemd starts a unit's TimeoutStartSec clock at ExecStart, which
    for both units is the moment `flock $LOCK` starts *blocking* -- not
    the moment the sweep itself begins. A TimeoutStartSec of just
    SWEEP_RUN_BUDGET would let systemd SIGTERM a scan that waited out a
    full discover run after only ~10 real minutes of work instead of the
    90 it was sized for. Both units need SWEEP_RUN_BUDGET (wait) +
    SWEEP_RUN_BUDGET (run) = SWEEP_LOCK_TIMEOUT, not SWEEP_RUN_BUDGET on
    its own."""
    text = _read()

    run_budget = re.search(r"^SWEEP_RUN_BUDGET=(\d+)$", text, re.M)
    lock_timeout = re.search(r"^SWEEP_LOCK_TIMEOUT=\$\(\(SWEEP_RUN_BUDGET \* 2\)\)$", text, re.M)
    assert run_budget, "expected a SWEEP_RUN_BUDGET=<seconds> assignment"
    assert lock_timeout, "expected SWEEP_LOCK_TIMEOUT derived as SWEEP_RUN_BUDGET * 2"

    discover = _unit_block("pennyrun-discover.service", text)
    scan = _unit_block("pennyrun-scan.service", text)
    assert re.search(r"^TimeoutStartSec=\$SWEEP_LOCK_TIMEOUT$", discover, re.M)
    assert re.search(r"^TimeoutStartSec=\$SWEEP_LOCK_TIMEOUT$", scan, re.M)
    # Neither unit's timeout may be hardcoded back down to just the run
    # budget -- that's exactly the bug being guarded against here.
    assert "TimeoutStartSec=$SWEEP_RUN_BUDGET" not in discover
    assert "TimeoutStartSec=$SWEEP_RUN_BUDGET" not in scan


def test_scan_gets_a_default_api_url_and_the_ingest_token():
    """A systemd unit does not inherit anyone's login environment --
    scan() only uploads when PENNYRUN_API and PENNYRUN_INGEST_TOKEN are
    both set in its own process (tests/test_sweep_upload.py), so the unit
    has to supply both itself rather than relying on the box's shell."""
    text = _read()
    scan = _unit_block("pennyrun-scan.service", text)

    assert re.search(r'^API_URL="\$\{PENNYRUN_API:-https://\$HOST\}"$', text, re.M)
    assert "Environment=PENNYRUN_API=$API_URL" in scan

    # "-" prefix: a missing/unreadable token file must not stop the scan
    # itself (and clearance.json) from running -- scan() already treats a
    # missing token as "don't upload", tested in test_sweep_upload.py.
    assert re.search(r'^\tSCAN_INGEST_ENV="\$INGEST_ENV"$', text, re.M)
    assert re.search(r'^INGEST_LINE="EnvironmentFile=-\$SCAN_INGEST_ENV"$', text, re.M)
    assert "$INGEST_LINE" in scan


def test_setup_sh_honours_an_external_api_url_and_ingest_token():
    """CRITICAL 1 of the branch review: the deploy kit only supported the
    topology the architecture proves impossible -- running setup.sh on
    the droplet pointed its own scan timer at itself, with no way to aim
    a collector at a *different* droplet. PENNYRUN_API and
    PENNYRUN_INGEST_TOKEN, when present in setup.sh's own environment,
    must override the self-referential defaults so a home box can point
    its scan timer at a droplet that actually serves the API."""
    text = _read()

    assert re.search(
        r'API_URL="\$\{PENNYRUN_API:-https://\$HOST\}"', text), \
        "PENNYRUN_API must override the https://$HOST default when set"

    assert re.search(
        r'if \[ -n "\$\{PENNYRUN_INGEST_TOKEN:-\}" \]; then', text), \
        "an explicit PENNYRUN_INGEST_TOKEN must be honoured, not just the local file"
    assert 'SCAN_INGEST_ENV="$UPSTREAM_INGEST_ENV"' in text


def test_upstream_ingest_token_is_written_to_its_own_0600_file():
    """Re-review Medium 1: an explicit PENNYRUN_INGEST_TOKEN used to be
    embedded directly in the unit as `Environment=PENNYRUN_INGEST_TOKEN=...`
    -- write_unit's plain `cat >` leaves that file at the process's
    default umask (0644, world-readable), with no chmod ever run on it,
    unlike every other secret this script writes. It must go through the
    same (umask 077; ...) + chmod 600 discipline as $DB_ENV/$INGEST_ENV,
    and the unit must reference it only via EnvironmentFile= -- one
    secret-handling rule, not two."""
    text = _read()

    assert "Environment=PENNYRUN_INGEST_TOKEN=$PENNYRUN_INGEST_TOKEN" not in text, \
        "the token must never be embedded directly in a unit body"

    assert re.search(r"\(umask 077;.*UPSTREAM_INGEST_ENV", text, re.S)
    assert re.search(r'^\tchmod 600 "\$UPSTREAM_INGEST_ENV"', text, re.M)


def test_sweep_timers_are_gated_on_sweepable():
    """CRITICAL 2 / re-review Low 4 of the branch review: a BLOCKED or
    UNKNOWN host used to get every sweep timer enabled unconditionally --
    ~3,750 refused discover chunks, ~408 refused scan chunks, and 772
    refused harvest requests a week, fired at Home Depot forever, from a
    host that can never produce a hit. The units must still be written
    either way (so enabling them later is one command, not a re-run of
    this script) but only *enabled* when SWEEPABLE=yes -- and harvest is
    gated exactly like discover/scan, not left unconditional."""
    text = _read()
    m = re.search(
        r'systemctl daemon-reload\n(?:#.*\n)*'
        r'SWEEP_TIMERS="pennyrun-discover\.timer pennyrun-scan\.timer pennyrun-harvest\.timer"\n'
        r'if \[ "\$SWEEPABLE" = "yes" \]; then\n'
        r'(.*?)\nelif \[ "\$CHECK_STATUS" = "1" \]; then\n'
        r'(.*?)\nelse\n(.*?)\nfi\n', text, re.S)
    assert m, "expected all three sweep timers gated on SWEEPABLE/CHECK_STATUS"
    yes_body, blocked_body, unknown_body = m.groups()

    assert "systemctl enable --now $SWEEP_TIMERS" in yes_body

    # BLOCKED enforces the state even if a prior run left the timers
    # enabled -- a definite, durable answer must not leave them firing.
    assert "systemctl disable --now $SWEEP_TIMERS" in blocked_body

    # UNKNOWN touches nothing -- not evidence of a refusal, and a
    # transient blip must not silently kill a working collector.
    assert "systemctl enable" not in unknown_body
    assert "systemctl disable" not in unknown_body

    # the units themselves are still written before this point, whether
    # or not they end up enabled
    assert text.index("write_unit pennyrun-discover.service") < text.index(m.group(0))
    assert text.index("write_unit pennyrun-scan.service") < text.index(m.group(0))
    assert text.index("write_unit pennyrun-harvest.service") < text.index(m.group(0))


def test_checkhost_blocked_and_unknown_are_narrated_differently():
    """A ledger item from the review: checkhost has three outcomes (0
    GOOD, 1 BLOCKED, 2 UNKNOWN), and narrating any non-zero exit as
    'refused' sends a local TLS/network problem (UNKNOWN) down the same
    road as a genuine Home Depot refusal (BLOCKED) -- exactly the
    confusion checkhost.py's own verdict() exists to prevent. setup.sh
    must capture and branch on the real exit code, and the old "will
    start working by itself" reassurance -- false for a datacentre
    address, which does not self-heal -- must be gone."""
    text = _read()

    assert re.search(r'\|\|\s*CHECK_STATUS=\$\?', text), \
        "expected the real checkhost exit code to be captured, not just pass/fail"
    assert re.search(r'if \[ "\$CHECK_STATUS" = "1" \]; then', text)
    assert "BLOCKED" in text
    assert "UNKNOWN" in text
    assert "will start working by itself" not in text


def test_old_combined_sweep_unit_is_retired_before_new_units_are_installed():
    """Pre-Task-9 boxes have pennyrun-sweep.{service,timer}: one unit
    running `discover scan` together through /usr/bin/python3, which
    never had curl_cffi. Left in place it's a red unit every night at the
    same minute pennyrun-scan.timer now runs, and upgrading is supposed
    to be safe to re-run, not leave a dead unit behind. A plain substring
    check can't tell a real, existence-guarded removal from the same
    words sitting in a comment or dead code, so this parses the actual
    `if [ -f ... ]; then ... fi` block and checks both commands are
    inside it -- and that the whole block runs before the replacement
    units are written, not after."""
    text = _read()
    m = re.search(
        r"if \[ -f /etc/systemd/system/pennyrun-sweep\.timer \] \|\| "
        r"\[ -f /etc/systemd/system/pennyrun-sweep\.service \]; then\n"
        r"(.*?)\nfi\n", text, re.S)
    assert m, "no existence-guarded removal block for the old pennyrun-sweep unit"
    body = m.group(1)

    assert re.search(
        r"^\tsystemctl disable --now pennyrun-sweep\.timer pennyrun-sweep\.service\b",
        body, re.M), "disable --now must be inside the guarded block, not just present somewhere"
    assert re.search(
        r"^\trm -f /etc/systemd/system/pennyrun-sweep\.timer "
        r"/etc/systemd/system/pennyrun-sweep\.service$",
        body, re.M), "rm -f must be inside the guarded block, not just present somewhere"

    assert text.index(m.group(0)) < text.index("write_unit pennyrun-discover.service"), \
        "the old unit must be retired before the new ones are installed"


def test_api_service_is_installed_and_started():
    text = _read()
    assert "write_unit pennyrun-api.service" in text
    assert "systemctl enable --now pennyrun-api.service" in text
    api = _unit_block("pennyrun-api.service", text)
    assert "EnvironmentFile=$DB_ENV" in api
    assert "EnvironmentFile=$INGEST_ENV" in api


def _db_setup_block(text):
    m = re.search(r'if \[ ! -f "\$DB_ENV" \]; then\n(.*?)\nelse\n', text, re.S)
    assert m, 'no guarded database-credential block (gated on "$DB_ENV" absence)'
    return m.group(1)


def test_database_role_and_credential_are_created_inside_the_guard():
    """A bare substring check can't tell 'create role' guarding real
    provisioning from the same words in a comment, or catch them running
    unconditionally (which would error on every re-run once the role
    already exists). This parses the actual `if [ ! -f "$DB_ENV" ]`
    block and checks the role, its password, the database and the
    credential file write are all inside it."""
    body = _db_setup_block(_read())
    assert re.search(r'create role \\"\$DB_ROLE\\" login', body)
    assert re.search(r'create database \\"\$DB_NAME\\" owner \\"\$DB_ROLE\\"', body)
    assert re.search(r'> "\$DB_ENV"\)?$', body, re.M), \
        "the credential file must be written inside the guard, not after it"


def test_role_password_is_synced_even_when_the_role_already_existed():
    """If $DB_ENV is deleted by hand but the `pennyrun` role survives,
    `create role` is skipped (it already exists) -- but a fresh
    PENNYRUN_DB_URL with a *new* random password still gets written
    unless something also pushes that password onto the role. Without
    this, db.migrate fails immediately (loud, set -euo pipefail catches
    it) but the mismatched file is left on disk, and the next restart of
    pennyrun-api.service -- which might be days later -- crash-loops
    with no self-repair. `alter role ... password` must run
    unconditionally in this block, not only inside the `create role`
    fallback, so the role's password always matches what's about to be
    written to $DB_ENV regardless of whether it was just created."""
    body = _db_setup_block(_read())
    alter = re.search(r'^\tsudo -u postgres psql -c \\\n\t\t"alter role \\"\$DB_ROLE\\" password \'\$DB_PASS\'"',
                       body, re.M)
    assert alter, "alter role ... password '$DB_PASS' must run in the guarded block"

    # It must not be nested inside the `create role` fallback (the `||`
    # branch) -- that would only run it when the role was just created,
    # which is exactly the case that already works today.
    create_role_line = re.search(r'^\t\t\| grep -q 1 \|\| sudo -u postgres psql -c \\\n(\t\t"create role.*"[^\n]*)$',
                                  body, re.M)
    assert create_role_line, "expected the create-role fallback line"
    assert "alter role" not in create_role_line.group(1)


def test_migrations_and_seed_run_via_the_venv_python_module_form():
    text = _read()
    assert '"$DIR/.venv/bin/python" -m db.migrate' in text
    assert '"$DIR/.venv/bin/python" -m db.seed' in text
    assert text.index("-m db.migrate") < text.index("-m db.seed"), \
        "migrations must run before seeding"


def test_ingest_token_and_db_credential_are_never_world_or_group_readable():
    """A `> file` followed by a later `chmod 600` leaves the secret
    readable at the process's default umask in between; `(umask 077; ...)`
    around the write closes that window instead of narrowing it after the
    fact."""
    text = _read()
    assert re.search(r"\(umask 077;.*DB_ENV", text, re.S)
    assert re.search(r"\(umask 077;.*INGEST_ENV", text, re.S)
    assert re.search(r"\(umask 077;.*UPSTREAM_INGEST_ENV", text, re.S)
