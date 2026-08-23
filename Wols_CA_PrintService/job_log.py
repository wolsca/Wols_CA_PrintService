"""Step by step logging of a print job.

A job used to leave almost no trace: the journal only showed 'NEW PRINT JOB
DETECTED', a state change and, when it went wrong, one error line - so there was
no way to see *where* a booklet, a double sided or a single sided job stopped.
Every stage of the pipeline now reports itself here, and one place forwards that
to all three consumers:

  * the journal (`journalctl -u wolsca-print-service`), one line per step;
  * MQTT, so Home Assistant has a 'Print Job Step' sensor whose attributes are
    the complete timeline of the running job and a 'Print Job Result' sensor
    with the timeline of the last finished job (including the error);
  * the web app (`/api/joblog`) and, when `history.enabled` is on, a rolling
    history file, so the previous jobs survive a restart.

The step names are stable identifiers (`intake`, `mode`, `impose`, `dispatch`,
`printer`, `flip`, `done`, `failed`, ...), which makes them usable in a Home
Assistant automation as well.
"""

import json
import os
import threading
import traceback
from datetime import datetime

import config
import mqtt_service

# A job with a lot of copies can produce many progress steps; keep the timeline
# usable (and the MQTT payload small enough for Home Assistant attributes).
MAX_STEPS_PER_JOB = 120
MAX_JOBS_IN_MEMORY = 25

_lock = threading.Lock()
_jobs = []          # finished jobs, newest first
_current = None     # the running job, or None
_counter = 0


def _now():
    return datetime.now().isoformat(timespec="seconds")


def history_settings():
    return config.get_config().get("history", {}) or {}


def history_enabled():
    return bool(history_settings().get("enabled", True))


def history_file():
    """Where the finished jobs are kept. Empty configuration -> spool default."""
    configured = str(history_settings().get("file") or "").strip()
    return configured or os.path.join(config.TEMP_DIR, "job-history.json")


def max_entries():
    try:
        return max(1, int(history_settings().get("max_entries", 10)))
    except (TypeError, ValueError):
        return 10


def topic(name):
    return f"{mqtt_service.PREFIX}/job/{name}"


def _describe(fields):
    return " ".join(f"{key}={value}" for key, value in fields.items() if value not in (None, ""))


def _publish(name, payload, retain=True):
    try:
        mqtt_service.mqtt_client.publish(topic(name), json.dumps(payload), retain=retain)
    except Exception as e:
        print(f"[Warning] Could not publish the job log: {e}")


def _live_payload(job, entry):
    """Payload of the running job: the last step plus the whole timeline."""
    return {
        "job": job.get("id") if job else 0,
        "state": job.get("result") if job else "IDLE",
        "step": entry.get("step") if entry else None,
        "level": entry.get("level") if entry else "info",
        "message": entry.get("message") if entry else "",
        "filename": job.get("filename") if job else None,
        "source": job.get("source") if job else None,
        "print_mode": job.get("print_mode") if job else None,
        "printer": job.get("printer") if job else None,
        "pages": job.get("pages") if job else None,
        "sheets": job.get("sheets") if job else None,
        "started": job.get("started") if job else None,
        "steps": list(job.get("steps", [])) if job else [],
        "timestamp": _now()
    }


def start(filename, source="drop", **fields):
    """Opens a new job timeline. Returns its number."""
    global _current, _counter
    with _lock:
        _counter += 1
        _current = {
            "id": _counter,
            "filename": filename,
            "source": source,
            "started": _now(),
            "finished": None,
            "result": "RUNNING",
            "steps": []
        }
        _current.update({key: value for key, value in fields.items() if value is not None})
        job_id = _counter
    step("intake", f"Job accepted from '{source}'", filename=filename, **fields)
    return job_id


def field(**fields):
    """Records job facts (mode, printer, pages, sheets) without a timeline entry."""
    with _lock:
        if _current is not None:
            _current.update(fields)


