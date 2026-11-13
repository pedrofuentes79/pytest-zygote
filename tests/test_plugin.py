"""Zygote's own suite: every case runs a real pytest in a subprocess and checks what it reported.

The plugin forks, so these cannot run in-process — a nested fork inside the suite that is
already under test tells you nothing about either.
"""

import importlib.util
import os

import pytest


@pytest.fixture
def run(pytester):
    def go(*args, workers=2):
        return pytester.runpytest_subprocess(f"--workers={workers}", *args)

    return go


def test_matches_a_serial_run(pytester, run):
    pytester.makepyfile(
        test_mixed="""
        import pytest

        @pytest.mark.parametrize("n", range(12))
        def test_passes(n): assert n >= 0

        def test_fails(): assert False

        @pytest.mark.skip(reason="deliberate")
        def test_skipped(): pass

        @pytest.mark.xfail(reason="deliberate")
        def test_xfails(): assert False
        """
    )
    serial = pytester.runpytest_subprocess()
    forked = run("-rsxX")
    assert forked.parseoutcomes() == serial.parseoutcomes()
    forked.assert_outcomes(passed=12, failed=1, skipped=1, xfailed=1)


def test_failure_keeps_traceback_and_captured_output(pytester, run):
    pytester.makepyfile(
        test_loud="""
        def test_loud():
            print("MARKER-STDOUT")
            raise RuntimeError("MARKER-BOOM")
        """
    )
    result = run(workers=1)
    result.stdout.fnmatch_lines(
        ["*RuntimeError: MARKER-BOOM*", "*Captured stdout call*", "MARKER-STDOUT"]
    )


def test_skip_reason_survives_serialization(pytester, run):
    """JSON has no tuples, and the terminal reporter asserts a skip's longrepr is one."""
    pytester.makepyfile(
        test_skips="""
        import pytest

        @pytest.mark.skip(reason="MARKER-REASON")
        def test_skipped(): pass
        """
    )
    result = run("-rs", workers=1)
    result.stdout.fnmatch_lines(["*MARKER-REASON*"])
    assert "INTERNALERROR" not in result.stdout.str()


def test_dead_worker_is_reported_not_lost(pytester, run):
    pytester.makepyfile(
        test_crash="""
        import os, signal

        def test_before(): assert True

        def test_kills_worker():
            os.kill(os.getpid(), signal.SIGKILL)

        def test_after(): assert True
        """
    )
    result = run(workers=1)
    result.assert_outcomes(passed=2, failed=1)
    result.stdout.fnmatch_lines(["*killed by signal 9*"])


def test_dead_worker_is_replaced_so_the_queue_drains(pytester, run):
    followers = "\n".join(f"def test_after_{i}(): assert True" for i in range(8))
    pytester.makepyfile(
        test_crash="import os, signal\n"
        "def test_kills_worker():\n"
        "    os.kill(os.getpid(), signal.SIGKILL)\n" + followers
    )
    result = run(workers=1)
    result.assert_outcomes(passed=8, failed=1)
    result.stdout.fnmatch_lines(["*replaced dead gw0*"])


def test_restarts_can_be_disabled(pytester, run):
    pytester.makepyfile(
        test_crash="""
        import os, signal

        def test_kills_worker():
            os.kill(os.getpid(), signal.SIGKILL)

        def test_never_reached(): assert True
        """
    )
    result = run("--workers-max-restarts=0", workers=1)
    result.assert_outcomes(failed=1)


def test_a_scoped_fixture_is_torn_down_once_per_worker(pytester, run):
    """One worker means one setup for the module fixture, and a teardown only after the last test."""
    pytester.makepyfile(
        test_scoped="""
        import pytest

        @pytest.fixture(scope="module")
        def resource():
            print("SETUP-MARKER")
            yield
            print("TEARDOWN-MARKER")

        def test_one(resource): assert True
        def test_two(resource): assert True
        def test_three(resource): assert True
        """
    )
    result = run("-s", workers=1)
    assert result.stdout.str().count("SETUP-MARKER") == 1
    assert result.stdout.str().count("TEARDOWN-MARKER") == 1


def _workers_per_file(pytester) -> dict[str, set[str]]:
    seen: dict[str, set[str]] = {}
    for line in (pytester.path / "ran.txt").read_text().split():
        name, worker = line.split(":")
        seen.setdefault(name, set()).add(worker)
    return seen


_RECORD = """
import os, time

def _record(name):
    time.sleep(0.05)
    with open("ran.txt", "a") as fh:
        fh.write(f"{name}:{os.environ['PYTEST_ZYGOTE_WORKER']}\\n")
"""


def test_a_file_stays_on_one_worker(pytester, run):
    """Each worker then touches only its own modules' objects, which keeps more pages shared."""
    for name in ("a", "b"):
        pytester.makepyfile(
            **{
                f"test_{name}": _RECORD
                + "\n".join(f"def test_{i}(): _record({name!r})" for i in range(4))
            }
        )
    run(workers=2).assert_outcomes(passed=8)
    assert all(len(workers) == 1 for workers in _workers_per_file(pytester).values())


def test_an_idle_worker_steals_from_a_long_file(pytester, run):
    pytester.makepyfile(
        test_long=_RECORD + "\n".join(f"def test_{i}(): _record('long')" for i in range(12))
    )
    run(workers=2).assert_outcomes(passed=12)
    assert len(_workers_per_file(pytester)["long"]) == 2


def test_maxfail_stops_the_run(pytester, run):
    pytester.makepyfile(
        test_many="""
        import pytest

        @pytest.mark.parametrize("n", range(40))
        def test_fails(n): assert False
        """
    )
    result = run("--maxfail=2", workers=2)
    assert result.parseoutcomes()["failed"] < 40


def test_zero_workers_leaves_pytest_alone(pytester):
    pytester.makepyfile(test_plain="def test_ok(): assert True")
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(passed=1)
    assert "zygote:" not in result.stdout.str()


def test_auto_means_one_worker_per_core(pytester):
    """The count is also capped at the number of tests, so give it more tests than cores."""
    cores = os.cpu_count() or 1
    pytester.makepyfile(
        test_plenty="\n".join(f"def test_{i}(): assert True" for i in range(cores * 2))
    )
    result = pytester.runpytest_subprocess("--workers", "auto")
    result.assert_outcomes(passed=cores * 2)
    result.stdout.fnmatch_lines([f"zygote: {cores} worker* forked*"])


def test_a_bad_worker_count_is_rejected(pytester):
    pytester.makepyfile(test_plain="def test_ok(): assert True")
    result = pytester.runpytest_subprocess("--workers", "many")
    result.stderr.fnmatch_lines(["*expected a number or 'auto'*"])


def test_the_banner_prints_once(pytester, run):
    """Children inherit the controller's buffered stdout, so it must be flushed before the fork."""
    pytester.makepyfile(test_many="\n".join(f"def test_{i}(): assert True" for i in range(20)))
    result = run(workers=4)
    assert result.stdout.str().count("workers forked from pid") == 1


def test_worker_identity_is_exported(pytester, run):
    pytester.makepyfile(
        test_env="""
        import os

        def test_worker_env():
            assert os.environ["PYTEST_ZYGOTE_WORKER"].startswith("gw")
        """
    )
    run(workers=1).assert_outcomes(passed=1)


@pytest.mark.skipif(importlib.util.find_spec("xdist") is None, reason="needs pytest-xdist to own -n")
def test_rejects_xdist_at_the_same_time(pytester, run):
    pytester.makepyfile(test_plain="def test_ok(): assert True")
    result = run("-n", "2")
    result.stderr.fnmatch_lines(["*alternatives*"])
