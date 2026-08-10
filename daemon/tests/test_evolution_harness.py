"""Tests for the fail-closed evolutionary code harness."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pilot.intelligence.evolution_harness import (
    CandidateState,
    DockerEvolutionRunner,
    EvolutionHarness,
    EvolutionState,
)
from pilot.intelligence.experience import ExperienceEventType, ExperienceLedger
from pilot.server import PilotServer


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@heliox.local")
    _git(repo, "config", "user.name", "Heliox Tests")
    (repo / "value.txt").write_text("baseline\n", encoding="utf-8")
    (repo / "note.txt").write_text("original\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline")
    return repo


def _patch(file_name: str, before: str, after: str) -> str:
    return (
        f"diff --git a/{file_name} b/{file_name}\n"
        f"--- a/{file_name}\n"
        f"+++ b/{file_name}\n"
        "@@ -1 +1 @@\n"
        f"-{before}\n"
        f"+{after}\n"
    )


class FakeRunner:
    def status(self):
        return {
            "available": True,
            "backend": "test-container",
            "network": "none",
            "credentials": "not inherited",
        }

    def run(self, worktree, commands):
        value = (worktree / "value.txt").read_text(encoding="utf-8").strip()
        note = (worktree / "note.txt").read_text(encoding="utf-8").strip()
        score = 1.0 if value == "candidate-one" or note == "candidate-two" else 0.5
        return {
            "passed": True,
            "score": score,
            "safety_score": 1.0,
            "commands": [{"label": command.label, "passed": True} for command in commands],
            "total_duration_ms": 10,
        }


class UnavailableRunner:
    def status(self):
        return {
            "available": False,
            "backend": "test-container",
            "reason": "isolation engine stopped",
            "fallback": "disabled",
        }

    def run(self, worktree, commands):
        raise AssertionError("unavailable runner must never execute")


class FakeModel:
    async def generate(self, prompt):
        if "Generate 2 diverse" in prompt:
            return (
                '{"candidates":['
                '{"title":"First","rationale":"Fix value","patch":'
                '"diff --git a/value.txt b/value.txt\\n--- a/value.txt\\n+++ b/value.txt\\n'
                '@@ -1 +1 @@\\n-baseline\\n+candidate-one\\n"},'
                '{"title":"Second","rationale":"Fix note","patch":'
                '"diff --git a/note.txt b/note.txt\\n--- a/note.txt\\n+++ b/note.txt\\n'
                '@@ -1 +1 @@\\n-original\\n+candidate-two\\n"}'
                "]}"
            )
        return '{"quality_score":0.8,"risk_level":"low","rationale":"Bounded change."}'


@pytest.fixture
async def harness(tmp_path):
    repo = _repository(tmp_path)
    ledger = ExperienceLedger(tmp_path / "experience.db")
    await ledger.initialize()
    instance = EvolutionHarness(
        tmp_path / "evolution.db",
        repo,
        tmp_path / "worktrees",
        model_router=FakeModel(),
        runner=FakeRunner(),
    )
    await instance.initialize()
    instance.set_experience_ledger(ledger)
    yield instance, repo, ledger, tmp_path / "worktrees"
    await instance.close()
    await ledger.close()


@pytest.mark.asyncio
async def test_generates_multiple_diverse_inert_candidates(harness):
    instance, repo, ledger, _ = harness
    run = await instance.create_run("Improve the bounded fixture.")
    candidates = await instance.generate_candidates(run.run_id, count=2)

    assert len(candidates) == 2
    assert all(candidate.state == CandidateState.PROPOSED for candidate in candidates)
    assert candidates[0].patch_sha256 != candidates[1].patch_sha256
    assert candidates[1].diversity_score == 1.0
    assert (repo / "value.txt").read_text(encoding="utf-8") == "baseline\n"
    events = await ledger.list_events(event_type=ExperienceEventType.EVOLUTION_CANDIDATE)
    assert len(events) == 2
    assert all("patch" not in event.payload for event in events)


@pytest.mark.asyncio
async def test_isolated_evaluation_compares_baseline_and_cleans_worktrees(harness):
    instance, repo, ledger, worktrees = harness
    run = await instance.create_run("Compare isolated candidates.")
    candidates = await instance.generate_candidates(run.run_id, count=2)

    evaluated = await instance.evaluate(run.run_id)
    archived = await instance.list_candidates(run_id=run.run_id, include_patch=True)

    assert evaluated.state == EvolutionState.EVALUATED
    assert evaluated.baseline_evaluation["score"] == 0.5
    assert all(candidate.state == CandidateState.ELIGIBLE for candidate in archived)
    assert all(candidate.deterministic_evaluation["score"] == 1.0 for candidate in archived)
    assert all(candidate.agent_evaluation["authority"] == "advisory_only" for candidate in archived)
    assert (repo / "value.txt").read_text(encoding="utf-8") == "baseline\n"
    assert not any(path.is_dir() for path in worktrees.rglob("*"))
    assert len(await ledger.list_events(event_type=ExperienceEventType.EVOLUTION_EVALUATION)) == 2
    assert {candidate.candidate_id for candidate in candidates} == {candidate.candidate_id for candidate in archived}


@pytest.mark.asyncio
async def test_promotion_request_never_merges_or_pushes(harness):
    instance, repo, ledger, _ = harness
    base_commit = _git(repo, "rev-parse", "HEAD")
    run = await instance.create_run("Prepare external review.")
    candidates = await instance.generate_candidates(run.run_id, count=2)
    await instance.evaluate(run.run_id)

    with pytest.raises(PermissionError, match="exact candidate id"):
        await instance.request_promotion(
            candidates[0].candidate_id,
            actor="local-admin",
            confirmation="wrong",
        )
    request = await instance.request_promotion(
        candidates[0].candidate_id,
        actor="local-admin",
        confirmation=candidates[0].candidate_id,
    )

    assert request["status"] == "pending_external_review"
    assert "No merge, push, release" in request["message"]
    assert _git(repo, "rev-parse", "HEAD") == base_commit
    assert _git(repo, "status", "--short") == ""
    assert (await instance._require_run(run.run_id)).state == EvolutionState.PROMOTION_REQUESTED
    events = await ledger.list_events(event_type=ExperienceEventType.EVOLUTION_PROMOTION_REQUEST)
    assert len(events) == 1


@pytest.mark.asyncio
async def test_unavailable_isolation_fails_closed_without_state_change(tmp_path):
    repo = _repository(tmp_path)
    instance = EvolutionHarness(
        tmp_path / "evolution.db",
        repo,
        tmp_path / "worktrees",
        runner=UnavailableRunner(),
    )
    await instance.initialize()
    run = await instance.create_run("Must not use host fallback.")
    await instance.add_candidate(
        run.run_id,
        title="one",
        rationale="first",
        patch=_patch("value.txt", "baseline", "candidate-one"),
    )
    await instance.add_candidate(
        run.run_id,
        title="two",
        rationale="second",
        patch=_patch("note.txt", "original", "candidate-two"),
    )

    with pytest.raises(PermissionError, match="no unsafe fallback"):
        await instance.evaluate(run.run_id)

    assert (await instance._require_run(run.run_id)).state == EvolutionState.COLLECTING
    assert not any(path.is_dir() for path in (tmp_path / "worktrees").rglob("*"))
    await instance.close()


@pytest.mark.asyncio
async def test_protected_release_and_security_paths_are_rejected(harness):
    instance, _, _, _ = harness
    run = await instance.create_run("Attempt protected edit.")

    with pytest.raises(PermissionError, match="protected path"):
        await instance.add_candidate(
            run.run_id,
            title="unsafe",
            rationale="must reject",
            patch=_patch("docs/RELEASING.md", "old", "new"),
        )


def test_docker_status_never_advertises_host_fallback(monkeypatch):
    runner = DockerEvolutionRunner()
    monkeypatch.setattr(runner, "_docker_path", lambda: None)

    status = runner.status()

    assert status["available"] is False
    assert status["fallback"] == "disabled"


def test_docker_workdir_cannot_escape_candidate_mount():
    assert DockerEvolutionRunner._container_workdir(".") == "/workspace"
    assert DockerEvolutionRunner._container_workdir("daemon") == "/workspace/daemon"
    with pytest.raises(ValueError, match="inside /workspace"):
        DockerEvolutionRunner._container_workdir("../host")


@pytest.mark.asyncio
async def test_status_publishes_all_hard_restrictions(harness):
    instance, _, _, _ = harness

    status = await instance.status()

    assert set(status["profiles"]) == {"python"}
    assert status["restrictions"] == {
        "direct_installed_app_modification": False,
        "direct_main_push": False,
        "release_credentials": False,
        "live_user_experimentation": False,
        "automatic_promotion": False,
        "unsafe_host_fallback": False,
        "minimum_candidates": 2,
        "maximum_candidates": 8,
    }


@pytest.mark.asyncio
async def test_server_rpc_exposes_archive_and_requires_explicit_promotion():
    fake = SimpleNamespace(
        status=AsyncMock(return_value={"enabled": True}),
        list_runs=AsyncMock(return_value=[SimpleNamespace(to_dict=lambda: {"run_id": "run-1"})]),
        list_candidates=AsyncMock(
            return_value=[
                SimpleNamespace(
                    to_dict=lambda *, include_patch=False: {
                        "candidate_id": "candidate-1",
                        "patch_included": include_patch,
                    }
                )
            ]
        ),
        create_run=AsyncMock(
            return_value=SimpleNamespace(
                state=EvolutionState.COLLECTING,
                to_dict=lambda: {"run_id": "run-1"},
            )
        ),
        generate_candidates=AsyncMock(return_value=[]),
        evaluate=AsyncMock(
            return_value=SimpleNamespace(
                state=EvolutionState.EVALUATED,
                to_dict=lambda: {"run_id": "run-1"},
            )
        ),
        request_promotion=AsyncMock(return_value={"status": "pending_external_review"}),
    )
    server = object.__new__(PilotServer)
    server._evolution_harness = fake

    assert await server._handle_evolution_status({}, None) == {"enabled": True}
    assert (await server._handle_evolution_runs({"limit": 20}, None))["runs"] == [{"run_id": "run-1"}]
    candidates = await server._handle_evolution_candidates(
        {"run_id": "run-1", "include_patch": False},
        None,
    )
    assert candidates["candidates"] == [{"candidate_id": "candidate-1", "patch_included": False}]
    created = await server._handle_evolution_create_run(
        {"problem": "Bounded failure", "profile": "python"},
        None,
    )
    assert created["status"] == EvolutionState.COLLECTING.value
    evaluated = await server._handle_evolution_evaluate({"run_id": "run-1"}, None)
    assert evaluated["status"] == EvolutionState.EVALUATED.value
    promoted = await server._handle_evolution_request_promotion(
        {
            "candidate_id": "candidate-1",
            "actor": "local-admin",
            "confirmation": "candidate-1",
        },
        None,
    )
    assert promoted["status"] == "pending_external_review"
    fake.request_promotion.assert_awaited_once_with(
        "candidate-1",
        actor="local-admin",
        confirmation="candidate-1",
    )
