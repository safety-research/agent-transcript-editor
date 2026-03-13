"""
Monitor Router - Trusted monitor integration for transcript evaluation.

Uses the trusted-monitor package to score transcripts for suspiciousness (0-100).
"""

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

import tool_imports  # noqa: F401, E402 — sets up sys.path for sibling tools
from fastapi import APIRouter, HTTPException  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from rate_limit import acquire, record  # noqa: E402
from sessions import session_manager  # noqa: E402

# Dedicated thread pool for monitor eval API calls so they don't starve
# the default executor (used by aiofiles, file loading, etc.)
_eval_executor = ThreadPoolExecutor(max_workers=128, thread_name_prefix="monitor-eval")


def _monitor_api_key(api_key_id: str = "default") -> str | None:
    """Return the Anthropic API key from the backend env, or None to use SDK default."""
    if api_key_id == "alt":
        key = os.getenv("ANTHROPIC_API_KEY_ALT")
        if not key:
            raise HTTPException(status_code=400, detail="ANTHROPIC_API_KEY_ALT not configured in backend .env")
        return key
    return os.getenv("ANTHROPIC_API_KEY") or None


router = APIRouter()


@router.get("/prompts")
async def get_prompts():
    """Return available suspiciousness prompt variants from trusted-monitor."""
    try:
        from trusted_monitor.prompts import list_prompts
        return {"prompts": list_prompts()}
    except ImportError:
        raise HTTPException(status_code=503, detail="trusted-monitor not installed")


async def _throttled_to_thread(fn, *args, api_key_id: str = "default", on_start=None, estimated_input_tokens: int = 0):
    """Run a sync function in a thread, throttled by per-key TPM rate limiting.

    Uses a dedicated thread pool so eval API calls don't exhaust the default
    executor (which aiofiles and file loading depend on).

    api_key_id: which API key this call uses (for per-key rate limiting).
    on_start: optional callback fired after acquiring the rate-limit slot,
    right before the function runs. Used to transition eval status from
    "queued" → "running" only when actual API work begins.
    estimated_input_tokens: pre-record this many tokens on send so the rate
    limiter accounts for in-flight requests (not just completed ones).
    """
    async with acquire(api_key_id):
        if estimated_input_tokens > 0:
            record(api_key_id, estimated_input_tokens)
        if on_start is not None:
            on_start()
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(_eval_executor, partial(fn, *args))
        return result


# Keep references to running asyncio tasks so they aren't GC'd
_running_tasks: set[asyncio.Task] = set()


def _create_tracked_task(coro) -> asyncio.Task:
    """Create an asyncio task and track it to prevent GC."""
    task = asyncio.create_task(coro)
    _running_tasks.add(task)
    task.add_done_callback(_running_tasks.discard)
    return task


# Cache for preprocessed transcripts (hash -> jsonl string)
# Avoids redundant preprocessing when multiple metrics evaluate the same transcript
_preprocess_cache: dict[str, str] = {}
_PREPROCESS_CACHE_MAX = 64

# Async locks to deduplicate concurrent preprocessing of the same transcript.
# When multiple metrics for the same transcript call _preprocess_tools_only_async
# concurrently, only one actually preprocesses; the rest wait and read the cache.
_preprocess_locks: dict[str, asyncio.Lock] = {}
_preprocess_locks_lock = asyncio.Lock()


def _messages_to_jsonl(messages: list[dict[str, Any]]) -> str:
    """Convert messages list to JSONL string (one JSON object per line)."""
    return "\n".join(json.dumps(msg, ensure_ascii=False) for msg in messages)


def _do_preprocess(jsonl: str) -> str:
    """Run the blocking preprocessing in a thread. Called via asyncio.to_thread()."""
    from trusted_monitor.transcript import preprocess_transcript

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
        f.write(jsonl)
        tmp_path = Path(f.name)

    try:
        return preprocess_transcript(tmp_path, tools_only=True)
    finally:
        tmp_path.unlink()


async def _preprocess_tools_only_async(messages: list[dict[str, Any]]) -> str:
    """Apply tools-only preprocessing without blocking the event loop.

    Uses asyncio.to_thread() for the actual preprocessing work and
    per-key locks to deduplicate concurrent requests for the same transcript.
    """
    jsonl = _messages_to_jsonl(messages)
    cache_key = hashlib.sha256(jsonl.encode()).hexdigest()

    if cache_key in _preprocess_cache:
        return _preprocess_cache[cache_key]

    # Get or create a lock for this cache key to deduplicate concurrent preprocessing
    async with _preprocess_locks_lock:
        if cache_key not in _preprocess_locks:
            _preprocess_locks[cache_key] = asyncio.Lock()
        lock = _preprocess_locks[cache_key]

    async with lock:
        # Double-check cache after acquiring lock (another task may have populated it)
        if cache_key in _preprocess_cache:
            return _preprocess_cache[cache_key]

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(_eval_executor, partial(_do_preprocess, jsonl))

        # Evict oldest entries if cache is full
        if len(_preprocess_cache) >= _PREPROCESS_CACHE_MAX:
            oldest_key = next(iter(_preprocess_cache))
            del _preprocess_cache[oldest_key]

        _preprocess_cache[cache_key] = result

        # Clean up the lock entry (optional, prevents unbounded growth)
        async with _preprocess_locks_lock:
            _preprocess_locks.pop(cache_key, None)

        return result


