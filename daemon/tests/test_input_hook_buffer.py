import dataclasses
import time
import types

from pilot.system.input_hook import InputActivitySnapshot, InputSupervisionHook


def _key(char: str | None = None, name: str = "") -> types.SimpleNamespace:
    return types.SimpleNamespace(char=char, name=name)


def test_snapshot_with_no_activity_returns_zero_rates():
    hook = InputSupervisionHook()
    snap = hook.snapshot()
    assert snap.keystroke_rate_per_min == 0.0
    assert snap.click_rate_per_min == 0.0
    assert snap.matched_pattern is None
    assert snap.hook_healthy is False  # never started


def test_snapshot_clears_the_character_buffer():
    hook = InputSupervisionHook()
    for c in "hello world":
        hook._on_key_press(_key(char=c))
    assert len(hook._char_buffer) == len("hello world")

    hook.snapshot()

    assert len(hook._char_buffer) == 0
    assert hook._keystroke_count == 0


def test_snapshot_never_leaks_raw_text_in_returned_object():
    hook = InputSupervisionHook()
    secret = "password: hunter2"
    for c in secret:
        hook._on_key_press(_key(char=c))

    snap = hook.snapshot()

    assert snap.matched_pattern == "credential_exposure"
    for value in dataclasses.asdict(snap).values():
        assert secret not in str(value)


def test_matched_pattern_surfaces_only_the_rule_name():
    hook = InputSupervisionHook()
    for c in "rm -rf /":
        hook._on_key_press(_key(char=c))

    snap = hook.snapshot()

    assert snap.matched_pattern == "destructive_shell_command"


def test_special_keys_become_word_separators():
    hook = InputSupervisionHook()
    for c in "rm -rf":
        hook._on_key_press(_key(char=c))
    hook._on_key_press(_key(char=None, name="space"))
    for c in "/":
        hook._on_key_press(_key(char=c))

    snap = hook.snapshot()

    assert snap.matched_pattern == "destructive_shell_command"


def test_unrecognized_special_keys_are_dropped():
    hook = InputSupervisionHook()
    hook._on_key_press(_key(char=None, name="up"))
    hook._on_key_press(_key(char=None, name="shift"))
    assert len(hook._char_buffer) == 0


def test_click_counter_ignores_coordinates_entirely():
    hook = InputSupervisionHook()
    hook._on_click(100, 200, "left", True)
    hook._on_click(999, 888, "right", True)
    hook._on_click(50, 60, "left", False)  # release, not counted

    snap = hook.snapshot()

    assert snap.click_rate_per_min > 0
    # No coordinate ever gets stored anywhere on the hook object.
    assert not hasattr(hook, "_last_click_position")


def test_rate_scales_with_elapsed_time():
    hook = InputSupervisionHook()
    hook._last_snapshot_time = time.monotonic() - 30.0  # pretend 30s elapsed
    for c in "abcdefghij":  # 10 keystrokes over 30s -> 20/min
        hook._on_key_press(_key(char=c))

    snap = hook.snapshot()

    assert 15.0 < snap.keystroke_rate_per_min < 25.0


def test_is_running_false_before_start():
    hook = InputSupervisionHook()
    assert hook.is_running() is False


def test_stop_is_safe_when_never_started():
    hook = InputSupervisionHook()
    hook.stop()  # must not raise
    assert hook.is_running() is False


def test_start_without_pynput_installed_sets_import_failed(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _blocking_import(name, *args, **kwargs):
        if name == "pynput":
            raise ImportError("no pynput in this test env")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocking_import)

    hook = InputSupervisionHook()
    hook.start()

    assert hook._import_failed is True
    assert hook.is_running() is False


def test_buffer_respects_max_chars():
    hook = InputSupervisionHook(keystroke_buffer_max_chars=5)
    for c in "abcdefghij":
        hook._on_key_press(_key(char=c))
    assert len(hook._char_buffer) == 5
    assert "".join(hook._char_buffer) == "fghij"


def test_input_activity_snapshot_is_a_plain_dataclass():
    snap = InputActivitySnapshot(
        keystroke_rate_per_min=1.0, click_rate_per_min=2.0, matched_pattern=None, hook_healthy=True
    )
    assert dataclasses.is_dataclass(snap)
