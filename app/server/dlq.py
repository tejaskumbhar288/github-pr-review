"""Dead-letter inspector.

A dead letter that can only be read by attaching to Redis is a dead letter
nobody reads. This is the other half of the queue's give-up path: what died,
why, how long ago, and one command to put it back once the cause is fixed.

    python -m app.server.dlq                  # list
    python -m app.server.dlq --requeue        # put all of them back
    python -m app.server.dlq --requeue 1      # put back the newest one
"""

from __future__ import annotations

import argparse
import asyncio
import time

from ..config import ConfigError, get_settings
from ..obs.logging import setup_logging
from .queue import build_queue


def _age(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 86400:.0f}d"


def render(records: list[dict], total: int) -> str:
    if not records:
        return "dead-letter queue is empty"

    lines = [
        f"{total} dead letter(s), newest first:",
        "",
        f"{'pr':<34} {'sha':<10} {'age':>6} {'tries':>6}  error",
        "-" * 92,
    ]
    now = time.time()
    for record in records:
        job = record.get("job", {})
        slug = f"{job.get('owner', '?')}/{job.get('repo', '?')}#{job.get('number', '?')}"
        error = (record.get("error") or "").replace("\n", " ")
        lines.append(
            f"{slug[:34]:<34} {str(job.get('head_sha', ''))[:10]:<10} "
            f"{_age(now - record.get('died_at', now)):>6} "
            f"{record.get('attempts', 0):>6}  {error[:40]}"
        )
    if total > len(records):
        lines.append(f"... and {total - len(records)} more")
    return "\n".join(lines)


async def _main(args: argparse.Namespace) -> int:
    settings = get_settings()
    queue = await build_queue(settings)
    try:
        if args.requeue is None:
            total = await queue.dead_depth()
            print(render(await queue.dead_letters(limit=args.limit), total))
            return 0

        drained = await queue.drain_dead_letters(limit=args.requeue)
        # The in-memory queue is per-process, so anything requeued here dies
        # with this command. Say so rather than reporting a hollow success.
        if getattr(queue, "name", "") == "memory":
            print("no Redis: the in-memory queue is empty in a fresh process, nothing to requeue")
            return 1
        print(f"requeued {drained} job(s)")
        return 0
    finally:
        await queue.close()


def main() -> None:  # pragma: no cover - process entrypoint
    parser = argparse.ArgumentParser(
        prog="ai-review-dlq", description="Inspect and drain the dead-letter queue"
    )
    parser.add_argument("--limit", type=int, default=20, help="how many to list (default 20)")
    parser.add_argument(
        "--requeue",
        nargs="?",
        type=int,
        const=0,
        default=None,
        help="requeue N dead letters, newest first; bare --requeue means all",
    )
    args = parser.parse_args()

    setup_logging("WARNING")
    try:
        raise SystemExit(asyncio.run(_main(args)))
    except ConfigError as exc:
        raise SystemExit(f"configuration error: {exc}") from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":  # pragma: no cover
    main()