def _run_single_eval(transcript_jsonl: str, prompt_variant: str, api_key_id: str = "default") -> dict[str, Any]:
    """Run a single monitor evaluation (synchronous, for use in thread)."""
    try:
        from trusted_monitor.monitor import create_monitor
        from trusted_monitor.prompts import SUSPICIOUSNESS_PROMPTS

        if prompt_variant not in SUSPICIOUSNESS_PROMPTS:
            raise ValueError(
                f"Unknown prompt variant: {prompt_variant!r}. Available: {list(SUSPICIOUSNESS_PROMPTS.keys())}"
            )

        system_prompt = SUSPICIOUSNESS_PROMPTS[prompt_variant]
        monitor = create_monitor(
            provider="anthropic",
            system_prompt=system_prompt,
            thinking_budget=10000,
            api_key=_monitor_api_key(api_key_id),
        )
        result = monitor.evaluate(transcript_jsonl)

        out: dict[str, Any] = {
            "score": result.score,
            "reasoning": result.reasoning,
        }
        if result.input_tokens:
            out["input_tokens"] = result.input_tokens
        if result.thinking:
            out["thinking"] = result.thinking
        return out
    except Exception as e:
        return {
            "score": -1,
            "reasoning": f"Error: {e}",
            "error": str(e),
        }


