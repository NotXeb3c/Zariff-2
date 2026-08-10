import pytest

from pilot.security.risk_patterns import RISK_PATTERNS, match_risk_pattern


def test_empty_text_returns_none():
    assert match_risk_pattern("") is None
    assert match_risk_pattern(None) is None  # type: ignore[arg-type]


def test_benign_text_returns_none():
    assert match_risk_pattern("just writing a normal email to a friend about lunch") is None


@pytest.mark.parametrize(
    ("name", "matching_text"),
    [
        ("destructive_shell_command", "about to run rm -rf / on this box"),
        ("destructive_shell_command", "format c: /fs:ntfs"),
        ("destructive_shell_command", "dd if=/dev/zero of=/dev/sda"),
        ("credential_exposure", "password: hunter2"),
        ("credential_exposure", "api_key=sk-abc123"),
        ("destructive_sql", "DROP TABLE users"),
        ("destructive_sql", "DELETE FROM accounts"),
        ("unauthorized_admin_command", "net user hacker /add"),
        ("unauthorized_admin_command", "icacls C:\\ /grant everyone:F"),
    ],
)
def test_known_patterns_match(name, matching_text):
    assert match_risk_pattern(matching_text) == name


@pytest.mark.parametrize(
    "non_matching_text",
    [
        "removing a temp file with rm notes.txt",
        "delete from my todo list",
        "the format of this document is markdown",
        "my password manager is great",
    ],
)
def test_similar_but_non_matching_text_returns_none(non_matching_text):
    assert match_risk_pattern(non_matching_text) is None


def test_every_pattern_name_is_reachable():
    """Every named pattern has at least one string in the test table above
    that matches it -- guards against a rule silently becoming unreachable."""
    exercised_names = {
        "destructive_shell_command",
        "credential_exposure",
        "destructive_sql",
        "unauthorized_admin_command",
    }
    assert exercised_names == set(RISK_PATTERNS.keys())


@pytest.mark.parametrize(
    "garbage",
    [
        "a" * 10000,
        "\x00\x01\x02 binary-ish garbage \xff",
        "".join(chr(i) for i in range(32, 127)),
        "🎉" * 500,
    ],
)
def test_never_raises_on_arbitrary_input(garbage):
    match_risk_pattern(garbage)  # must not raise
