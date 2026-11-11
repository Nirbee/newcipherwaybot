"""router-agent/agent.sh v8: syntax plus the behaviour of the light per-minute check, run in a real
POSIX shell against stub curl/jq/xkeen — no router, no network. Skips when dash isn't available.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
AGENT = REPO / "router-agent" / "agent.sh"
INSTALLER = REPO / "router-agent" / "install.sh"

pytestmark = pytest.mark.skipif(shutil.which("dash") is None, reason="dash not installed")


def _lf(path: Path) -> str:
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def _syntax_ok(text: str) -> None:
    res = subprocess.run(
        ["dash", "-n"], input=text.encode(), capture_output=True, timeout=20, check=False
    )
    assert res.returncode == 0, res.stderr.decode(errors="replace")


def test_agent_and_installer_parse_under_dash() -> None:
    _syntax_ok(_lf(AGENT))
    _syntax_ok(_lf(INSTALLER))


def test_agent_version_and_new_cron_schedule() -> None:
    match = re.search(r'^AGENT_VERSION="([^"]+)"', _lf(AGENT), re.M)
    assert match and int(match.group(1)) >= 8
    # A fresh install runs the light check every minute.
    assert re.search(r'cron_line="\*/1 ', _lf(INSTALLER))
    # An older router's own */5 line is rewritten in place, in both crontab locations.
    agent = _lf(AGENT)
    assert "ensure_minute_cron" in agent
    assert "/opt/var/spool/cron/crontabs/root /opt/etc/crontabs/root" in agent
    # self-update stays unconditional: ensure_minute_cron comes after it and never replaces it.
    assert agent.index('self_update "$advertised"') < agent.index("ensure_minute_cron || true")


CURL_STUB = r"""#!/bin/sh
# Stub curl: logs the URL, writes canned headers/body, prints the canned status.
url=""; headers=""; body="/dev/null"; prev=""
for a in "$@"; do
    case "$prev" in
        -D) headers="$a" ;;
        -o) body="$a" ;;
    esac
    case "$a" in http*) url="$a" ;; esac
    prev="$a"
done
echo "$url" >> "$FAKE_LOG"
case "$url" in
    */api/agent/rev)
        [ -z "$FAKE_REV_FAILS" ] || exit 7
        printf '%s' "${FAKE_REV_CODE:-304}" ;;
    */api/agent/config)
        code="${FAKE_CONFIG_CODE:-200}"
        if [ -n "$headers" ]; then
            printf 'etag: "newetag"\r\nx-config-rev: %s\r\n\r\n' \
                "${FAKE_NEW_REV:-rev-2}" > "$headers"
        fi
        printf '{"outbounds":[]}' > "$body"
        printf '%s' "$code" ;;
    *)  printf '200' ;;