def _run_single_egregiousness(scenario: str, outcome: str, api_key_id: str = "default") -> dict[str, Any]:
    """Run a single egregiousness evaluation (synchronous, for use in thread)."""
    try:
        from trusted_monitor.monitor import create_monitor

        monitor = create_monitor(provider="anthropic", thinking_budget=10000, api_key=_monitor_api_key(api_key_id))
        result = monitor.evaluate_egregiousness(scenario=scenario, outcome=outcome)

        out: dict[str, Any] = {
            "score": result.score_with_reasoning,
            "scoreNumberOnly": result.score_number_only,
            "reasoning": result.reasoning,
            "input_tokens": result.input_tokens or (len(scenario + outcome) // 4 + 2000),
        }
        if hasattr(result, "thinking") and result.thinking:
            out["thinking"] = result.thinking
        return out
    except Exception as e:
        return {
            "score": -1,
            "scoreNumberOnly": -1,
            "reasoning": f"Error: {e}",
            "error": str(e),
        }


def _run_single_incriminating(
    transcript_jsonl: str, outcome: str, mechanism: str, api_key_id: str = "default"
) -> dict[str, Any]:
    """Run a single incriminating evaluation (synchronous, for use in thread)."""
    try:
        from trusted_monitor.monitor import create_monitor

        monitor = create_monitor(provider="anthropic", thinking_budget=10000, api_key=_monitor_api_key(api_key_id))
        result = monitor.evaluate_incriminating(transcript_jsonl, outcome, mechanism)

        out: dict[str, Any] = {
            "score": result.score,
            "reasoning": result.reasoning,
            "input_tokens": result.input_tokens or (len(transcript_jsonl) // 4 + 2000),
        }
        if hasattr(result, "thinking") and result.thinking:
            out["thinking"] = result.thinking
        return out
    except Exception as e:
        return {
            "score": -1,
            "reasoning": f"Error: {e}",
            "error": str(e),
        }


def _run_single_confidence(
    transcript_jsonl: str, outcome: str, mechanism: str, api_key_id: str = "default"
) -> dict[str, Any]:
    """Run a single confidence evaluation (synchronous, for use in thread)."""
    try:
        from trusted_monitor.monitor import create_monitor

        monitor = create_monitor(provider="anthropic", thinking_budget=10000, api_key=_monitor_api_key(api_key_id))
        result = monitor.evaluate_confidence(transcript_jsonl, outcome, mechanism)

        out: dict[str, Any] = {
            "score": result.score,
            "reasoning": result.reasoning,
            "input_tokens": result.input_tokens or (len(transcript_jsonl) // 4 + 2000),
        }
        if hasattr(result, "thinking") and result.thinking:
            out["thinking"] = result.thinking
        return out
    except Exception as e:
        return {
            "score": -1,
            "reasoning": f"Error: {e}",
            "error": str(e),
        }


def _run_single_effectiveness(
    transcript_jsonl: str, outcome: str, mechanism: str, api_key_id: str = "default"
) -> dict[str, Any]:
    """Run a single effectiveness evaluation (synchronous, for use in thread)."""
    try:
        from trusted_monitor.monitor import create_monitor

        monitor = create_monitor(provider="anthropic", thinking_budget=10000, api_key=_monitor_api_key(api_key_id))
        result = monitor.evaluate_effectiveness(transcript_jsonl, outcome, mechanism)

        out: dict[str, Any] = {
            "score": result.score,
            "reasoning": result.reasoning,
            "input_tokens": result.input_tokens or (len(transcript_jsonl) // 4 + 2000),
        }
        if hasattr(result, "thinking") and result.thinking:
            out["thinking"] = result.thinking
        return out
    except Exception as e:
        return {
            "score": -1,
            "reasoning": f"Error: {e}",
            "error": str(e),
        }


def _run_single_realism(transcript_jsonl: str, api_key_id: str = "default") -> dict[str, Any]:
    """Run a single realism evaluation (synchronous, for use in thread)."""
    try:
        from trusted_monitor.monitor import create_monitor

        monitor = create_monitor(provider="anthropic", thinking_budget=10000, api_key=_monitor_api_key(api_key_id))
        result = monitor.evaluate_realism(transcript_jsonl)

        out: dict[str, Any] = {
            "score": result.score,
            "reasoning": result.reasoning,
        }
        if result.input_tokens:
            out["input_tokens"] = result.input_tokens
        else:
            out["input_tokens"] = len(transcript_jsonl) // 4 + 2000
        if result.thinking:
            out["thinking"] = result.thinking
        return out
    except Exception as e:
        return {
            "score": -1,
            "reasoning": f"Error: {e}",
            "error": str(e),
        }


class TokenCountRequest(BaseModel):
    """Request to count tokens in tools-only transcript."""

    messages: list[dict[str, Any]]


@router.post("/token-count")
async def monitor_token_count(request: TokenCountRequest):
    """Count tokens in the tools-only preprocessed transcript.

    Applies the same tools-only filtering used for evaluation,
    then counts tokens via the Anthropic tokenizer.
    """
    import anthropic

    transcript_jsonl = await _preprocess_tools_only_async(request.messages)

    try:
        client = anthropic.Anthropic(api_key=_monitor_api_key())
        from sessions import DEFAULT_MODEL

        result = client.messages.count_tokens(
            model=DEFAULT_MODEL,
            messages=[{"role": "user", "content": transcript_jsonl}],
        )
        return {"token_count": result.input_tokens}
    except Exception:
        rough_count = len(transcript_jsonl) // 4
        return {"token_count": rough_count, "estimated": True}


@router.get("/status")
async def monitor_status():
    """Check if the trusted monitor is available."""
    try:
        from trusted_monitor.monitor import create_monitor  # noqa: F401

        return {"available": True}
    except ImportError as e:
        return {"available": False, "error": str(e)}


# ── Full File Evaluation ─────────────────────────────────────────────────
#
# Single endpoint that runs all applicable metrics for a transcript file,
# tracks per-metric progress, and auto-writes scores to meta.json on completion.


class EvaluateFileRequest(BaseModel):
    """Request to evaluate all metrics for a transcript file."""

    project: str
    file_name: str
    transcript: list[dict[str, Any]]
    outcome: str | None = None
    scenario: str | None = None
    mechanism: str | None = None  # pre-stringified mechanism
    n_evals: int = 1
    n_evals_other: int = 1  # evaluations per run for non-suspiciousness metrics
    prompt_variants: list[str] = ["control_arena"]
    eval_egregiousness: bool = True
    eval_incriminating: bool = True
    eval_effectiveness: bool = True
    eval_confidence: bool = True
    eval_realism: bool = True
    api_key_id: str = "default"
    transcript_hash: str | None = None
    metrics_override: list[str] | None = None  # when set, only run these metrics
    force: bool = False  # skip cache and always re-evaluate


# Separate store for file-level evaluations.
# Bounded to prevent unbounded memory growth from long-running servers.
_file_eval_store: dict[str, dict[str, Any]] = {}
_FILE_EVAL_STORE_MAX = 256

# Track active (in-flight) evaluations per file to deduplicate concurrent requests.
# Maps file_key → (transcript_hash, eval_id).
# Same hash → reuse eval_id; different hash → cancel old + start new.
_active_evals: dict[str, tuple[str, str]] = {}
_active_evals_lock = asyncio.Lock()


def _evict_old_evals() -> None:
    """Evict oldest completed evals when store exceeds max size."""
    if len(_file_eval_store) <= _FILE_EVAL_STORE_MAX:
        return
    # Remove oldest completed entries first
    completed = [k for k, v in _file_eval_store.items() if v["status"] in ("done", "error")]
    to_remove = len(_file_eval_store) - _FILE_EVAL_STORE_MAX
    for key in completed[:to_remove]:
        del _file_eval_store[key]


def _read_existing_variant_data(project: str, file_name: str, transcript_hash: str | None) -> dict[str, dict]:
    """Read existing suspiciousness variant data from meta.json for merging during partial reruns.

    Only returns data if the stored transcript_hash matches the current one,
    to avoid merging stale evals from a different version of the transcript.
    """
    from routers.files import get_transcripts_dir, read_sidecar

    transcripts_dir = get_transcripts_dir()
    fname = file_name if file_name.endswith(".jsonl") else file_name + ".jsonl"
    file_path = transcripts_dir / project / fname
    meta = read_sidecar(file_path) or {}

    stored_hash = meta.get("transcript_hash")
    if transcript_hash is not None and stored_hash != transcript_hash:
        return {}  # Transcript changed — don't merge stale variant data

    return meta.get("evals", {}).get("suspiciousness", {}).get("variants", {})


def _write_scores_to_meta(project: str, file_name: str, scores: dict, evals: dict, transcript_hash: str | None):
    """Write evaluation scores to the .meta.json sidecar file."""
    from routers.files import get_transcripts_dir, write_sidecar

    transcripts_dir = get_transcripts_dir()
    fname = file_name if file_name.endswith(".jsonl") else file_name + ".jsonl"
    file_path = transcripts_dir / project / fname

    data: dict[str, Any] = {"scores": scores, "evals": evals}
    if transcript_hash is not None:
        data["transcript_hash"] = transcript_hash

    write_sidecar(file_path, data)


async def _run_file_evaluation(eval_id: str, request: EvaluateFileRequest):
    """Run all applicable metrics for a file, writing results to meta.json on completion."""
    store = _file_eval_store[eval_id]
    file_key = f"{request.project}/{request.file_name}"
    cancel_event: asyncio.Event = store["cancel_event"]

    try:
        # Check cancellation before starting work
        if cancel_event.is_set():
            store["status"] = "cancelled"
            store["error_message"] = "Superseded by newer evaluation"
            store["done_event"].set()
            return

        # Preprocess transcript (async, non-blocking)
        transcript_jsonl = await _preprocess_tools_only_async(request.transcript)

        # Determine which metrics to run
        metric_coros: dict[str, Any] = {}
        override = set(request.metrics_override) if request.metrics_override else None

        # Suspiciousness: one per prompt variant
        if override is None or "suspiciousness" in override:
            for variant in request.prompt_variants:
                key = f"suspiciousness:{variant}"
                store["metrics"][key] = {"status": "queued"}
                metric_coros[key] = _run_single_metric("suspiciousness", transcript_jsonl, request, variant=variant)

        if (
            (override is None or "egregiousness" in override)
            and request.eval_egregiousness
            and request.outcome
            and request.scenario
        ):
            store["metrics"]["egregiousness"] = {"status": "queued"}
            metric_coros["egregiousness"] = _run_single_metric("egregiousness", transcript_jsonl, request)

        if (
            (override is None or "incriminating" in override)
            and request.eval_incriminating
            and request.outcome
            and request.mechanism
        ):
            store["metrics"]["incriminating"] = {"status": "queued"}
            metric_coros["incriminating"] = _run_single_metric("incriminating", transcript_jsonl, request)

        if (
            (override is None or "effectiveness" in override)
            and request.eval_effectiveness
            and request.outcome
            and request.mechanism
        ):
            store["metrics"]["effectiveness"] = {"status": "queued"}
            metric_coros["effectiveness"] = _run_single_metric("effectiveness", transcript_jsonl, request)

        if (
            (override is None or "confidence" in override)
            and request.eval_confidence
            and request.outcome
            and request.mechanism
        ):
            store["metrics"]["confidence"] = {"status": "queued"}
            metric_coros["confidence"] = _run_single_metric("confidence", transcript_jsonl, request)

        if (override is None or "realism" in override) and request.eval_realism:
            store["metrics"]["realism"] = {"status": "queued"}
            metric_coros["realism"] = _run_single_metric("realism", transcript_jsonl, request)

        store["status"] = "running"

        # Run all metrics concurrently, updating store as each completes
        async def _run_and_store(key: str, coro):
            try:
                if cancel_event.is_set():
                    store["metrics"][key] = {"status": "cancelled"}
                    return
                store["metrics"][key]["status"] = "running"
                result = await coro
                if cancel_event.is_set():
                    store["metrics"][key] = {"status": "cancelled"}
                    return
                store["metrics"][key] = {"status": "done", **result}
            except Exception as e:
                store["metrics"][key] = {"status": "error", "error": str(e)}

            # Broadcast progress to SSE subscribers
            session = session_manager.get_session(file_key)
            if session:
                session.monitor_eval_id = eval_id
                session.monitor_eval_status = "running"
                await session_manager.broadcast(
                    session,
                    {
                        "type": "monitor_eval_progress",
                        "eval_id": eval_id,
                        "status": "running",
                        "metrics": store["metrics"],
                        "scores": store.get("scores", {}),
                    },
                )

        tasks = [_run_and_store(key, coro) for key, coro in metric_coros.items()]
        await asyncio.gather(*tasks, return_exceptions=True)

        # Bail if cancelled while metrics were running
        if cancel_event.is_set():
            store["status"] = "cancelled"
            store["error_message"] = "Superseded by newer evaluation"
            store["done_event"].set()
            async with _active_evals_lock:
                if _active_evals.get(file_key, (None, None))[1] == eval_id:
                    _active_evals.pop(file_key, None)
            return

        # Aggregate results and write to meta.json
        scores: dict[str, float] = {}
        evals: dict[str, Any] = {}
        error_metrics: list[str] = []

        # Process suspiciousness variants
        variant_data: dict[str, dict] = {}
        for key, metric in store["metrics"].items():
            if key.startswith("suspiciousness:") and metric["status"] == "done":
                variant_name = key.split(":", 1)[1]
                variant_data[variant_name] = metric

        if variant_data:
            # If this is a partial rerun, merge with existing variant data from meta.json
            if override and "suspiciousness" in override:
                existing_meta = _read_existing_variant_data(request.project, request.file_name, request.transcript_hash)
                # Existing variants that weren't rerun this time
                for vname, vdata in existing_meta.items():
                    if vname not in variant_data:
                        variant_data[vname] = {
                            "mean_score": vdata["score"],
                            "reasoning": vdata.get("reasoning"),
                            "results": vdata.get("results", []),
                        }

            # Max score across variants
            max_variant = max(variant_data.items(), key=lambda kv: kv[1].get("mean_score", -1))
            max_score = max_variant[1]["mean_score"]
            scores["suspiciousness"] = max_score
            for vname, vdata in variant_data.items():
                scores[f"suspiciousness_{vname}"] = vdata["mean_score"]
            evals["suspiciousness"] = {
                "score": max_score,
                "reasoning": max_variant[1].get("reasoning"),
                "results": max_variant[1].get("results", []),
                "variants": {
                    vname: {
                        "score": vdata["mean_score"],
                        "reasoning": vdata.get("reasoning"),
                        "results": vdata.get("results", []),
                    }
                    for vname, vdata in variant_data.items()
                },
            }
        else:
            # Check if suspiciousness errored
            susp_errors = [
                k
                for k in store["metrics"]
                if k.startswith("suspiciousness:") and store["metrics"][k]["status"] == "error"
            ]
            if susp_errors:
                error_metrics.append("suspiciousness")

        # Process other metrics
        for metric_name in ("egregiousness", "incriminating", "effectiveness", "confidence", "realism"):
            m = store["metrics"].get(metric_name)
            if not m:
                continue
            if m["status"] == "done" and m.get("mean_score") is not None and m["mean_score"] != -1:
                scores[metric_name] = m["mean_score"]
                eval_data: dict[str, Any] = {
                    "score": m["mean_score"],
                    "reasoning": m.get("reasoning"),
                    "results": m.get("results", []),
                }
                if metric_name == "egregiousness" and m.get("meanScoreNumberOnly") is not None:
                    eval_data["scoreNumberOnly"] = m["meanScoreNumberOnly"]
                evals[metric_name] = eval_data
            elif m["status"] == "error":
                error_metrics.append(metric_name)

        # Write to meta.json
        if scores:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                _eval_executor,
                partial(
                    _write_scores_to_meta,
                    request.project,
                    request.file_name,
                    scores,
                    evals,
                    request.transcript_hash,
                ),
            )

        store["scores"] = scores
        store["evals"] = evals

        if error_metrics:
            store["status"] = "done"  # partial success
            store["error_message"] = "Failed metrics: " + ", ".join(error_metrics)
        else:
            store["status"] = "done"

        # Signal waiters that evaluation is complete
        store["done_event"].set()

        # Clean up active eval tracking
        async with _active_evals_lock:
            if _active_evals.get(file_key, (None, None))[1] == eval_id:
                _active_evals.pop(file_key, None)

        # Sync scores/evals into the session's sidecar_metadata so SSE
        # session_state (and future loads from cache) include the results.
        session = session_manager.get_session(file_key)
        if session:
            session.sidecar_metadata["scores"] = scores
            session.sidecar_metadata["evals"] = evals
            if request.transcript_hash:
                session.sidecar_metadata["transcript_hash"] = request.transcript_hash
            session.monitor_eval_id = eval_id
            session.monitor_eval_status = store["status"]
            session.monitor_eval_scores = scores
            session.monitor_eval_error = store.get("error_message")
            await session_manager.broadcast(
                session,
                {
                    "type": "monitor_eval_progress",
                    "eval_id": eval_id,
                    "status": store["status"],
                    "metrics": store["metrics"],
                    "scores": store.get("scores", {}),
                    "evals": store.get("evals", {}),
                    "error_message": store.get("error_message"),
                },
            )

    except Exception as e:
        store["status"] = "error"
        store["error_message"] = str(e)
        store["done_event"].set()  # Signal waiters on error too

        # Clean up active eval tracking
        async with _active_evals_lock:
            if _active_evals.get(file_key, (None, None))[1] == eval_id:
                _active_evals.pop(file_key, None)

        # Broadcast error to SSE subscribers
        session = session_manager.get_session(file_key)
        if session:
            session.monitor_eval_status = "error"
            session.monitor_eval_error = str(e)
            await session_manager.broadcast(
                session,
                {
                    "type": "monitor_eval_progress",
                    "eval_id": eval_id,
                    "status": "error",
                    "metrics": store.get("metrics", {}),
                    "scores": {},
                    "error_message": str(e),
                },
            )


async def _run_single_metric(
    metric_type: str,
    transcript_jsonl: str,
    request: EvaluateFileRequest,
    variant: str | None = None,
) -> dict[str, Any]:
    """Run a single metric's evaluations, returning aggregated results."""
    api_key_id = request.api_key_id
    n_evals = request.n_evals if metric_type == "suspiciousness" else request.n_evals_other

    # Estimate input tokens for rate limiting (recorded on send, not receive)
    if metric_type == "egregiousness":
        est_tokens = len((request.scenario or "") + (request.outcome or "")) // 4 + 2000
    else:
        est_tokens = len(transcript_jsonl) // 4 + 2000

    if metric_type == "suspiciousness":
        first = await _throttled_to_thread(
            _run_single_eval,
            transcript_jsonl,
            variant,
            api_key_id,
            api_key_id=api_key_id,
            estimated_input_tokens=est_tokens,
        )
        if n_evals > 1:
            rest = await asyncio.gather(
                *[
                    _throttled_to_thread(
                        _run_single_eval,
                        transcript_jsonl,
                        variant,
                        api_key_id,
                        api_key_id=api_key_id,
                        estimated_input_tokens=est_tokens,
                    )
                    for _ in range(n_evals - 1)
                ]
            )
            results = [first] + list(rest)
        else:
            results = [first]

    elif metric_type == "egregiousness":
        results = list(
            await asyncio.gather(
                *[
                    _throttled_to_thread(
                        _run_single_egregiousness,
                        request.scenario,
                        request.outcome,
                        api_key_id,
                        api_key_id=api_key_id,
                        estimated_input_tokens=est_tokens,
                    )
                    for _ in range(n_evals)
                ]
            )
        )

    elif metric_type == "incriminating":
        first = await _throttled_to_thread(
            _run_single_incriminating,
            transcript_jsonl,
            request.outcome,
            request.mechanism,
            api_key_id,
            api_key_id=api_key_id,
            estimated_input_tokens=est_tokens,
        )
        if n_evals > 1:
            rest = await asyncio.gather(
                *[
                    _throttled_to_thread(
                        _run_single_incriminating,
                        transcript_jsonl,
                        request.outcome,
                        request.mechanism,
                        api_key_id,
                        api_key_id=api_key_id,
                        estimated_input_tokens=est_tokens,
                    )
                    for _ in range(n_evals - 1)
                ]
            )
            results = [first] + list(rest)
        else:
            results = [first]

    elif metric_type == "effectiveness":
        first = await _throttled_to_thread(
            _run_single_effectiveness,
            transcript_jsonl,
            request.outcome,
            request.mechanism,
            api_key_id,
            api_key_id=api_key_id,
            estimated_input_tokens=est_tokens,
        )
        if n_evals > 1:
            rest = await asyncio.gather(
                *[
                    _throttled_to_thread(
                        _run_single_effectiveness,
                        transcript_jsonl,
                        request.outcome,
                        request.mechanism,
                        api_key_id,
                        api_key_id=api_key_id,
                        estimated_input_tokens=est_tokens,
                    )
                    for _ in range(n_evals - 1)
                ]
            )
            results = [first] + list(rest)
        else:
            results = [first]

    elif metric_type == "confidence":
        first = await _throttled_to_thread(
            _run_single_confidence,
            transcript_jsonl,
            request.outcome,
            request.mechanism,
            api_key_id,
            api_key_id=api_key_id,
            estimated_input_tokens=est_tokens,
        )
        if n_evals > 1:
            rest = await asyncio.gather(
                *[
                    _throttled_to_thread(
                        _run_single_confidence,
                        transcript_jsonl,
                        request.outcome,
                        request.mechanism,
                        api_key_id,
                        api_key_id=api_key_id,
                        estimated_input_tokens=est_tokens,
                    )
                    for _ in range(n_evals - 1)
                ]
            )
            results = [first] + list(rest)
        else:
            results = [first]

    elif metric_type == "realism":
        first = await _throttled_to_thread(
            _run_single_realism,
            transcript_jsonl,
            api_key_id,
            api_key_id=api_key_id,
            estimated_input_tokens=est_tokens,
        )
        if n_evals > 1:
            rest = await asyncio.gather(
                *[
                    _throttled_to_thread(
                        _run_single_realism,
                        transcript_jsonl,
                        api_key_id,
                        api_key_id=api_key_id,
                        estimated_input_tokens=est_tokens,
                    )
                    for _ in range(n_evals - 1)
                ]
            )
            results = [first] + list(rest)
        else:
            results = [first]

    else:
        raise ValueError(f"Unknown metric type: {metric_type}")

    # Aggregate
    valid = [r for r in results if r.get("score", -1) >= 0]
    if not valid:
        raise RuntimeError(f"All {metric_type} evaluations failed: {results[0].get('error', 'unknown')}")

    out: dict[str, Any] = {
        "mean_score": round(sum(r["score"] for r in valid) / len(valid), 1),
        "reasoning": valid[0]["reasoning"],
        "results": results,
    }

    if metric_type == "egregiousness":
        valid_num = [r for r in valid if r.get("scoreNumberOnly", -1) >= 0]
        if valid_num:
            out["meanScoreNumberOnly"] = round(sum(r["scoreNumberOnly"] for r in valid_num) / len(valid_num), 1)

    return out


def _check_cached_scores(request: EvaluateFileRequest) -> dict[str, Any] | None:
    """Check if meta.json already has fresh scores for this transcript.

    Returns the cached result dict if transcript_hash matches and all
    requested metrics have scores, otherwise None.
    """
    if not request.transcript_hash:
        logger.info("Cache miss: no transcript_hash in request")
        return None

    from routers.files import get_transcripts_dir, read_sidecar

    transcripts_dir = get_transcripts_dir()
    fname = request.file_name if request.file_name.endswith(".jsonl") else request.file_name + ".jsonl"
    file_path = transcripts_dir / request.project / fname

    meta = read_sidecar(file_path)
    if not meta:
        logger.info("Cache miss: no meta.json for %s/%s", request.project, request.file_name)
        return None

    if meta.get("transcript_hash") != request.transcript_hash:
        logger.info(
            "Cache miss: hash mismatch for %s/%s — meta=%s request=%s",
            request.project,
            request.file_name,
            meta.get("transcript_hash"),
            request.transcript_hash,
        )
        return None

    scores = meta.get("scores", {})
    evals = meta.get("evals", {})
    if not scores or "suspiciousness" not in scores:
        logger.info("Cache miss: no suspiciousness score in meta for %s/%s", request.project, request.file_name)
        return None

    # Check all requested prompt variants have been evaluated
    for variant in request.prompt_variants:
        if f"suspiciousness_{variant}" not in scores:
            logger.info("Cache miss: missing variant %s for %s/%s", variant, request.project, request.file_name)
            return None

    # Check all requested metrics are present
    if request.eval_egregiousness and request.outcome and request.scenario and "egregiousness" not in scores:
        logger.info("Cache miss: missing egregiousness for %s/%s", request.project, request.file_name)
        return None
    if request.eval_incriminating and request.outcome and request.mechanism and "incriminating" not in scores:
        logger.info("Cache miss: missing incriminating for %s/%s", request.project, request.file_name)
        return None
    if request.eval_effectiveness and request.outcome and request.mechanism and "effectiveness" not in scores:
        logger.info("Cache miss: missing effectiveness for %s/%s", request.project, request.file_name)
        return None
    if request.eval_confidence and request.outcome and request.mechanism and "confidence" not in scores:
        logger.info("Cache miss: missing confidence for %s/%s", request.project, request.file_name)
        return None
    if request.eval_realism and "realism" not in scores:
        logger.info("Cache miss: missing realism for %s/%s", request.project, request.file_name)
        return None

    logger.info("Cache HIT for %s/%s", request.project, request.file_name)
    return {"scores": scores, "evals": evals}


def _reconstruct_metrics_from_cache(cached: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct the metrics dict from cached evals so the frontend polling logic works."""
    metrics: dict[str, Any] = {}
    cached_evals = cached.get("evals", {})

    susp = cached_evals.get("suspiciousness")
    if susp and "variants" in susp:
        for vname, vdata in susp["variants"].items():
            metrics[f"suspiciousness:{vname}"] = {
                "status": "done",
                "mean_score": vdata["score"],
                "reasoning": vdata.get("reasoning"),
                "results": vdata.get("results", []),
            }
    elif susp:
        metrics["suspiciousness:control_arena"] = {
            "status": "done",
            "mean_score": susp["score"],
            "reasoning": susp.get("reasoning"),
            "results": susp.get("results", []),
        }

    for metric_name in ("egregiousness", "incriminating", "effectiveness", "confidence", "realism"):
        m = cached_evals.get(metric_name)
        if not m:
            continue
        # Normalize old snake_case results to camelCase
        raw_results = m.get("results", [])
        if metric_name == "egregiousness":
            normalized_results = [
                {
                    **{k: v for k, v in r.items() if k != "score_number_only"},
                    "scoreNumberOnly": r.get("scoreNumberOnly", r.get("score_number_only")),
                }
                for r in raw_results
            ]
        else:
            normalized_results = raw_results
        entry: dict[str, Any] = {
            "status": "done",
            "mean_score": m["score"],
            "reasoning": m.get("reasoning"),
            "results": normalized_results,
        }
        if metric_name == "egregiousness" and "scoreNumberOnly" in m:
            entry["meanScoreNumberOnly"] = m["scoreNumberOnly"]
        metrics[metric_name] = entry

    return metrics


@router.post("/evaluate-file")
async def evaluate_file(request: EvaluateFileRequest):
    """
    Start a full multi-metric evaluation for a transcript file.

    Deduplicates concurrent requests per file using (file_key, transcript_hash):
    - Same hash as in-flight eval → return existing eval_id (coalesce)
    - Different hash → cancel in-flight eval, start new one
    - No in-flight eval → start new one

    Uses cached scores from meta.json when available (unless force=True
    or metrics_override is set). The UI Re-evaluate button passes force=True;
    the agent's get_monitor_score uses the default (force=False) to hit cache.
    """
    # Use cache unless force or partial-metric rerun
    if not request.force and not request.metrics_override:
        cached = _check_cached_scores(request)
        if cached:
            eval_id = str(uuid.uuid4())
            done_event = asyncio.Event()
            done_event.set()  # Already done (cached)
            _file_eval_store[eval_id] = {
                "status": "done",
                "metrics": _reconstruct_metrics_from_cache(cached),
                "scores": cached["scores"],
                "evals": cached["evals"],
                "error_message": None,
                "cached": True,
                "done_event": done_event,
            }
            return {"eval_id": eval_id}

    file_key = f"{request.project}/{request.file_name}"
    transcript_hash = request.transcript_hash

    async with _active_evals_lock:
        # Check for in-flight evaluation on this file
        if file_key in _active_evals:
            active_hash, active_eval_id = _active_evals[file_key]
            active_store = _file_eval_store.get(active_eval_id)

            if active_store and active_store["status"] in ("queued", "running"):
                if transcript_hash and active_hash == transcript_hash and not request.force:
                    # Same content — coalesce into the existing evaluation
                    logger.info(
                        "Dedup: reusing in-flight eval %s for %s (same hash %s)",
                        active_eval_id,
                        file_key,
                        transcript_hash,
                    )
                    return {"eval_id": active_eval_id}
                else:
                    # Different content or force — cancel the stale evaluation
                    logger.info(
                        "Dedup: cancelling stale eval %s for %s (hash %s → %s)",
                        active_eval_id,
                        file_key,
                        active_hash,
                        transcript_hash,
                    )
                    active_store["cancel_event"].set()

        # Start a new evaluation
        eval_id = str(uuid.uuid4())
        _evict_old_evals()
        _file_eval_store[eval_id] = {
            "status": "queued",
            "metrics": {},
            "scores": {},
            "evals": {},
            "error_message": None,
            "done_event": asyncio.Event(),
            "cancel_event": asyncio.Event(),
        }
        _active_evals[file_key] = (transcript_hash or "", eval_id)

    _create_tracked_task(_run_file_evaluation(eval_id, request))

    return {"eval_id": eval_id}


@router.get("/evaluate-file/{eval_id}")
async def get_file_eval_status(eval_id: str):
    """
    Get status of a file evaluation with per-metric breakdown.

    Poll until status is 'done' or 'error'.
    """
    if eval_id not in _file_eval_store:
        raise HTTPException(status_code=404, detail="File evaluation not found")

    store = _file_eval_store[eval_id]
    return {
        "status": store["status"],
        "metrics": store["metrics"],
        "scores": store.get("scores", {}),
        "evals": store.get("evals", {}),
        "error_message": store.get("error_message"),
    }
