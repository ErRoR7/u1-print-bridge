"""Fetcher tests: file listing + file_id selection (GraphQL mocked)."""
import pytest

from backend import fetchers

GQL_FILES = {"print": {"name": "Test Model",
                       "otherFiles": [{"id": "111", "name": "a.3mf"},
                                      {"id": "222", "name": "b.3mf"}],
                       "stls": [{"id": "333", "name": "c.stl"}]}}
URL = "https://www.printables.com/model/123-test"


@pytest.fixture()
def gql(monkeypatch):
    monkeypatch.setattr(fetchers, "_gql", lambda q, v, **k: GQL_FILES)
    monkeypatch.setattr(fetchers.time, "sleep", lambda *a: None)


def test_list_printables_files_prefers_3mf(gql):
    title, files = fetchers.list_printables_files(URL)
    assert title == "Test Model"
    assert [f["id"] for f in files] == ["111", "222", "333"]
    assert files[0]["kind"] == "3mf"
    assert files[2]["kind"] == "stl"


def test_list_printables_files_bad_url():
    with pytest.raises(fetchers.FetchError):
        fetchers.list_printables_files("https://example.com/x")


def test_download_printables_file_id(gql, tmp_path, monkeypatch):
    seen = {}

    def fake_gql(q, v, **k):
        if "GetDownloadLink" in q:
            seen.update(v)
            return {"getDownloadLink": {"output": {"link": "http://x/f.3mf"}}}
        return GQL_FILES

    monkeypatch.setattr(fetchers, "_gql", fake_gql)
    monkeypatch.setattr(fetchers, "_download_stream",
                        lambda url, dest, **k: dest.write_bytes(b"fake3mf"))
    dest, _ = fetchers.download_printables(URL, tmp_path, file_id="222")
    assert seen["id"] == "222"
    assert dest.name == "model.3mf"
    with pytest.raises(fetchers.FetchError, match="no longer"):
        fetchers.download_printables(URL, tmp_path, file_id="999")


def test_download_model_passes_file_id(tmp_path, monkeypatch):
    monkeypatch.setattr(fetchers.time, "sleep", lambda *a: None)
    got = {}
    monkeypatch.setattr(fetchers, "download_printables",
                        lambda url, jd, file_id=None, **k: got.update(file_id=file_id) or (tmp_path / "m.3mf", "t"))
    fetchers.download_model(URL, tmp_path, file_id="222")
    assert got["file_id"] == "222"


# --- Rich search ----------------------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self._payload


GQL_SEARCH = {"data": {"searchPrints2": {"items": [
    {"id": "1", "name": "Flexi Dragon", "slug": "flexi-dragon",
     "likesCount": 1500, "downloadCount": 16000, "makesCount": 54,
     "datePublished": "2026-09-24T06:49:15.456454+00:00",
     "summary": "A wiggly dragon.", "user": {"handle": "KOGI3D"},
     "image": {"filePath": "media/prints/1/10.jpg"},
     "category": {"name": "Toys"}, "tags": [{"name": "dragon"}, {"name": "flexi"}]},
    {"id": "2", "name": "No Image Model", "slug": "no-image-model",
     "likesCount": None, "downloadCount": None, "makesCount": None,
     "datePublished": None, "summary": None, "user": None,
     "image": None, "category": None, "tags": None},
]}}}


def _fake_post(url, json=None, headers=None, timeout=None):
    return _FakeResp(GQL_SEARCH)
