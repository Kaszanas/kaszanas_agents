"""Process-pool map that degrades to a plain loop for small inputs (spawn costs ~100 ms on Windows)."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from typing import TypeVar

TaskType = TypeVar("TaskType")
ResultType = TypeVar("ResultType")

MINIMUM_TASKS_FOR_PARALLELISM = 64
MINIMUM_TASKS_PER_WORKER = 16
# Every worker unpickles the whole project index; past ~8 workers start-up cost outweighs the gain
# (measured on Django, 2,943 files: 8 workers 2.3 s, 16 workers 2.6 s, 32 workers 3.6 s).
DEFAULT_MAXIMUM_WORKERS = 8


def default_worker_count(task_count: int) -> int:
    available_cpus = os.cpu_count() or 1
    return max(1, min(available_cpus, DEFAULT_MAXIMUM_WORKERS, task_count // MINIMUM_TASKS_PER_WORKER))


def map_possibly_in_parallel(
    function: Callable[[TaskType], ResultType],
    tasks: Sequence[TaskType],
    jobs: int | None,
    initializer: Callable[..., None] | None = None,
    initializer_arguments: tuple[object, ...] = (),
) -> list[ResultType]:
    tasks = list(tasks)
    worker_count = jobs if jobs is not None else default_worker_count(task_count=len(tasks))
    run_serially = worker_count <= 1 or (jobs is None and len(tasks) < MINIMUM_TASKS_FOR_PARALLELISM)
    if run_serially:
        if initializer is not None:
            initializer(*initializer_arguments)
        return [function(task) for task in tasks]
    chunk_size = max(1, len(tasks) // (worker_count * 4))
    with ProcessPoolExecutor(
        max_workers=worker_count, initializer=initializer, initargs=initializer_arguments
    ) as executor:
        return list(executor.map(function, tasks, chunksize=chunk_size))
