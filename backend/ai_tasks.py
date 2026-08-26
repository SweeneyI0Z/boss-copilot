"""页面级 AI 任务调度器。

调度器只负责进程内排队、并发、取消、限流退避和状态广播，不依赖具体的
LLM 或数据库实现。调用方通过 ``task_runner(task, cancelled)`` 注入实际工作，
其中 ``cancelled`` 必须在流式读取和落库前被检查。
"""
from __future__ import annotations

import copy
import heapq
import json
import math
import re
import statistics
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Generator, Optional


VALID_PAGES = ("jobs", "workbench")
ACTIVE_STATUSES = frozenset(("queued", "running", "retrying"))
TERMINAL_STATUSES = frozenset(("succeeded", "failed", "cancelled"))
MAX_PAGE_CONCURRENCY = 5
DEFAULT_TASK_SECONDS = {
    "score": 45.0,
    "analysis": 55.0,
    "greeting": 30.0,
}
DEFAULT_UNKNOWN_TASK_SECONDS = 45.0
MIN_DURATION_SECONDS = 0.25
MAX_DURATION_SECONDS = 3600.0
DEFAULT_DURATION_HISTORY_SIZE = 20
CANCEL_RELEASE_PRIOR_SECONDS = 2.0
MAX_RETRY_WAIT_CHUNK_SECONDS = 3600.0

TaskRunner = Callable[[dict, Callable[[], bool]], Any]
SleepFunc = Callable[[float], None]
DelayFunc = Callable[[int], float]


def _active_key(page: str, job_key: str, resume_id: Optional[int],
                kind: str, payload: Optional[dict]) -> tuple:
    """活动任务按简历修订去重，升版后的新任务不能复用旧修订任务。"""
    revision = (payload or {}).get("resume_revision")
    try:
        revision = int(revision) if revision is not None else None
    except (TypeError, ValueError):
        revision = str(revision)
    return page, job_key, resume_id, kind, revision


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _safe_copy(value):
    try:
        return copy.deepcopy(value)
    except Exception:
        return value


def _ceil_seconds(value: float) -> int:
    """向上取整 ETA，同时消除浮点加法产生的极小越界。"""
    value = max(0.0, float(value))
    return int(math.ceil(value - 1e-9)) if value > 0 else 0


def _status_code(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_RATE_LIMIT_TEXT = re.compile(
    r"(?<!\d)429(?!\d)|rate[\s_-]*limit|too many requests|"
    r"请求(?:过于)?频繁|请求过多|限流",
    re.IGNORECASE,
)


def is_rate_limit_error(error: BaseException) -> bool:
    """识别 OpenAI 及兼容客户端通过属性或文本表达的 429。"""
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop(0)
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)

        for name in ("status_code", "status", "http_status", "code"):
            value = getattr(current, name, None)
            if _status_code(value) == 429:
                return True
            if isinstance(value, str) and _RATE_LIMIT_TEXT.search(value):
                return True

        response = getattr(current, "response", None)
        if response is not None:
            if isinstance(response, dict):
                values = (response.get("status_code"), response.get("status"),
                          response.get("code"))
            else:
                values = (getattr(response, "status_code", None),
                          getattr(response, "status", None),
                          getattr(response, "code", None))
            if any(_status_code(value) == 429 for value in values):
                return True

        if _RATE_LIMIT_TEXT.search(str(current)):
            return True
        for nested in (getattr(current, "__cause__", None),
                       getattr(current, "__context__", None)):
            if isinstance(nested, BaseException):
                pending.append(nested)
    return False


def _retry_after_seconds(error: BaseException) -> Optional[float]:
    """读取兼容客户端保留的 Retry-After；未提供时由指数退避接管。"""
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        raw = getattr(current, "retry_after", None)
        if raw is None:
            response = getattr(current, "response", None)
            headers = (response.get("headers", {}) if isinstance(response, dict)
                       else getattr(response, "headers", {})) or {}
            if hasattr(headers, "get"):
                raw = headers.get("retry-after") or headers.get("Retry-After")
        try:
            if raw is not None:
                value = float(raw)
                if math.isfinite(value):
                    return max(0.0, value)
        except (TypeError, ValueError):
            pass
        for nested in (getattr(current, "__cause__", None),
                       getattr(current, "__context__", None)):
            if isinstance(nested, BaseException):
                pending.append(nested)
    return None


