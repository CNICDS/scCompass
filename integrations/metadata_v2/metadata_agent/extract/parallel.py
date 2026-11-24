"""Bounded worker lifetimes with results consumed by the caller's thread."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from queue import Empty, Full, Queue
from threading import Event
from typing import Any, Callable, ContextManager, Iterable, Iterator


def parallel_process(
    jobs: Iterable[Any], worker_factory: Callable[[], ContextManager[Callable]],
    workers: int, *, on_cancel: Callable[[], None] | None = None,
) -> Iterator[Any]:
    """Each worker constructs/cleans up its own SQLite and HTTP connections.

    Workers never touch the caller's canonical store. The bounded result queue
    provides back pressure while the caller commits finished samples promptly.
    """
    if workers < 1:
        raise ValueError("workers must be positive")
    inputs: Queue = Queue()
    for job in jobs:
        inputs.put(job)
    if inputs.empty():
        return
    outputs: Queue = Queue(maxsize=2 * workers)
    stop = Event()

    def send(kind, value=None):
        while not stop.is_set():
            try:
                outputs.put((kind, value), timeout=0.1)
                return
            except Full:
                continue

    def worker():
        try:
            with worker_factory() as process:
                while not stop.is_set():
                    try:
                        job = inputs.get_nowait()
                    except Empty:
                        break
                    send("result", process(job))
        except BaseException as exc:
            send("error", exc)
        finally:
            send("done")

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="metadata") as executor:
        for _ in range(workers):
            executor.submit(worker)
        remaining = workers
        try:
            while remaining:
                kind, value = outputs.get()
                if kind == "done":
                    remaining -= 1
                elif kind == "error":
                    raise value
                else:
                    yield value
        finally:
            stop.set()
            if remaining and on_cancel:
                on_cancel()
