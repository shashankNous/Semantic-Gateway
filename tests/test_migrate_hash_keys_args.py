"""Argument handling for the key-hashing migration.

The script drops the plaintext column, so the in-place/rotate choice cannot be
undone by re-running it. These tests pin the property that matters: it never runs
without being told which one the operator meant.
"""

import importlib.util
import pathlib

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "migrate_hash_keys.py"


def load_script():
    """Import the script as a module without executing its __main__ block."""
    spec = importlib.util.spec_from_file_location("migrate_hash_keys", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = load_script()


def test_mode_is_required():
    with pytest.raises(ValueError, match="--mode is required"):
        script.parse_args([])


def test_dry_run_alone_is_still_not_enough():
    """--dry-run must not stand in for a mode."""
    with pytest.raises(ValueError, match="--mode is required"):
        script.parse_args(["--dry-run"])


def test_in_place_mode():
    assert script.parse_args(["--mode=in-place"]) == (script.MODE_IN_PLACE, False)


def test_rotate_mode():
    assert script.parse_args(["--mode=rotate"]) == (script.MODE_ROTATE, False)


@pytest.mark.parametrize(
    "argv",
    [
        ["--mode=in-place", "--dry-run"],
        ["--dry-run", "--mode=in-place"],
    ],
)
def test_dry_run_is_independent_of_mode_and_order(argv):
    assert script.parse_args(argv) == (script.MODE_IN_PLACE, True)


def test_dry_run_composes_with_rotate_too():
    assert script.parse_args(["--mode=rotate", "--dry-run"]) == (script.MODE_ROTATE, True)


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError, match="Unknown mode"):
        script.parse_args(["--mode=inplace"])


def test_the_old_rotate_flag_points_at_the_new_spelling():
    """--rotate used to work; failing loudly beats being silently ignored."""
    with pytest.raises(ValueError, match="--mode=rotate"):
        script.parse_args(["--rotate"])


def test_space_separated_mode_is_rejected():
    with pytest.raises(ValueError, match="with an '='"):
        script.parse_args(["--mode", "rotate"])


def test_unknown_arguments_are_rejected():
    with pytest.raises(ValueError, match="Unknown argument"):
        script.parse_args(["--mode=rotate", "--force"])


def test_usage_text_names_both_modes():
    assert "--mode=in-place" in script.USAGE
    assert "--mode=rotate" in script.USAGE
    assert "--dry-run" in script.USAGE
