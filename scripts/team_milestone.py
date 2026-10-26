#!/usr/bin/env python3
"""Verify routines and bot collaboration through an isolated local service."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
MODEL_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY")


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("fixture", "hermes"), default="fixture")
    parser.add_argument("--timeout", type=int, default=170, help="Total limit in seconds, at most 170.")
    parser.add_argument("--data-dir", type=Path, help="An empty directory inside this project's .runtime.")
    args = parser.parse_args()
    if not 60 <= args.timeout <= 170:
        parser.error("--timeout must be between 60 and 170 seconds.")
    if RUNTIME.is_symlink():
        raise SystemExit("The runtime is a symbolic link. The milestone did not follow it.")
    RUNTIME.mkdir(exist_ok=True, mode=0o700)
    data_dir = (args.data_dir or Path(tempfile.mkdtemp(prefix=f"team-milestone-{args.provider}-", dir=RUNTIME))).resolve()
    if not data_dir.is_relative_to(RUNTIME.resolve()) or data_dir == (RUNTIME / "host").resolve():
        raise SystemExit("Use a new directory inside .runtime. The live host cannot run this milestone.")
    if data_dir.exists() and any(data_dir.iterdir()):
        raise SystemExit("The milestone needs an empty directory. Existing data remains unchanged.")
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    data_dir.chmod(0o700)
    deadline = time.monotonic() + args.timeout
    began = time.monotonic()
    process = None
    token = None
    stage = "setup"
    report = {"provider": args.provider, "real_hermes": args.provider == "hermes",
              "data_dir": str(data_dir.relative_to(ROOT)), "passed": False}
    log = (data_dir / "service.log").open("ab")

    names = ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR",
             "COCONUT_MODEL", "COCONUT_MODEL_BASE_URL", "COCONUT_HERMES_PYTHON", "COCONUT_HERMES_ROOT")
    env = {name: os.environ[name] for name in names if name in os.environ}
    env["PYTHONUNBUFFERED"] = "1"

    def remaining() -> float:
        available = deadline - time.monotonic()
        if available <= 0:
            raise TimeoutError("The milestone reached its total time limit.")
        return available

    def request(method, path, body=None, key=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if method != "GET":
            headers["Idempotency-Key"] = key or str(uuid4())
        payload = None if body is None else json.dumps(body).encode()
        with urlopen(Request(address + path, data=payload, headers=headers, method=method),
                     timeout=min(5, remaining())) as response:
            return json.load(response)

    def state():
        return request("GET", "/v1/state")

    def start():
        nonlocal process
        process = subprocess.Popen([sys.executable, "-m", "coconut", "--data-dir", str(data_dir),
                                    "--port", str(port), "--provider", args.provider],
                                   cwd=ROOT / "backend", env=env, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=log, start_new_session=True)
        health_deadline = min(deadline, time.monotonic() + 10)
        while time.monotonic() < health_deadline:
            if process.poll() is not None:
                raise RuntimeError("The isolated service exited during startup.")
            try:
                request("GET", "/health")
                return
            except (OSError, URLError):
                time.sleep(.1)
        raise TimeoutError("The isolated service did not become healthy.")

    def stop(*, crash=False):
        if process is None or process.poll() is not None:
            return
        process.kill() if crash else process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    def until(predicate, check):
        nonlocal stage
        stage = check
        while True:
            remaining()
            current = state()
            if any(task["status"] == "failed" for task in current["tasks"]):
                raise AssertionError("A milestone task failed.")
            if predicate(current):
                return current
            time.sleep(min(.2, remaining()))

    def task_in(snapshot, task_id):
        return next(task for task in snapshot["tasks"] if task["id"] == task_id)

    def completed(snapshot, task_id):
        return task_in(snapshot, task_id)["status"] == "completed"

    def events():
        result = []
        cursor = 0
        while True:
            page = request("GET", f"/v1/events?after={cursor}&wait=0")
            if not page["events"]:
                return result
            result.extend(page["events"])
            assert page["cursor"] > cursor
            cursor = page["cursor"]

    def routine_events(routine_id):
        return [event for event in events() if event["type"] == "routine.triggered"
                and event["data"]["routine_id"] == routine_id]

    def thread_tasks(snapshot, thread_id):
        return [task for task in snapshot["tasks"] if task["thread_id"] == thread_id]

    def settled_thread(thread_id):
        nonlocal stage
        stage = "automatic_routine_delegates_complete"
        signature = None
        unchanged_since = time.monotonic()
        while True:
            remaining()
            snapshot = state()
            tasks = thread_tasks(snapshot, thread_id)
            if any(task["status"] == "failed" for task in tasks):
                raise AssertionError("A routine task failed.")
            current = [(task["id"], task["status"], task["updated_at"]) for task in tasks]
            if current != signature:
                signature = current
                unchanged_since = time.monotonic()
            if tasks and all(task["status"] == "completed" for task in tasks) and time.monotonic() - unchanged_since >= 1:
                return snapshot
            time.sleep(min(.2, remaining()))

    try:
        if args.provider == "hermes":
            from host import load_credentials
            load_credentials(RUNTIME / "host")
            env.update({name: os.environ[name] for name in MODEL_KEYS if name in os.environ})
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        address = f"http://127.0.0.1:{port}"
        start()
        sys.path.insert(0, str(ROOT / "backend"))
        from coconut.server import create_pairing_code
        token = request("POST", "/v1/pair", {
            "code": create_pairing_code(data_dir), "device_name": "Team milestone",
        })["token"]
        bot_a = request("POST", "/v1/bots", {
            "name": "Ada", "role": "Coordinates a brief check", "avatar": "pencil.line",
            "color": "#BA5D3B", "instructions": "Keep replies under two short sentences. Use Coconut tools for requests to another bot.",
        })
        bot_b = request("POST", "/v1/bots", {
            "name": "Lin", "role": "Checks arithmetic", "avatar": "checkmark.seal",
            "color": "#517568", "instructions": "Answer briefly. When another bot requests a check, send the result to that bot once.",
        })
        assert bot_a["id"] != bot_b["id"]
        assert bot_a["session_id"] != bot_b["session_id"]
        report["bots"] = [bot_a["id"], bot_b["id"]]
        report["separate_sessions"] = True

        stage = "routine_create_edit_pause_resume"
        scheduled_minute = (datetime.now(timezone.utc) + timedelta(minutes=1, seconds=2)).replace(second=0, microsecond=0)
        routine = request("POST", f'/v1/bots/{bot_a["id"]}/routines', {
            "name": "Minute check", "prompt": "Check that 17 multiplied by 19 equals 323. Answer in one short sentence.",
            "cron": f"{scheduled_minute.minute} {scheduled_minute.hour} * * *",
            "timezone": "UTC", "enabled": True,
        })
        assert routine["enabled"] is True
        edited = request("PATCH", f'/v1/routines/{routine["id"]}', {"name": "Scheduled check"})
        assert edited["name"] == "Scheduled check" and edited["id"] == routine["id"]
        paused = request("PATCH", f'/v1/routines/{routine["id"]}', {"enabled": False})
        assert paused["enabled"] is False
        assert routine_events(routine["id"]) == []
        resumed = request("PATCH", f'/v1/routines/{routine["id"]}', {"enabled": True})
        assert resumed["enabled"] is True
        assert datetime.fromisoformat(resumed["next_run_at"].replace("Z", "+00:00")) <= scheduled_minute
        report.update(routine_id=routine["id"], routine_edited=True, routine_paused=True, routine_resumed=True)

        stage = "mention_dispatch"
        mention_key = str(uuid4())
        mention_body = {"text": "Lin, check that 17 multiplied by 19 equals 323. Reply here in one short sentence. Do not message another bot.",
                        "mode": "queue", "recipient_bot_ids": [bot_b["id"], bot_b["id"]]}
        mention = request("POST", f'/v1/bots/{bot_a["id"]}/messages', mention_body, mention_key)
        repeated_mention = request("POST", f'/v1/bots/{bot_a["id"]}/messages', mention_body, mention_key)
        assert repeated_mention == mention
        assert mention["bot_id"] == bot_b["id"]
        assert mention["conversation_bot_id"] == bot_a["id"]
        assert len(mention["dispatched_task_ids"]) == 1

        stage = "tool_collaboration_dispatch"
        handoff = request("POST", f'/v1/bots/{bot_a["id"]}/messages', {
            "text": "TEAM_MILESTONE_HANDOFF: Use coconut_message_bot to ask Lin for the result of 17 multiplied by 19. "
                    "Ask Lin to use coconut_message_bot to send that result back to you once. "
                    "When Lin returns the result, report it here without sending another bot message. Do not create a file.",
            "mode": "queue", })

        stage = "queued_cancellation"
        scheduled_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        future = request("POST", f'/v1/bots/{bot_a["id"]}/messages', {
            "text": "Check 17 multiplied by 19 without creating a file.",
            "mode": "queue", "scheduled_at": scheduled_at,
        })
        assert future["status"] == "scheduled"
        cancelled = request("POST", f'/v1/tasks/{future["id"]}/cancel', {})
        assert cancelled["status"] == "cancelled"

        observed = until(lambda snapshot: completed(snapshot, mention["id"]), "mention_response")
        mention_messages = [message for message in observed["messages"]
                            if message["task_id"] == mention["id"] and message["role"] == "assistant"
                            and message["kind"] == "text" and message["text"]]
        assert mention_messages
        assert all(message["bot_id"] == bot_a["id"] and message["sender_bot_id"] == bot_b["id"]
                   and message["thread_id"] == mention["thread_id"] for message in mention_messages)
        report.update(mention_task_id=mention["id"], mention_response_attributed=True,
                      mention_retry_idempotent=True, duplicate_recipients_removed=True)

        def collaboration_finished(snapshot):
            tasks = thread_tasks(snapshot, handoff["thread_id"])
            return len(tasks) >= 3 and all(task["status"] == "completed" for task in tasks)

        observed = until(collaboration_finished, "tool_collaboration_return")
        exchange_tasks = thread_tasks(observed, handoff["thread_id"])
        assert len(exchange_tasks) == 3
        assert [task["bot_id"] for task in exchange_tasks] == [bot_a["id"], bot_b["id"], bot_a["id"]]
        assert all(task["conversation_bot_id"] == bot_a["id"] for task in exchange_tasks)
        assert exchange_tasks[1]["source_bot_id"] == bot_a["id"]
        assert exchange_tasks[2]["source_bot_id"] == bot_b["id"]
        assert len({task["exchange_id"] for task in exchange_tasks}) == 1
        tool_events = [event for event in events() if event["type"] == "bot_message.delivered"
                       and event["task_id"] in {task["id"] for task in exchange_tasks}]
        assert len(tool_events) == 2
        assert all(event["data"]["exchange_id"] == handoff["exchange_id"] for event in tool_events)
        assert [event["data"]["deliveries"] for event in tool_events] == [1, 2]
        handoff_messages = []
        for task in exchange_tasks:
            messages = [message for message in observed["messages"] if message["task_id"] == task["id"]
                        and message["role"] == "assistant" and message["kind"] == "text" and message["text"]]
            replies = [message for message in messages if not message.get("recipient_bot_ids")]
            directed = [message for message in messages if message.get("recipient_bot_ids")]
            if task["id"] == exchange_tasks[-1]["id"]:
                assert replies
            assert all(message["bot_id"] == bot_a["id"]
                       and message["thread_id"] == handoff["thread_id"] for message in messages)
            assert all(message["sender_bot_id"] == task["bot_id"] for message in replies)
            assert all(message["sender_bot_id"] == task["source_bot_id"]
                       and message["recipient_bot_ids"] == [task["bot_id"]] for message in directed)
            handoff_messages.extend(directed)
        assert len(handoff_messages) == 2
        for event in tool_events:
            delivery = event["data"]
            matching_messages = [message for message in handoff_messages
                                 if message["task_id"] == delivery["task_id"]
                                 and message["sender_bot_id"] == delivery["source_bot_id"]
                                 and message["recipient_bot_ids"] == [delivery["target_bot_id"]]]
            assert len(matching_messages) == 1
        report.update(collaboration_thread_id=handoff["thread_id"], collaboration_task_ids=[task["id"] for task in exchange_tasks],
                      collaboration_delivery_count=len(tool_events),
                      reported_delivery_count=max(event["data"]["deliveries"] for event in tool_events),
                      handoff_message_count=len(handoff_messages), handoff_messages_attributed=True,
                      collaboration_return_attributed=True, same_thread=True)

        stage = "automatic_routine_occurrence"
        while not routine_events(routine["id"]):
            remaining()
            time.sleep(min(.2, remaining()))
        triggers = routine_events(routine["id"])
        assert len(triggers) == 1
        occurrence = triggers[0]["data"]
        observed = until(lambda snapshot: completed(snapshot, occurrence["task_id"]), "automatic_routine_completion")
        routine_thread_id = task_in(observed, occurrence["task_id"])["thread_id"]
        observed = settled_thread(routine_thread_id)
        routine_task_ids = {task["id"] for task in thread_tasks(observed, routine_thread_id)}
        before_cursor = observed["cursor"]
        before_routine = next(item for item in observed["routines"] if item["id"] == routine["id"])

        stage = "hard_restart_occurrence_reconciliation"
        stop(crash=True)
        start()
        # The dispatcher gets a full tick while the routine remains enabled.
        time.sleep(min(1, remaining()))
        restored = state()
        assert completed(restored, occurrence["task_id"])
        assert task_in(restored, future["id"])["status"] == "cancelled"
        restored_routine = next(item for item in restored["routines"] if item["id"] == routine["id"])
        assert restored_routine["enabled"] is True
        assert restored_routine["next_run_at"] == before_routine["next_run_at"]
        after_triggers = routine_events(routine["id"])
        assert len(after_triggers) == 1
        matching = [event for event in after_triggers if event["data"]["occurrence_id"] == occurrence["occurrence_id"]]
        assert len(matching) == 1
        assert len({event["data"]["occurrence_id"] for event in after_triggers}) == len(after_triggers)
        final_routine = request("PATCH", f'/v1/routines/{routine["id"]}', {"enabled": False})
        assert final_routine["enabled"] is False
        assert len([task for task in restored["tasks"] if task["id"] == occurrence["task_id"]]) == 1
        # Delegation creates more tasks, but only the occurrence root carries routine_id.
        scheduled_tasks = [task for task in restored["tasks"] if task.get("routine_id") == routine["id"]]
        assert len(scheduled_tasks) == 1
        assert {task["id"] for task in thread_tasks(restored, routine_thread_id)} == routine_task_ids
        assert all(task["status"] == "completed" for task in thread_tasks(restored, routine_thread_id))
        assert not any(message["task_id"] == future["id"] and message["role"] == "assistant" for message in restored["messages"])
        assert len(thread_tasks(restored, handoff["thread_id"])) == 3
        assert len(thread_tasks(restored, mention["thread_id"])) == 1
        replay = request("GET", f"/v1/events?after={before_cursor}&wait=0")
        assert replay["events"]
        if args.provider == "hermes":
            homes = [data_dir / "bots" / bot["id"] / "home" for bot in (bot_a, bot_b)]
            assert all(home.is_dir() for home in homes)
            assert homes[0].resolve() != homes[1].resolve()
        report.update(automatic_occurrence_id=occurrence["occurrence_id"],
                      automatic_task_id=occurrence["task_id"], matching_occurrence_count=len(matching),
                      scheduled_task_count=len(scheduled_tasks),
                      routine_thread_task_count=len(routine_task_ids),
                      unique_scheduled_occurrences=True, hard_restarts=1, queued_cancellation=True,
                      cancelled_task_id=future["id"], event_replay=True,
                      separate_hermes_homes=args.provider == "hermes", budget_exhaustion_tested=False,
                      final_task_count=len(restored["tasks"]), routine_final_enabled=False,
                      final_task_statuses={task["id"]: task["status"] for task in restored["tasks"]}, passed=True)
    except Exception as error:
        report.update(failed_check=stage, error_type=type(error).__name__)
        if isinstance(error, HTTPError):
            report["http_status"] = error.code
        raise SystemExit(1) from None
    finally:
        stop()
        log.close()
        report["elapsed_seconds"] = round(time.monotonic() - began, 2)
        (data_dir / "team-milestone-report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
