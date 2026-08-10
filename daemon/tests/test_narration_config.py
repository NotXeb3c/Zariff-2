from pilot.config import PilotConfig, _merge_config


def test_default_is_disabled():
    config = PilotConfig()
    assert config.narration.enabled is False
    assert config.narration.narrate_steps is True
    assert config.narration.interrupt_on_risk is True
    assert config.narration.proactive_review_enabled is True
    assert config.narration.live_corrections_enabled is True
    assert config.narration.follow_up_enabled is True
    assert config.narration.max_auto_revisions == 2
    assert config.narration.confirm_timeout_seconds == 120.0
    assert config.narration.advisory_timeout_seconds == 5.0


def test_narration_section_merges_scalars():
    config = PilotConfig()
    merged = _merge_config(
        config,
        {
            "narration": {
                "enabled": True,
                "narrate_steps": False,
                "interrupt_on_risk": False,
                "proactive_review_enabled": False,
                "live_corrections_enabled": False,
                "follow_up_enabled": False,
                "max_auto_revisions": 4,
                "confirm_timeout_seconds": 30.0,
                "advisory_timeout_seconds": 4.0,
            }
        },
    )
    assert merged.narration.enabled is True
    assert merged.narration.narrate_steps is False
    assert merged.narration.interrupt_on_risk is False
    assert merged.narration.proactive_review_enabled is False
    assert merged.narration.live_corrections_enabled is False
    assert merged.narration.follow_up_enabled is False
    assert merged.narration.max_auto_revisions == 4
    assert merged.narration.confirm_timeout_seconds == 30.0
    assert merged.narration.advisory_timeout_seconds == 4.0


def test_missing_section_leaves_default():
    config = PilotConfig()
    merged = _merge_config(config, {})
    assert merged.narration.enabled is False
