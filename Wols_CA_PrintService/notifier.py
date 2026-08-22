"""Push notifications to a phone through ntfy (or a Gotify compatible server).

Only the standard library is used: a notification must never be the reason the
print workflow fails, so every error is caught and reported once in the log.

The topic is what identifies the phone. It is generated with a random suffix at
first use and stored in the configuration, because on the public ntfy.sh server
anybody who knows the topic name can read along.
"""

import secrets
import threading
import urllib.error
import urllib.request

import config

TOPIC_PREFIX = "wolsca_print_service"
REQUEST_TIMEOUT = 8


def get_settings():
    return config.get_config().get("notify", {})


def ensure_topic():
    """Returns the topic, generating and saving a unique one when it is empty."""
    notify = config.get_config().setdefault("notify", {})
    topic = str(notify.get("topic") or "").strip()
    if topic:
        return topic

    topic = f"{TOPIC_PREFIX}_{secrets.token_hex(4)}"
    notify["topic"] = topic
    config.save_config()
    print(f"[Notify] Generated notification topic '{topic}'. Subscribe to it in "
          f"the ntfy app on your phone: {subscribe_url(topic)}")
    return topic


def subscribe_url(topic=None):
    """The address to open or scan on the phone."""
    notify = get_settings()
    url = str(notify.get("url") or "https://ntfy.sh").rstrip("/")
    return f"{url}/{topic or notify.get('topic', '')}"


def is_enabled():
    notify = get_settings()
    return bool(notify.get("enabled", False))


def send(message, title=None, priority=None, tags=None, click=None):
    """Sends a notification in the background; never raises."""
    if not is_enabled():
        return False
    threading.Thread(target=_send_now,
                     args=(message, title, priority, tags, click),
                     daemon=True).start()
    return True


def _send_now(message, title, priority, tags, click):
    notify = get_settings()
    try:
        topic = ensure_topic()
        url = str(notify.get("url") or "https://ntfy.sh").rstrip("/")
        request = urllib.request.Request(f"{url}/{topic}",
                                        data=str(message).encode("utf-8"),
                                        method="POST")
        request.add_header("Title", _header(title or "Wols CA Print Service"))
        request.add_header("Priority", str(priority or notify.get("priority") or "default"))
        if tags:
            request.add_header("Tags", tags if isinstance(tags, str) else ",".join(tags))
        if click:
            request.add_header("Click", click)
        token = str(notify.get("token") or "").strip()
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            response.read()
        return True
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"[Notify] Could not send the notification: {e}")
        return False


def _header(value):
    # HTTP headers are latin-1; ntfy titles with accents would otherwise raise.
    return str(value).encode("ascii", "replace").decode("ascii")


def notify_flip(filename, web_url=None):
    """The front side is printed and the user has to flip the paper."""
    return send(f"'{filename}': the front side is printed. Flip the paper and "
                f"confirm to print the back side.",
                title="Flip the paper",
                priority=str(get_settings().get("priority") or "high"),
                tags="repeat",
                click=web_url)


def notify_error(message, filename=None):
    """A job failed. Only sent when notify_on_error is on."""
    if not get_settings().get("notify_on_error", True):
        return False
    subject = f"'{filename}': " if filename else ""
    return send(f"{subject}{message}", title="Print error",
                priority="high", tags="warning")


def notify_completed(filename, pages=None):
    """A job finished. Low priority: no sound on the phone."""
    detail = f" ({pages} pages)" if pages else ""
    return send(f"'{filename}' is printed{detail}.", title="Print job ready",
                priority="low", tags="printer")


def self_test():
    """Sends a test notification, used by the diagnostics phase and the web app."""
    notify = get_settings()
    if not notify.get("enabled", False):
        return False, "Notifications are switched off (notify.enabled)."
    topic = ensure_topic()
    ok = _send_now("This is a test notification from the Wols CA Print Service.",
                   "Test notification", notify.get("priority"), "white_check_mark", None)
    if ok:
        return True, f"Test notification sent to {subscribe_url(topic)}"
    return False, f"Sending to {subscribe_url(topic)} failed; see the log."


def describe():
    """Short summary for the web app and the self-test."""
    notify = get_settings()
    return {
        "enabled": bool(notify.get("enabled", False)),
        "url": notify.get("url", ""),
        "topic": notify.get("topic", ""),
        "subscribe_url": subscribe_url(),
        "notify_on_error": bool(notify.get("notify_on_error", True)),
    }


if __name__ == "__main__":
    ok, detail = self_test()
    print(detail)
    raise SystemExit(0 if ok else 1)