esac
exit 0
"""


def _write_exec(path: Path, text: str) -> None:
    path.write_bytes(text.encode())
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def sandbox(tmp_path: Path) -> dict[str, object]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    confdir = tmp_path / "conf"
    fake_log = tmp_path / "curl.log"
    _write_exec(bin_dir / "curl", CURL_STUB)
    # jq and the Xray/XKeen side: present and happy, without doing anything.
    _write_exec(bin_dir / "jq", "#!/bin/sh\nexit 0\n")
    _write_exec(bin_dir / "xray", "#!/bin/sh\nexit 0\n")
    _write_exec(bin_dir / "xkeen", "#!/bin/sh\nexit 0\n")
    _write_exec(bin_dir / "pidof", "#!/bin/sh\nexit 0\n")
    _write_exec(bin_dir / "sleep", "#!/bin/sh\nexit 0\n")
    conf = tmp_path / "agent.conf"
    conf.write_bytes(  # bytes: a CRLF on Windows would land inside the values
        f'API_BASE="https://example.invalid"\nTOKEN="t"\nCONFDIR="{confdir.as_posix()}"\n'.encode()
    )
    return {
        "tmp": tmp_path,
        "bin": bin_dir,
        "state": state,
        "conf": conf,
        "lock": tmp_path / "lock.d",
        "fake_log": fake_log,
        "agent_log": tmp_path / "agent.log",
        "confdir": confdir,
    }


def _run(sandbox: dict[str, object], **env: str) -> subprocess.CompletedProcess[bytes]:
    full_env = {
        **os.environ,
        "PATH": str(sandbox["bin"]) + os.pathsep + os.environ.get("PATH", ""),
        # Forward slashes: on Windows these paths land in a POSIX shell.
        "CIPHERWAY_AGENT_CONF": Path(str(sandbox["conf"])).as_posix(),
        "CIPHERWAY_AGENT_STATE_DIR": Path(str(sandbox["state"])).as_posix(),
        "CIPHERWAY_AGENT_LOG": Path(str(sandbox["agent_log"])).as_posix(),
        "CIPHERWAY_AGENT_LOCK": Path(str(sandbox["lock"])).as_posix(),
        "FAKE_LOG": Path(str(sandbox["fake_log"])).as_posix(),
        **env,
    }
    return subprocess.run(
        ["dash", "-"],
        input=_lf(AGENT).encode(),
        env=full_env,
        capture_output=True,
        timeout=60,
        check=False,
    )


def _prime(sandbox: dict[str, object], rev: str = "rev-1") -> None:
    """A run in between full cycles: the last full one was just now."""
    state = sandbox["state"]
    assert isinstance(state, Path)
    state.mkdir(exist_ok=True)
    (state / "last_full").write_text(str(int(time.time())), encoding="utf-8")
    (state / "rev").write_text(rev, encoding="utf-8")


def _urls(sandbox: dict[str, object]) -> list[str]:
    log = sandbox["fake_log"]
    assert isinstance(log, Path)
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_light_check_with_no_change_touches_nothing(sandbox: dict[str, object]) -> None:
    _prime(sandbox)
    res = _run(sandbox, FAKE_REV_CODE="304")
    assert res.returncode == 0, res.stderr
    urls = _urls(sandbox)
    assert urls == ["https://example.invalid/api/agent/rev"]  # one question, no /config fetch
    state = sandbox["state"]
    assert isinstance(state, Path)
    assert (state / "rev").read_text(encoding="utf-8") == "rev-1"
    assert not Path(str(sandbox["lock"])).exists()  # lock released


def test_light_check_with_changed_revision_fetches_and_stores_it(
    sandbox: dict[str, object],
) -> None:
    _prime(sandbox)
    res = _run(sandbox, FAKE_REV_CODE="200", FAKE_NEW_REV="rev-2")
    assert res.returncode == 0, res.stderr
    urls = _urls(sandbox)
    assert urls[0].endswith("/api/agent/rev")
    assert any(u.endswith("/api/agent/config") for u in urls)
    # A light run does not run a heartbeat, self-test or pulse.
    assert not any("/api/agent/heartbeat" in u for u in urls)
    state = sandbox["state"]
    assert isinstance(state, Path)
    assert (state / "rev").read_text(encoding="utf-8") == "rev-2"


def test_light_check_failure_leaves_the_stored_revision_alone(
    sandbox: dict[str, object],
) -> None:
    """A network failure, or /config answering 503 for a new revision, must simply end the run:
    the revision stays unsaved so the next minute retries."""
    _prime(sandbox)
    res = _run(sandbox, FAKE_REV_FAILS="1")
    assert res.returncode == 0, res.stderr
    assert not any(u.endswith("/api/agent/config") for u in _urls(sandbox))

    log_path = sandbox["fake_log"]
    assert isinstance(log_path, Path)
    log_path.write_text("", encoding="utf-8")
    res = _run(sandbox, FAKE_REV_CODE="200", FAKE_CONFIG_CODE="503")
    assert res.returncode == 0, res.stderr
    state = sandbox["state"]
    assert isinstance(state, Path)
    assert (state / "rev").read_text(encoding="utf-8") == "rev-1"
    assert not (state / "stopped_by_agent").exists()


def test_a_busy_lock_means_nothing_runs(sandbox: dict[str, object]) -> None:
    _prime(sandbox)
    lock = sandbox["lock"]
    assert isinstance(lock, Path)
    lock.mkdir()
    res = _run(sandbox, FAKE_REV_CODE="200")
    assert res.returncode == 0, res.stderr
    assert _urls(sandbox) == []  # never touched the network on top of the other run
    assert lock.exists()  # and did not remove another run's lock


def test_watchdog_state_is_left_to_the_full_cycle(sandbox: dict[str, object]) -> None:
    _prime(sandbox)
    state = sandbox["state"]
    assert isinstance(state, Path)
    (state / "stopped_by_agent").write_text("", encoding="utf-8")
    res = _run(sandbox, FAKE_REV_CODE="200")
    assert res.returncode == 0, res.stderr
    assert _urls(sandbox) == []


# --- cron schedule rewrite (functions run on their own, without main) ----------------------


def _run_function(tmp_path: Path, call: str, crontabs: list[Path]) -> None:
    script = _lf(AGENT)
    assert script.rstrip().endswith('main "$@"')
    script = script.rstrip()[: -len('main "$@"')] + call + "\n"
    env = {
        **os.environ,
        "CIPHERWAY_AGENT_CRONTABS": " ".join(p.as_posix() for p in crontabs),
        "CIPHERWAY_AGENT_STATE_DIR": (tmp_path / "state").as_posix(),
        "CIPHERWAY_AGENT_LOG": (tmp_path / "agent.log").as_posix(),
    }
    (tmp_path / "state").mkdir(exist_ok=True)
    res = subprocess.run(
        ["dash", "-"], input=script.encode(), env=env, capture_output=True, timeout=30, check=False
    )
    assert res.returncode == 0, res.stderr.decode(errors="replace")


LINE = "/opt/etc/cipherway-agent/agent.sh >/dev/null 2>&1"


def test_cron_goes_to_every_minute_in_place(tmp_path: Path) -> None:
    crlf = tmp_path / "root-crlf"
    crlf.write_bytes(f"*/5 * * * * {LINE}\r\n0 4 * * * xkeen -ug\r\n".encode())
    twice = tmp_path / "root-twice"
    twice.write_bytes(f"*/5 * * * * {LINE}\n*/5 * * * * {LINE}\n".encode())
    foreign = tmp_path / "root-foreign"
    foreign.write_bytes(b"*/5 * * * * /opt/sbin/other-job\n")
    missing = tmp_path / "no-such-file"

    _run_function(tmp_path, "ensure_minute_cron", [crlf, twice, foreign, missing])

    # (Git Bash's sed drops the CR on Windows; busybox keeps it — the line is what matters.)
    assert crlf.read_bytes().replace(b"\r", b"") == (
        f"*/1 * * * * {LINE}\n0 4 * * * xkeen -ug\n".encode()
    )
    # Rewritten in place — never a line added, so nothing doubles.
    assert twice.read_bytes() == f"*/1 * * * * {LINE}\n*/1 * * * * {LINE}\n".encode()
    assert foreign.read_bytes() == b"*/5 * * * * /opt/sbin/other-job\n"
    assert not missing.exists()

    # Idempotent: a second pass changes nothing.
    _run_function(tmp_path, "ensure_minute_cron", [crlf, twice])
    assert twice.read_bytes() == f"*/1 * * * * {LINE}\n*/1 * * * * {LINE}\n".encode()


def test_going_back_below_v8_restores_the_five_minute_cron(tmp_path: Path) -> None:
    cron = tmp_path / "root"
    cron.write_bytes(f"*/1 * * * * {LINE}\n*/1 * * * * /opt/sbin/other-job\n".encode())
    _run_function(tmp_path, "restore_five_minute_cron", [cron])
    assert cron.read_bytes() == f"*/5 * * * * {LINE}\n*/1 * * * * /opt/sbin/other-job\n".encode()
    # self_update does it only for a version below 8, after its checks and before replacing
    # itself.
    agent = _lf(AGENT)
    body = agent[agent.index("self_update() {") : agent.index("# --- diagnostics")]
    assert "[0-7]) restore_five_minute_cron" in body
    assert body.index('grep -q "^AGENT_VERSION=') < body.index("restore_five_minute_cron")
    assert body.index("restore_five_minute_cron") < body.index('mv "$tmp" "$SELF"')