@dataclass
class _Task:
    id: str
    page: str
    kind: str
    job_key: str
    resume_id: Optional[int]
    payload: dict
    status: str = "queued"
    attempts: int = 0
    rate_limit_retries: int = 0
    result: Any = None
    error: str = ""
    progress: Optional[float] = None
    progress_attempt: int = field(default=0, repr=False)
    message: str = ""
    partial_output: str = ""
    cancel_requested: bool = False
    cancel_requested_monotonic: Optional[float] = field(default=None, repr=False)
    cancel_estimate_seconds: Optional[float] = field(default=None, repr=False)
    created_at: str = field(default_factory=_now_iso)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    updated_at: str = field(default_factory=_now_iso)
    created_monotonic: float = field(default_factory=time.monotonic, repr=False)
    started_monotonic: Optional[float] = field(default=None, repr=False)
    attempt_started_monotonic: Optional[float] = field(default=None, repr=False)
    retry_ready_monotonic: Optional[float] = field(default=None, repr=False)
    finished_monotonic: Optional[float] = field(default=None, repr=False)
    cancel_event: threading.Event = field(
        default_factory=threading.Event, repr=False, compare=False)

    def active_key(self) -> tuple:
        return _active_key(
            self.page, self.job_key, self.resume_id, self.kind, self.payload)


@dataclass
class _PageState:
    page: str
    max_concurrency: int
    queue: deque = field(default_factory=deque)
    running: set = field(default_factory=set)
    executing: set = field(default_factory=set)
    effective_concurrency: int = 1
    consecutive_successes: int = 0
    version: int = 0
    duration_samples: dict[str, deque] = field(default_factory=dict)

    def __post_init__(self):
        self.effective_concurrency = self.max_concurrency


