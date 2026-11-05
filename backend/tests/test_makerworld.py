"""MakerWorld download tests: design-service f3mf API path (mocked HTTP).

Grounded in the live-verified behavior (2026-11-04):
- GET api.bambulab.com/v1/design-service/design/{id} answers anonymously
  with design metadata incl. defaultInstanceId.
- GET makerworld.com/api/v1/design-service/instance/{id}/f3mf?type=download
  answers {"code":1,"error":"Please log in to download models."} without a
  session cookie; with one it returns {name, url} (presigned S3 link) or the
  3MF binary directly (both shapes are handled).
"""
import pytest

from backend import fetchers

URL = ("https://makerworld.com/en/models/1814598-fc-cincinnati-coaster"
       "?from=search#profileId-1936219")
META = {"id": 1814598, "title": "FC Cincinnati coaster", "defaultInstanceId": 1936219}


class _Resp:
    def __init__(self, status=200, content=b"", headers=None, json_data=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}
        self._json = json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")

    def json(self):
        if self._json is not None:
            return self._json
        import json as _json
        return _json.loads(self.content)


@pytest.fixture()
def no_sleep(monkeypatch):
    monkeypatch.setattr(fetchers.time, "sleep", lambda *a: None)


def _dispatch(monkeypatch, *, meta=META, f3mf=None, page=b"<html></html>"):
    """Fake httpx.get: metadata API, f3mf API, or the model page."""
    seen = {}

    def fake_get(url, **kw):
        seen.setdefault("calls", []).append(url)
        if "f3mf" in url:
            seen["f3mf_headers"] = kw.get("headers") or {}
            return f3mf() if callable(f3mf) else f3mf
        if "api.bambulab.com" in url:
            return _Resp(200, json_data=meta,
                         headers={"content-type": "application/json"})
        return _Resp(200, content=page, headers={"content-type": "text/html"})

    monkeypatch.setattr(fetchers.httpx, "get", fake_get)
    return seen


def test_ids_from_fragment():
    design, instance = fetchers._makerworld_ids(URL)
    assert design == "1814598"
    assert instance == "1936219"


def test_ids_from_query_and_missing():
    design, instance = fetchers._makerworld_ids(
        "https://makerworld.com/en/models/1814598-x?profileId=555")
    assert (design, instance) == ("1814598", "555")
    design, instance = fetchers._makerworld_ids(
        "https://makerworld.com/en/models/1814598-x")
    assert (design, instance) == ("1814598", None)


def test_f3mf_json_url(tmp_path, monkeypatch, no_sleep):
    f3mf = _Resp(200, json_data={"name": "coaster.3mf", "url": "https://s3.example/coaster.3mf"},
                 headers={"content-type": "application/json"})
    seen = _dispatch(monkeypatch, f3mf=f3mf)
    downloaded = {}
    monkeypatch.setattr(fetchers, "_download_stream",
                        lambda url, dest, **k: downloaded.update(url=url) or dest.write_bytes(b"PK-fake"))
    dest, title = fetchers.download_makerworld(URL, tmp_path, session_cookie="token=abc")
    assert dest.name == "model.3mf"
    assert title == "FC Cincinnati coaster"
    assert downloaded["url"] == "https://s3.example/coaster.3mf"
    assert seen["f3mf_headers"].get("Cookie") == "token=abc"
    assert any("1936219/f3mf" in c for c in seen["calls"])


def test_f3mf_binary_direct(tmp_path, monkeypatch, no_sleep):
    f3mf = _Resp(200, content=b"PK\x03\x04binary-3mf",
                 headers={"content-type": "application/octet-stream"})
    _dispatch(monkeypatch, f3mf=f3mf)
    dest, title = fetchers.download_makerworld(URL, tmp_path, session_cookie="token=abc")
    assert dest.read_bytes()[:2] == b"PK"
    assert title == "FC Cincinnati coaster"


def test_f3mf_login_required(tmp_path, monkeypatch, no_sleep):
    f3mf = _Resp(200, json_data={"code": 1, "error": "Please log in to download models."},
                 headers={"content-type": "application/json"})
    _dispatch(monkeypatch, f3mf=f3mf)
    with pytest.raises(fetchers.NeedsManualUpload) as excinfo:
        fetchers.download_makerworld(URL, tmp_path)
    assert "login" in str(excinfo.value).lower()
    assert "MAKERWORLD_SESSION_COOKIE" in str(excinfo.value)


def test_f3mf_unauthorized(tmp_path, monkeypatch, no_sleep):
    _dispatch(monkeypatch, f3mf=_Resp(401, content=b""))
    with pytest.raises(fetchers.NeedsManualUpload) as excinfo:
        fetchers.download_makerworld(URL, tmp_path, session_cookie="token=stale")
    assert "login" in str(excinfo.value).lower()


def test_no_instance_uses_default_from_metadata(tmp_path, monkeypatch, no_sleep):
    f3mf = _Resp(200, json_data={"name": "c.3mf", "url": "https://s3.example/c.3mf"},
                 headers={"content-type": "application/json"})
    seen = _dispatch(monkeypatch, f3mf=f3mf)
    monkeypatch.setattr(fetchers, "_download_stream",
                        lambda url, dest, **k: dest.write_bytes(b"PK-fake"))
    dest, title = fetchers.download_makerworld(
        "https://makerworld.com/en/models/1814598-fc-cincinnati-coaster",
        tmp_path, session_cookie="token=abc")
    assert dest.name == "model.3mf"
    assert any("1936219/f3mf" in c for c in seen["calls"])


def test_api_failure_falls_back_to_legacy_then_manual(tmp_path, monkeypatch, no_sleep):
    def boom(url, **kw):
        if "f3mf" in url:
            raise Exception("boom")
        if "api.bambulab.com" in url:
            return _Resp(200, json_data=META, headers={"content-type": "application/json"})
        return _Resp(200, content=b"<html><title>x</title></html>",
                     headers={"content-type": "text/html"})

    monkeypatch.setattr(fetchers.httpx, "get", boom)
    with pytest.raises(fetchers.NeedsManualUpload):
        fetchers.download_makerworld(URL, tmp_path, session_cookie="token=abc")
