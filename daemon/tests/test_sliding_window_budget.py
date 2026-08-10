from pilot.memory.sliding_window import count_message_tokens, fit_messages_to_token_budget


def test_hard_fit_preserves_system_policy_and_latest_goal_under_action_cap():
    messages = [
        {"role": "system", "content": "policy " * 1800},
        {"role": "user", "content": "old goal " * 2000, "type": "goal"},
        {"role": "assistant", "content": "old result " * 2000},
        {"role": "user", "content": "older context " * 2000},
        {
            "role": "user",
            "content": "preferences " * 500 + "CURRENT REQUEST: open Notepad and type the live test marker",
        },
    ]

    fitted = fit_messages_to_token_budget(messages, max_tokens=3000)

    assert count_message_tokens(fitted) <= 3000
    assert fitted[0]["role"] == "system"
    assert fitted[0]["content"] == messages[0]["content"]
    assert fitted[-1]["content"].endswith("CURRENT REQUEST: open Notepad and type the live test marker")
    assert len(fitted) < len(messages)


def test_hard_fit_is_noop_when_messages_already_fit():
    messages = [
        {"role": "system", "content": "policy"},
        {"role": "user", "content": "request"},
    ]

    assert fit_messages_to_token_budget(messages, max_tokens=100) == messages