class AITaskScheduler:
    """为岗位列表和收藏工作台提供互相隔离的进程内任务队列。"""

    def __init__(
            self,
            task_runner: TaskRunner,
            *,
            max_concurrency: int = MAX_PAGE_CONCURRENCY,
            max_retries: int = 4,
            backoff_base: float = 1.0,
            backoff_cap: float = 30.0,
            recovery_successes: int = 3,
            sleep_func: Optional[SleepFunc] = None,
            delay_func: Optional[DelayFunc] = None,
            default_task_seconds: Optional[dict[str, float]] = None,
            duration_history_size: int = DEFAULT_DURATION_HISTORY_SIZE,
            clock_func: Optional[Callable[[], float]] = None):
        if not callable(task_runner):
            raise TypeError("task_runner 必须可调用")
        self._task_runner = task_runner
        self._max_concurrency = min(
            MAX_PAGE_CONCURRENCY, max(1, int(max_concurrency)))
        self._max_retries = max(0, int(max_retries))
        self._backoff_base = max(0.0, float(backoff_base))
        self._backoff_cap = max(self._backoff_base, float(backoff_cap))
        self._recovery_successes = max(1, int(recovery_successes))
        self._sleep_func = sleep_func
        self._delay_func = delay_func
        self._clock_func = clock_func or time.monotonic
        self._duration_history_size = max(1, int(duration_history_size))
        self._default_task_seconds = dict(DEFAULT_TASK_SECONDS)
        for kind, seconds in (default_task_seconds or {}).items():
            value = float(seconds)
            if not math.isfinite(value) or value <= 0:
                raise ValueError("任务默认耗时必须大于 0")
            self._default_task_seconds[str(kind)] = min(
                MAX_DURATION_SECONDS, value)

        self._condition = threading.Condition(threading.RLock())
        self._tasks: dict[str, _Task] = {}
        self._task_order: list[str] = []
        self._active_index: dict[tuple, str] = {}
        self._pages = {
            page: _PageState(page, self._max_concurrency) for page in VALID_PAGES
        }
        self._workers: set[threading.Thread] = set()
        self._closed = False
        self._version = 0
        self._dispatchers = []
        for page in VALID_PAGES:
            thread = threading.Thread(
                target=self._dispatch_loop,
                args=(page,),
                name=f"ai-tasks-{page}",
                daemon=True,
            )
            self._dispatchers.append(thread)
            thread.start()

    @staticmethod
    def _validate_page(page: str) -> str:
        page = str(page or "").strip()
        if page not in VALID_PAGES:
            raise ValueError(f"page 必须是 {'/'.join(VALID_PAGES)}")
        return page

    def _touch_locked(self, page: str) -> None:
        self._version += 1
        self._pages[page].version += 1
        self._condition.notify_all()

    def _default_duration(self, kind: str) -> float:
        return self._default_task_seconds.get(
            kind, self._default_task_seconds.get(
                "*", DEFAULT_UNKNOWN_TASK_SECONDS))

    def _duration_estimate_locked(self, state: _PageState,
                                  kind: str) -> tuple[float, int]:
        """用近期成功耗时校准默认值，少量样本时保留先验以减少跳变。"""
        samples = list(state.duration_samples.get(kind, ()))
        if not samples:
            return self._default_duration(kind), 0
        observed = statistics.median(samples)
        confidence = min(1.0, len(samples) / 3.0)
        estimate = (self._default_duration(kind) * (1.0 - confidence) +
                    observed * confidence)
        estimate = max(
            MIN_DURATION_SECONDS, min(MAX_DURATION_SECONDS, estimate))
        return estimate, len(samples)

    def _record_success_duration_locked(self, state: _PageState,
                                        task: _Task, now: float) -> None:
        if task.started_monotonic is None:
            return
        duration = max(
            MIN_DURATION_SECONDS,
            min(MAX_DURATION_SECONDS, now - task.started_monotonic),
        )
        samples = state.duration_samples.get(task.kind)
        if samples is None:
            samples = deque(maxlen=self._duration_history_size)
            state.duration_samples[task.kind] = samples
        samples.append(duration)

    def _task_remaining_seconds_locked(self, state: _PageState,
                                       task: _Task, now: float) -> float:
        if task.cancel_requested:
            started = task.cancel_requested_monotonic
            elapsed = max(0.0, now - (started if started is not None else now))
            prior = task.cancel_estimate_seconds or CANCEL_RELEASE_PRIOR_SECONDS
            predicted_total = max(prior, elapsed * 1.2)
            return max(MIN_DURATION_SECONDS, predicted_total - elapsed)
        estimate, _ = self._duration_estimate_locked(state, task.kind)
        if task.status == "queued":
            return estimate
        if task.status == "retrying":
            retry_wait = max(
                0.0, (task.retry_ready_monotonic or now) - now)
            return retry_wait + estimate
        if task.status != "running":
            return 0.0

        started = task.attempt_started_monotonic
        if started is None:
            started = task.started_monotonic
        elapsed = max(0.0, now - (started if started is not None else now))
        progress = (task.progress
                    if task.progress_attempt == task.attempts else None)
        if progress is not None and 0.0 < progress < 1.0:
            inferred_total = elapsed / max(0.05, progress)
            predicted_total = max(estimate, inferred_total)
            return max(
                MIN_DURATION_SECONDS, predicted_total * (1.0 - progress))
        if progress is not None and progress >= 1.0:
            return MIN_DURATION_SECONDS

        # 无 token 级进度时先线性倒计时；超出历史均值后按已耗时的 20%
        # 继续外推，避免任务尚未结束但 ETA 长时间卡在 0。
        predicted_total = max(estimate, elapsed * 1.2)
        return max(MIN_DURATION_SECONDS, predicted_total - elapsed)

    @staticmethod
    def _eta_confidence(sample_count: int) -> str:
        if sample_count <= 0:
            return "low"
        if sample_count < 3:
            return "medium"
        return "high"

    def _page_eta_locked(self, page: str, active_tasks: list[_Task],
                         now: float) -> tuple[dict, dict[str, int], dict[str, str]]:
        """模拟当前有效并发下的完成时刻；rate 的单位为任务/秒。"""
        state = self._pages[page]
        remaining = len(active_tasks)
        if not active_tasks:
            return {
                "remaining": 0,
                "eta_seconds": 0,
                "eta_confidence": "none",
                "rate": 0.0,
                "rate_per_minute": 0.0,
                "average_seconds_per_task": 0.0,
                "estimate_samples": sum(
                    len(samples) for samples in state.duration_samples.values()),
            }, {}, {}

        concurrency = max(1, state.effective_concurrency)
        active_by_id = {task.id: task for task in active_tasks}
        executing = [task for task in active_tasks
                     if task.id in state.executing]
        retry_waiting = [task for task in active_tasks
                         if task.status == "retrying" and
                         task.id not in state.executing]
        queued_ids = {task.id for task in active_tasks if task.status == "queued"}
        queued = []
        for task_id in state.queue:
            if task_id in queued_ids:
                queued.append(self._tasks[task_id])
                queued_ids.remove(task_id)
        # 极短的派发窗口里任务可能还未进入 deque，按创建顺序兜底补齐。
        queued.extend(task for task in active_tasks if task.id in queued_ids)

        # ETA 同时模拟两个约束：effective_concurrency 是实际 LLM 请求槽，
        # max_concurrency 是线程上限。retrying 在退避时仍占线程，但不占请求槽，
        # 因而线程有空位时，普通排队任务可以先运行。
        completion_times = {}
        completion_events = []
        retry_events = []
        ready_retries = deque()
        queued = deque(queued)
        sequence = 0
        executing_count = len(executing)
        worker_count = len(state.running.intersection(active_by_id))

        def add_completion(task: _Task, completed_at: float,
                           holds_request: bool) -> None:
            nonlocal sequence
            completion_times[task.id] = completed_at
            heapq.heappush(
                completion_events,
                (completed_at, sequence, task.id, holds_request),
            )
            sequence += 1

        for task in executing:
            add_completion(
                task,
                self._task_remaining_seconds_locked(state, task, now),
                True,
            )
        for task in retry_waiting:
            if task.cancel_requested:
                add_completion(
                    task,
                    self._task_remaining_seconds_locked(state, task, now),
                    False,
                )
                continue
            ready_at = max(
                0.0, (task.retry_ready_monotonic or now) - now)
            retry_events.append((ready_at, sequence, task.id))
            sequence += 1
        retry_events = deque(sorted(retry_events))

        def start_available(current_time: float) -> None:
            nonlocal executing_count, worker_count
            while executing_count < concurrency:
                if ready_retries:
                    task = active_by_id[ready_retries.popleft()]
                    duration = self._duration_estimate_locked(
                        state, task.kind)[0]
                    executing_count += 1
                    add_completion(task, current_time + duration, True)
                    continue
                if queued and worker_count < state.max_concurrency:
                    task = queued.popleft()
                    duration = self._task_remaining_seconds_locked(
                        state, task, now)
                    worker_count += 1
                    executing_count += 1
                    add_completion(task, current_time + duration, True)
                    continue
                break

        current_time = 0.0
        while retry_events and retry_events[0][0] <= current_time:
            ready_retries.append(retry_events.popleft()[2])
        start_available(current_time)
        while len(completion_times) < len(active_tasks):
            next_completion = (completion_events[0][0]
                               if completion_events else math.inf)
            next_retry = retry_events[0][0] if retry_events else math.inf
            next_time = min(next_completion, next_retry)
            if not math.isfinite(next_time):
                # 状态瞬时不一致时采用串行兜底，保证快照仍可用且不返回 0。
                cursor = max(completion_times.values(), default=current_time)
                pending = ([active_by_id[task_id]
                            for task_id in ready_retries] + list(queued))
                for task in pending:
                    cursor += self._duration_estimate_locked(
                        state, task.kind)[0]
                    completion_times[task.id] = cursor
                break
            current_time = next_time
            while (completion_events and
                   completion_events[0][0] <= current_time + 1e-9):
                _, _, _, holds_request = heapq.heappop(completion_events)
                worker_count = max(0, worker_count - 1)
                if holds_request:
                    executing_count = max(0, executing_count - 1)
            while retry_events and retry_events[0][0] <= current_time + 1e-9:
                ready_retries.append(retry_events.popleft()[2])
            start_available(current_time)

        eta = max(completion_times.values(), default=0.0)
        estimated_durations = [
            self._duration_estimate_locked(state, task.kind)[0]
            for task in active_tasks if not task.cancel_requested
        ]
        average = (sum(estimated_durations) / len(estimated_durations)
                   if estimated_durations else 0.0)
        active_capacity = min(concurrency, len(estimated_durations))
        rate = (active_capacity * 60.0 / average
                if average > 0 and active_capacity else 0.0)
        active_kinds = {task.kind for task in active_tasks}
        sample_count = sum(
            len(state.duration_samples.get(kind, ())) for kind in active_kinds)
        task_confidence = {
            task.id: self._eta_confidence(
                len(state.duration_samples.get(task.kind, ())))
            for task in active_tasks
        }
        task_eta = {
            task_id: _ceil_seconds(seconds)
            for task_id, seconds in completion_times.items()
        }
        return {
            "remaining": remaining,
            "eta_seconds": _ceil_seconds(eta),
            "eta_confidence": min(
                task_confidence.values(),
                key=("low", "medium", "high").index,
            ) if task_confidence else "none",
            "rate": round(rate / 60.0, 4),
            "rate_per_minute": round(rate, 2),
            "average_seconds_per_task": round(average, 1),
            "estimate_samples": sample_count,
        }, task_eta, task_confidence

    def _task_dict_locked(self, task: _Task, *, eta_seconds=None,
                          eta_confidence=None) -> dict:
        return {
            "id": task.id,
            "page": task.page,
            "kind": task.kind,
            "job_key": task.job_key,
            "resume_id": task.resume_id,
            "payload": _safe_copy(task.payload),
            "status": task.status,
            "attempts": task.attempts,
            "rate_limit_retries": task.rate_limit_retries,
            "result": _safe_copy(task.result),
            "error": task.error,
            "progress": task.progress,
            "message": task.message,
            "partial_output": task.partial_output,
            "cancel_requested": task.cancel_requested,
            "created_at": task.created_at,
            "started_at": task.started_at,
            "finished_at": task.finished_at,
            "updated_at": task.updated_at,
            "eta_seconds": (0 if task.status in TERMINAL_STATUSES
                            else eta_seconds),
            "eta_confidence": ("none" if task.status in TERMINAL_STATUSES
                               else eta_confidence),
        }

    def enqueue(self, page: str, kind: str, job_key: str,
                resume_id: Optional[int], payload: Optional[dict] = None) -> dict:
        """加入单个任务；同页同岗位、简历和类型的活动任务会直接复用。"""
        page = self._validate_page(page)
        kind = str(kind or "").strip()
        job_key = str(job_key or "").strip()
        if not kind:
            raise ValueError("kind 不能为空")
        if not job_key:
            raise ValueError("job_key 不能为空")
        if payload is not None and not isinstance(payload, dict):
            raise ValueError("payload 必须是对象")
        normalized_resume_id = int(resume_id) if resume_id is not None else None
        active_key = _active_key(
            page, job_key, normalized_resume_id, kind, payload)

        with self._condition:
            if self._closed:
                raise RuntimeError("AI 任务调度器已关闭")
            existing_id = self._active_index.get(active_key)
            if existing_id:
                existing = self._tasks[existing_id]
                result = self._task_dict_locked(existing)
                result["deduplicated"] = True
                return result

            task = _Task(
                id=uuid.uuid4().hex,
                page=page,
                kind=kind,
                job_key=job_key,
                resume_id=normalized_resume_id,
                payload=_safe_copy(payload or {}),
                created_monotonic=self._clock_func(),
            )
            self._tasks[task.id] = task
            self._task_order.append(task.id)
            self._active_index[active_key] = task.id
            self._pages[page].queue.append(task.id)
            self._touch_locked(page)
            result = self._task_dict_locked(task)
            result["deduplicated"] = False
            return result

    submit = enqueue

    def enqueue_many(self, page: str, kind: str, items: list,
                     resume_id: Optional[int] = None) -> dict:
        """批量加入任务，item 可为 job_key 字符串或含 job_key/payload 的对象。"""
        tasks = []
        for item in items or []:
            if isinstance(item, dict):
                item_resume = item.get("resume_id", resume_id)
                task = self.enqueue(
                    page, item.get("kind", kind), item.get("job_key", ""),
                    item_resume, item.get("payload"),
                )
            else:
                task = self.enqueue(page, kind, str(item), resume_id)
            tasks.append(task)
        duplicate_count = sum(bool(task["deduplicated"]) for task in tasks)
        return {
            "tasks": tasks,
            "added": len(tasks) - duplicate_count,
            "deduplicated": duplicate_count,
        }

    submit_many = enqueue_many

    def _next_queued_locked(self, state: _PageState) -> Optional[_Task]:
        while state.queue:
            task_id = state.queue.popleft()
            task = self._tasks.get(task_id)
            if task and task.status == "queued" and not task.cancel_requested:
                return task
        return None

    def _dispatch_loop(self, page: str) -> None:
        state = self._pages[page]
        while True:
            with self._condition:
                task = None
                while not self._closed:
                    if (len(state.running) < state.max_concurrency and
                            len(state.executing) < state.effective_concurrency):
                        task = self._next_queued_locked(state)
                        if task is not None:
                            break
                    self._condition.wait()
                if self._closed:
                    return

                task.status = "running"
                task.started_at = task.started_at or _now_iso()
                task.started_monotonic = (
                    task.started_monotonic
                    if task.started_monotonic is not None
                    else self._clock_func())
                task.updated_at = _now_iso()
                state.running.add(task.id)
                state.executing.add(task.id)
                worker = threading.Thread(
                    target=self._execute_task,
                    args=(page, task.id),
                    name=f"ai-task-{page}-{task.id[:8]}",
                    daemon=True,
                )
                self._workers.add(worker)
                self._touch_locked(page)
                worker.start()

    def _cancelled_callback(self, task: _Task) -> Callable[[], bool]:
        return lambda: task.cancel_event.is_set() or self._closed

    @staticmethod
    def _error_text(error: BaseException) -> str:
        message = str(error).strip() or error.__class__.__name__
        return f"{error.__class__.__name__}: {message}"

    def _backoff_delay(self, retry_number: int) -> float:
        if self._delay_func is not None:
            return max(0.0, float(self._delay_func(retry_number)))
        return min(
            self._backoff_cap,
            self._backoff_base * (2 ** max(0, retry_number - 1)),
        )

    def _sleep_for_retry(self, task: _Task, delay: float) -> None:
        if delay <= 0:
            return
        if self._sleep_func is not None:
            self._sleep_func(delay)
        else:
            # 分段等待既能被取消立即唤醒，也避免超大 Retry-After 超过
            # threading.Condition 支持的 timeout 上限而让 worker 异常退出。
            remaining = delay
            while remaining > 0 and not task.cancel_event.is_set():
                chunk = min(remaining, MAX_RETRY_WAIT_CHUNK_SECONDS)
                started = time.monotonic()
                if task.cancel_event.wait(chunk):
                    return
                elapsed = max(0.0, time.monotonic() - started)
                remaining = max(
                    0.0, remaining - (elapsed if elapsed > 0 else chunk))

    def _mark_terminal_locked(self, task: _Task, status: str,
                              *, result=None, error: str = "") -> None:
        state = self._pages[task.page]
        now = self._clock_func()
        if status == "succeeded":
            self._record_success_duration_locked(state, task, now)
        task.status = status
        task.result = _safe_copy(result) if status == "succeeded" else None
        task.error = error
        task.progress = 1.0
        task.finished_at = _now_iso()
        task.finished_monotonic = now
        task.retry_ready_monotonic = None
        task.updated_at = task.finished_at
        state.running.discard(task.id)
        state.executing.discard(task.id)
        if self._active_index.get(task.active_key()) == task.id:
            self._active_index.pop(task.active_key(), None)
        self._touch_locked(task.page)

    def _execute_task(self, page: str, task_id: str) -> None:
        current_thread = threading.current_thread()
        try:
            while True:
                with self._condition:
                    task = self._tasks[task_id]
                    state = self._pages[page]
                    if task.cancel_requested or self._closed:
                        state.consecutive_successes = 0
                        self._mark_terminal_locked(task, "cancelled")
                        return
                    while (task.id not in state.executing and
                           len(state.executing) >= state.effective_concurrency and
                           not task.cancel_requested and not self._closed):
                        self._condition.wait()
                    if task.cancel_requested or self._closed:
                        state.consecutive_successes = 0
                        self._mark_terminal_locked(task, "cancelled")
                        return
                    state.executing.add(task.id)
                    is_retry = task.attempts > 0
                    task.attempts += 1
                    task.status = "running"
                    task.error = ""
                    if is_retry:
                        task.progress = None
                        task.progress_attempt = 0
                        task.message = ""
                        task.partial_output = ""
                    task.attempt_started_monotonic = self._clock_func()
                    task.retry_ready_monotonic = None
                    task.updated_at = _now_iso()
                    runner_task = self._task_dict_locked(task)
                    self._touch_locked(page)

                try:
                    result = self._task_runner(
                        runner_task, self._cancelled_callback(task))
                except Exception as error:
                    with self._condition:
                        if task.cancel_requested or self._closed:
                            state.consecutive_successes = 0
                            self._mark_terminal_locked(task, "cancelled")
                            return

                        error_text = self._error_text(error)
                        if not is_rate_limit_error(error):
                            state.consecutive_successes = 0
                            self._mark_terminal_locked(
                                task, "failed", error=error_text)
                            return

                        task.rate_limit_retries += 1
                        state.consecutive_successes = 0
                        state.executing.discard(task.id)
                        state.effective_concurrency = max(
                            1, state.effective_concurrency - 1)
                        can_retry = task.rate_limit_retries <= self._max_retries
                        if not can_retry:
                            self._mark_terminal_locked(
                                task, "failed", error=error_text)
                            return
                        task.status = "retrying"
                        task.error = error_text
                        task.message = (
                            f"请求限流，准备第 {task.rate_limit_retries} 次重试")
                        task.updated_at = _now_iso()
                        retry_after = _retry_after_seconds(error)
                        delay = (retry_after
                                 if retry_after is not None
                                 else self._backoff_delay(task.rate_limit_retries))
                        task.retry_ready_monotonic = self._clock_func() + delay
                        self._touch_locked(page)

                    self._sleep_for_retry(task, delay)
                    with self._condition:
                        if task.cancel_requested or self._closed:
                            state.consecutive_successes = 0
                            self._mark_terminal_locked(task, "cancelled")
                            return
                    continue

                with self._condition:
                    if task.cancel_requested or self._closed:
                        state.consecutive_successes = 0
                        self._mark_terminal_locked(task, "cancelled")
                        return
                    state.consecutive_successes += 1
                    if (state.effective_concurrency < state.max_concurrency and
                            state.consecutive_successes >= self._recovery_successes):
                        state.effective_concurrency += 1
                        state.consecutive_successes = 0
                    elif state.effective_concurrency >= state.max_concurrency:
                        state.consecutive_successes = 0
                    task.message = ""
                    self._mark_terminal_locked(
                        task, "succeeded", result=result)
                    return
        finally:
            with self._condition:
                self._workers.discard(current_thread)
                self._condition.notify_all()

    def update_task(self, task_id: str, *, progress=None, message=None,
                    partial_output=None) -> dict:
        """由 runner 的流式回调发布中间进度，并唤醒 SSE 订阅者。"""
        with self._condition:
            task = self._tasks.get(str(task_id))
            if task is None:
                raise ValueError("任务不存在")
            if task.status in TERMINAL_STATUSES:
                return self._task_dict_locked(task)
            if progress is not None:
                task.progress = max(0.0, min(1.0, float(progress)))
                task.progress_attempt = task.attempts
            if message is not None:
                task.message = str(message)
            if partial_output is not None:
                task.partial_output = str(partial_output)
            task.updated_at = _now_iso()
            self._touch_locked(task.page)
            return self._task_dict_locked(task)

    def cancel(self, task_id: Optional[str] = None, *, page: Optional[str] = None,
               job_key: Optional[str] = None, resume_id: Optional[int] = None,
               kind: Optional[str] = None) -> int:
        """取消符合条件的活动任务；运行中任务通过 cancelled 回调协作停止。"""
        if task_id is None and all(value is None for value in
                                   (page, job_key, resume_id, kind)):
            raise ValueError("取消任务至少需要一个筛选条件")
        if page is not None:
            page = self._validate_page(page)
        normalized_resume = int(resume_id) if resume_id is not None else None
        cancelled_count = 0
        with self._condition:
            candidates = ([self._tasks.get(str(task_id))] if task_id is not None
                          else list(self._tasks.values()))
            for task in candidates:
                if task is None or task.status not in ACTIVE_STATUSES:
                    continue
                if page is not None and task.page != page:
                    continue
                if job_key is not None and task.job_key != str(job_key):
                    continue
                if resume_id is not None and task.resume_id != normalized_resume:
                    continue
                if kind is not None and task.kind != str(kind):
                    continue
                if task.cancel_requested:
                    continue
                now = self._clock_func()
                task.cancel_estimate_seconds = min(
                    CANCEL_RELEASE_PRIOR_SECONDS,
                    self._task_remaining_seconds_locked(
                        self._pages[task.page], task, now),
                )
                task.cancel_requested_monotonic = now
                task.cancel_requested = True
                task.cancel_event.set()
                cancelled_count += 1
                self._pages[task.page].consecutive_successes = 0
                if task.status == "queued":
                    self._mark_terminal_locked(task, "cancelled")
                else:
                    task.message = "正在取消"
                    task.updated_at = _now_iso()
                    self._touch_locked(task.page)
            return cancelled_count

    def _page_snapshot_locked(self, page: str) -> dict:
        state = self._pages[page]
        task_objects = [self._tasks[task_id] for task_id in self._task_order
                        if self._tasks[task_id].page == page]
        eta, task_eta, task_confidence = self._page_eta_locked(
            page,
            [task for task in task_objects if task.status in ACTIVE_STATUSES],
            self._clock_func(),
        )
        tasks = [self._task_dict_locked(
            task,
            eta_seconds=task_eta.get(task.id),
            eta_confidence=task_confidence.get(task.id),
        ) for task in task_objects]
        counts = {status: 0 for status in
                  ("queued", "running", "retrying", "succeeded", "failed", "cancelled")}
        for task in tasks:
            counts[task["status"]] += 1
        completed = sum(counts[status] for status in TERMINAL_STATUSES)
        total = len(tasks)
        return {
            "page": page,
            "version": state.version,
            "global_version": self._version,
            "closed": self._closed,
            "effective_concurrency": state.effective_concurrency,
            "max_concurrency": state.max_concurrency,
            "active": len(state.running),
            "consecutive_successes": state.consecutive_successes,
            "progress": {
                "total": total,
                "completed": completed,
                "percent": round(completed * 100 / total) if total else 0,
                **eta,
                **counts,
            },
            "tasks": tasks,
        }

    def snapshot(self, page: Optional[str] = None) -> dict:
        """返回可直接 JSON 序列化的页级或全局状态快照。"""
        if page is not None:
            page = self._validate_page(page)
        with self._condition:
            if page is not None:
                return self._page_snapshot_locked(page)
            return {
                "version": self._version,
                "closed": self._closed,
                "pages": {name: self._page_snapshot_locked(name)
                          for name in VALID_PAGES},
            }

    def _scope_version_locked(self, page: Optional[str]) -> int:
        return self._version if page is None else self._pages[page].version

    def _scope_snapshot_locked(self, page: Optional[str]) -> dict:
        if page is not None:
            return self._page_snapshot_locked(page)
        return {
            "version": self._version,
            "closed": self._closed,
            "pages": {name: self._page_snapshot_locked(name)
                      for name in VALID_PAGES},
        }

    def event_stream(
            self,
            page: Optional[str] = None,
            *,
            since_version: Optional[int] = None,
            heartbeat: float = 15.0,
            cancelled: Optional[Callable[[], bool]] = None,
            ) -> Generator[str, None, None]:
        """生成 SSE 快照；无版本变化时只发送 keep-alive 注释。"""
        if page is not None:
            page = self._validate_page(page)
        heartbeat = float(heartbeat)
        if heartbeat <= 0:
            raise ValueError("heartbeat 必须大于 0")
        last_version = -1 if since_version is None else int(since_version)
        cancelled = cancelled or (lambda: False)

        while not cancelled():
            heartbeat_due = False
            payload = None
            stream_closed = False
            with self._condition:
                deadline = time.monotonic() + heartbeat
                while (self._scope_version_locked(page) == last_version and
                       not self._closed and not cancelled()):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        heartbeat_due = True
                        break
                    self._condition.wait(remaining)

                current_version = self._scope_version_locked(page)
                if current_version != last_version:
                    payload = self._scope_snapshot_locked(page)
                    last_version = current_version
                    stream_closed = self._closed
                elif self._closed or cancelled():
                    return
                else:
                    heartbeat_due = True

            if payload is not None:
                data = json.dumps(payload, ensure_ascii=False, default=str,
                                  separators=(",", ":"))
                yield (f"id: {last_version}\n"
                       "event: snapshot\n"
                       f"data: {data}\n\n")
                if stream_closed:
                    return
            elif heartbeat_due:
                yield ": keep-alive\n\n"

    def wait_for_idle(self, page: Optional[str] = None,
                      timeout: Optional[float] = None) -> bool:
        """等待指定页面或全部页面没有活动任务，主要用于测试和优雅退出。"""
        pages = ({self._validate_page(page)} if page is not None
                 else set(VALID_PAGES))
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._condition:
            while any(key[0] in pages for key in self._active_index):
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def close(self, *, wait: bool = True, timeout: Optional[float] = 2.0) -> None:
        """关闭调度器；排队任务立即取消，运行任务收到取消信号。"""
        with self._condition:
            if not self._closed:
                self._closed = True
                for task in list(self._tasks.values()):
                    if task.status not in ACTIVE_STATUSES:
                        continue
                    task.cancel_requested = True
                    task.cancel_event.set()
                    if task.status == "queued":
                        self._mark_terminal_locked(task, "cancelled")
                    else:
                        task.message = "正在取消"
                        task.updated_at = _now_iso()
                self._version += 1
                for state in self._pages.values():
                    state.version += 1
                self._condition.notify_all()

        if not wait:
            return
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        threads = [*self._dispatchers]
        with self._condition:
            threads.extend(self._workers)
        for thread in threads:
            if thread is threading.current_thread():
                continue
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            thread.join(remaining)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
