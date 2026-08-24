"""Database connections — one synchronous, one pooled and asynchronous.

`get_connection()` is unchanged and remains the entry point for everything
that runs one invoice at a time: the evaluation runner, the demo scripts, the
FastAPI read endpoints, the CSV upload, and the synchronous audit-trail
helpers. One connection per statement is cheap when statements are serialised.

The async layer exists for the parallel batch path only. Fifty-two invoices
processed concurrently would otherwise open fifty-two connections per stage;
`AsyncConnectionPool` bounds that to ASYNC_POOL_MAX_SIZE and hands back the
same `dict_row` rows, so no caller can tell a pooled row from a direct one.
"""

import asyncio
import logging
import os
import selectors
import sys
from contextlib import asynccontextmanager

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

load_dotenv()

log = logging.getLogger(__name__)


def get_connection() -> psycopg.Connection:
    """Opens a new Postgres connection using DATABASE_URL, rows returned as dicts."""
    return psycopg.connect(
        os.environ["DATABASE_URL"],
        row_factory=psycopg.rows.dict_row
    )


# ─────────────────────────────────────────────────────────────────────────
# Async connection pool
#
# Sized for the parallel batch: the workers spend nearly all their wall-clock
# time waiting on the LLM, not on Postgres, so each holds a connection for
# milliseconds. Twenty is comfortably above the peak the batch actually
# reaches; fifty-two would be fifty-two idle backends bought for no throughput.
# ─────────────────────────────────────────────────────────────────────────

ASYNC_POOL_MIN_SIZE = 2
ASYNC_POOL_MAX_SIZE = 20

# Seconds to wait for the pool to fill on open, and for a free connection when
# every one of them is checked out.
ASYNC_POOL_OPEN_TIMEOUT = 30.0

# Module-level singleton. `_pool_loop` records which event loop opened it: a
# pooled connection belongs to the loop that created it, so a pool left over
# from a previous asyncio.run() in the same process is unusable rather than
# merely stale.
_pool = None
_pool_loop = None
_pool_lock = None


def _selector_loop_factory():
    """Builds an event loop that psycopg's async connections can run on.

    Windows defaults to ProactorEventLoop, which has no `add_reader()`;
    psycopg needs it to wait on a socket and refuses to connect otherwise.
    Everywhere else the default loop is already compatible.
    """
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop(selectors.SelectSelector())
    return asyncio.new_event_loop()


def loop_supports_async_pg(loop) -> bool:
    """True when `loop` can drive psycopg's async connections.

    Lets a caller that is already inside somebody else's event loop — uvicorn,
    say — find out whether it has to hand the work to a compatible one.
    """
    if sys.platform != "win32":
        return True
    return not isinstance(loop, asyncio.ProactorEventLoop)


def run_async(coro):
    """`asyncio.run()` on a loop the async pool can actually use."""
    with asyncio.Runner(loop_factory=_selector_loop_factory) as runner:
        return runner.run(coro)


async def _get_pool() -> AsyncConnectionPool:
    """Returns the open pool for the running loop, creating it on first use."""
    global _pool, _pool_loop, _pool_lock

    loop = asyncio.get_running_loop()

    # Everything up to the `async with` below is synchronous, so it runs to
    # completion before any other coroutine is scheduled — the check and the
    # lock's creation cannot interleave with a second caller's.
    if _pool is not None and _pool_loop is not loop:
        log.warning(
            "async connection pool was left open on a previous event loop — "
            "discarding it. Call close_pool() before the loop it was opened on ends."
        )
        _pool = None
    if _pool_lock is None or _pool_loop is not loop:
        _pool_lock = asyncio.Lock()
        _pool_loop = loop

    async with _pool_lock:
        if _pool is None:
            pool = AsyncConnectionPool(
                os.environ["DATABASE_URL"],
                min_size=ASYNC_POOL_MIN_SIZE,
                max_size=ASYNC_POOL_MAX_SIZE,
                # dict_row so a pooled row is indistinguishable from one read
                # through get_connection().
                kwargs={"row_factory": dict_row},
                # Opened explicitly below: the constructor's own `open=True`
                # is deprecated in psycopg_pool 3.2+ and cannot be awaited.
                open=False,
            )
            await pool.open(wait=True, timeout=ASYNC_POOL_OPEN_TIMEOUT)
            _pool = pool
    return _pool


@asynccontextmanager
async def get_async_connection():
    """Yields a pooled async connection, with dict rows.

    The async mirror of `with get_connection() as conn:` — on leaving the
    block the transaction is committed and the connection goes back to the
    pool rather than being closed.
    """
    pool = await _get_pool()
    async with pool.connection() as conn:
        yield conn


async def close_pool() -> None:
    """Closes the async pool. A no-op when no pool was ever opened."""
    global _pool, _pool_loop, _pool_lock

    pool, _pool, _pool_loop, _pool_lock = _pool, None, None, None
    if pool is not None:
        await pool.close()