def step(name, message="", level="info", **fields):
    """One stage of the job: journal line, MQTT event and timeline entry."""
    entry = {"time": _now(), "step": name, "level": level, "message": message}
    if fields:
        entry["data"] = {k: v for k, v in fields.items() if v is not None}

    with _lock:
        job = _current
        if job is not None:
            job["steps"].append(entry)
            if len(job["steps"]) > MAX_STEPS_PER_JOB:
                # Keep the first steps - the decisions are made there - and drop
                # from the middle, so the reason a job started this way is never
                # lost to a flood of progress lines.
                del job["steps"][MAX_STEPS_PER_JOB // 2]
            payload = _live_payload(job, entry)
        else:
            payload = _live_payload({}, entry)
        job_id = job.get("id") if job else 0

    extra = _describe(fields)
    prefix = f"[Job {job_id}]" if job_id else "[Job]"
    print(f"{prefix} {name}: {message}{f' ({extra})' if extra else ''}")

    _publish("step", payload)
    mqtt_service.publish_log(f"{name}: {message}{f' ({extra})' if extra else ''}", level)


def warn(name, message, **fields):
    step(name, message, "warning", **fields)


def error(name, message, exception=None, **fields):
    """A failing step, with the traceback kept for the report."""
    if exception is not None:
        with _lock:
            if _current is not None:
                _current["traceback"] = "".join(
                    traceback.format_exception(type(exception), exception, exception.__traceback__))
    step(name, message, "error", **fields)


def finish(result, message=""):
    """Closes the timeline and publishes it as the last finished job."""
    global _current
    with _lock:
        job = _current
    if job is None:
        return

    # The closing line still belongs to this job, so it is recorded before the
    # job is detached. A step that already reported the same thing (the 'failed'
    # step of the error handler) is not repeated.
    step_name = "done" if result == "COMPLETED" else ("cancelled" if result == "CANCELLED" else "failed")
    last_step = (job["steps"][-1].get("step") if job["steps"] else None)
    if last_step != step_name:
        step(step_name, message or result, "info" if result == "COMPLETED" else
             ("warning" if result == "CANCELLED" else "error"))
    with _lock:
        _current = None
        job["result"] = result
        job["detail"] = message
        job["finished"] = _now()
        _jobs.insert(0, job)
        del _jobs[MAX_JOBS_IN_MEMORY:]
        snapshot = json.loads(json.dumps(job))

    _publish("last", snapshot)
    _publish("step", _live_payload({}, {"step": "idle", "message": "Waiting for the next print job"}))
    save_history()


def current():
    with _lock:
        return json.loads(json.dumps(_current)) if _current is not None else None


def recent(limit=None):
    with _lock:
        jobs = json.loads(json.dumps(_jobs))
    return jobs[:limit] if limit else jobs


def payload():
    """What the web app shows: the running job plus the finished ones."""
    return {"current": current(), "jobs": recent(max_entries()), "text": text_report()}


def text_report():
    """The timeline as plain text - copyable from the web app, readable in HA."""
    lines = []
    jobs = [job for job in [current()] if job] + recent(max_entries())
    for job in jobs:
        header = f"#{job.get('id')} {job.get('filename') or '-'} [{job.get('result')}]"
        details = _describe({"mode": job.get("print_mode"), "printer": job.get("printer"),
                             "pages": job.get("pages"), "sheets": job.get("sheets"),
                             "copies": job.get("copies")})
        lines.append(f"{header}{f' - {details}' if details else ''}")
        for entry in job.get("steps", []):
            marker = {"error": "!", "warning": "~"}.get(entry.get("level"), " ")
            extra = _describe(entry.get("data") or {})
            lines.append(f"  {entry.get('time')} {marker} {entry.get('step')}: "
                         f"{entry.get('message')}{f' ({extra})' if extra else ''}")
        if job.get("traceback"):
            lines.append("  traceback:")
            lines.extend(f"    {line}" for line in job["traceback"].rstrip().splitlines())
        lines.append("")
    return "\n".join(lines).strip() or "No print job yet."


def save_history():
    """Writes the finished jobs to disk, trimmed to history.max_entries."""
    if not history_enabled():
        return
    path = history_file()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"updated": _now(), "jobs": recent(max_entries())}, handle, indent=2)
    except OSError as e:
        print(f"[Warning] Could not write the job history to {path}: {e}")


def load_history():
    """Restores the previous jobs at start-up, so a restart keeps the history."""
    global _jobs, _counter
    if not history_enabled():
        return
    path = history_file()
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as handle:
            jobs = (json.load(handle) or {}).get("jobs") or []
    except (OSError, ValueError) as e:
        print(f"[Warning] Could not read the job history from {path}: {e}")
        return
    with _lock:
        _jobs = [job for job in jobs if isinstance(job, dict)][:MAX_JOBS_IN_MEMORY]
        _counter = max([0] + [int(job.get("id") or 0) for job in _jobs])
    print(f"[Job] Restored {len(_jobs)} job(s) from {path}.")


def publish_ha_discovery():
    """Two sensors: the live step and the result of the last finished job."""
    device = mqtt_service.DEVICE_INFO

    live = {
        "name": mqtt_service.entity_name("Print Job Step"),
        "state_topic": topic("step"),
        "value_template": "{{ value_json.step }}",
        "json_attributes_topic": topic("step"),
        "icon": "mdi:format-list-numbered",
        "unique_id": mqtt_service.uid("wolsca_print_job_step"),
        "device": device
    }
    mqtt_service.mqtt_client.publish(
        f"{mqtt_service.HA_PREFIX}/sensor/{mqtt_service.NODE_ID}/job_step/config",
        json.dumps(live), retain=True)

    detail = {
        "name": mqtt_service.entity_name("Print Job Detail"),
        "state_topic": topic("step"),
        # 'level: message' is what tells an automation whether it went wrong.
        "value_template": "{{ (value_json.level ~ ': ' ~ value_json.message)[:250] }}",
        "icon": "mdi:text-long",
        "unique_id": mqtt_service.uid("wolsca_print_job_detail"),
        "device": device
    }
    mqtt_service.mqtt_client.publish(
        f"{mqtt_service.HA_PREFIX}/sensor/{mqtt_service.NODE_ID}/job_detail/config",
        json.dumps(detail), retain=True)

    last = {
        "name": mqtt_service.entity_name("Print Job Result"),
        "state_topic": topic("last"),
        "value_template": "{{ value_json.result }}",
        "json_attributes_topic": topic("last"),
        "icon": "mdi:printer-check",
        "unique_id": mqtt_service.uid("wolsca_print_job_result"),
        "device": device
    }
    mqtt_service.mqtt_client.publish(
        f"{mqtt_service.HA_PREFIX}/sensor/{mqtt_service.NODE_ID}/job_result/config",
        json.dumps(last), retain=True)
