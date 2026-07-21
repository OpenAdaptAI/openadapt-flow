"""The runner -- evaluate a :class:`~effectbench.adapter.SystemUnderTest`.

Given a system under test, the runner provisions a fresh synthetic MockMed
system of record per (task x trial), drives the SUT through an
:class:`~effectbench.fixtures.mockmed.MockMedEnv` (goal + params only), and
scores it with an INDEPENDENT oracle the SUT can never reach -- one
:class:`~effectbench.schema.EpisodeRecord` per episode. Every headline
(:func:`~effectbench.metrics.summarize`) is a pure function of those rows.

Fairness / isolation by construction:

- the harness oracle and the SUT's optional product verifier are DISTINCT
  instances built over the same store (different objects / read paths);
- the SUT receives only the task goal (never a step list) + the trial-unique
  params; it cannot reach the harness oracle;
- an UNREADABLE oracle is a harness condition, not an agent outcome -- the
  episode is dropped, never scored as success or SWER.
"""

from __future__ import annotations

from typing import Optional, Sequence

from effectbench.adapter import SystemUnderTest
from effectbench.fixtures.mockmed import MockMedEnv, MockMedSoR
from effectbench.oracle import CompoundSnapshotOracle, score_episode
from effectbench.schema import EpisodeRecord
from effectbench.tasks.mockmed import MOCKMED_TASKS, MockMedTask, trial_params


def _provision(task: MockMedTask, params: dict[str, str]) -> MockMedEnv:
    """Build a fresh MockMed store + arm-facing env for one episode."""
    sor = MockMedSoR()
    sor.reset(seed_concurrent=task.seed_concurrent)
    resolved_extra = [e.resolve(params) for e in task.extra_effects]

    def verifier_factory() -> CompoundSnapshotOracle:
        # The SUT's OWN verifier -- a distinct instance from the harness oracle.
        return CompoundSnapshotOracle(
            sor.read_records, extra=resolved_extra, substrate="mockmed"
        )

    return MockMedEnv(
        goal=task.spec.goal,
        sor=sor,
        fault=task.fault,
        n_posts=task.n_posts,
        params=params,
        _verifier_factory=verifier_factory,
    )


def run_episode(
    sut: SystemUnderTest, task: MockMedTask, trial: int
) -> Optional[EpisodeRecord]:
    """Run and score ONE (sut, task, trial). ``None`` if the episode is dropped."""
    params = trial_params(task.spec.task_id, trial)
    env = _provision(task, params)
    resolved_extra = [e.resolve(params) for e in task.extra_effects]
    # The harness oracle -- a SEPARATE instance over the same store.
    oracle = CompoundSnapshotOracle(
        env.sor.read_records, extra=resolved_extra, substrate="mockmed"
    )

    def run_action():
        return sut.run_task(task.spec, env, params=params)

    try:
        return score_episode(
            episode_id=f"{sut.name}::{task.spec.task_id}::t{trial}",
            task_id=task.spec.task_id,
            arm=sut.name,
            trial=trial,
            substrate=task.spec.substrate,
            category=task.spec.category,
            oracle=oracle,
            expected_effect=task.spec.expected_effect,
            run_action=run_action,
            correct_action_available=task.correct_action_available,
            params=params,
            seed=trial,
            env_fingerprint={
                "env": "mockmed",
                "substrate": task.spec.substrate.value,
                "fault": task.fault,
                "synthetic": True,
            },
        )
    except ValueError:
        # UNREADABLE oracle -> not scoreable. Drop rather than guess.
        return None


def evaluate(
    sut: SystemUnderTest,
    *,
    tasks: Sequence[MockMedTask] = MOCKMED_TASKS,
    trials: int = 10,
) -> list[EpisodeRecord]:
    """Run the whole MockMed anchor for ``sut``, ``trials`` per task.

    Returns every scoreable :class:`~effectbench.schema.EpisodeRecord`. Summarize
    with :func:`effectbench.metrics.summarize` to get SWER + co-metrics.
    """
    records: list[EpisodeRecord] = []
    for task in tasks:
        for trial in range(trials):
            record = run_episode(sut, task, trial)
            if record is not None:
                records.append(record)
    return records
