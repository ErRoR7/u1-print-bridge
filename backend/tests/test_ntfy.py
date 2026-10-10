import httpx
import pytest

from backend import ntfy


def test_build_message_failed():
    title, body, tags, priority = ntfy.build_message(
        "print_failed", {"title": "Owl", "error": "printer reported state 'error'"})
    assert "Owl" in body
    assert "error" in body
    assert isinstance(tags, list) and tags
    assert 1 <= priority <= 5


def test_build_message_done():
    title, body, tags, priority = ntfy.build_message(
        "done", {"title": "Owl"}, duration="2h 5m")
    assert "finished" in title.lower() or "finished" in body.lower()
    assert "2h 5m" in body


def test_build_message_filament_low():
    title, body, tags, priority = ntfy.build_message(
        "filament_low", {"title": "Owl"},
        need="120", have="40", spool="Red PLA", toolhead="T1")
    assert "T1" in body and "Red PLA" in body and "40" in body
    assert priority >= 4


def test_build_message_unknown_event():
    with pytest.raises(KeyError):
        ntfy.build_message("nope", {"title": "x"})


def test_send_noop_when_no_topic(monkeypatch):
    called = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: called.append((a, k)))
    assert ntfy.send("https://ntfy.sh", "", "t", "b") is False
    assert called == []


def test_send_constructs_request(monkeypatch):
    captured = {}

    class Resp:
        def raise_for_status(self):
            pass

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return Resp()

    monkeypatch.setattr(httpx, "post", fake_post)
    assert ntfy.send("https://ntfy.sh", "u1-bridge", "Hello", "body text",
                     tags=["printer"], priority=4, click="https://x/#job-1") is True
    assert captured["url"] == "https://ntfy.sh/u1-bridge"
    headers = captured["kwargs"]["headers"]
    assert headers["Title"] == "Hello"
    assert headers["Tags"] == "printer"
    assert headers["Priority"] == "4"
    assert headers["Click"] == "https://x/#job-1"
    assert captured["kwargs"]["content"] == "body text".encode("utf-8")


def test_send_failure_returns_false(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("nope")

    monkeypatch.setattr(httpx, "post", boom)
    assert ntfy.send("https://ntfy.sh", "u1-bridge", "t", "b") is False


def test_health_disabled():
    h = ntfy.health("https://ntfy.sh", "")
    assert h["ok"] is True and h["detail"] == "disabled (no NTFY_TOPIC)"


def test_build_message_material_mismatch():
    title, body, tags, priority = ntfy.build_message(
        "material_mismatch", {"title": "Owl", "error": "material mismatch: 'X' at T1 is PETG"})
    assert "material" in title.lower()
    assert "PETG" in body
    assert priority >= 4
