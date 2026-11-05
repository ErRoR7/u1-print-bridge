"""Model acquisition for the U1 print bridge.

Sources:
- Printables: public GraphQL API (api.printables.com/graphql) -- verified
  working end-to-end from a cloud VM, no auth. 2s delay between calls.
- MakerWorld: design-service f3mf API (makerworld.com/api/v1/design-service/
  instance/{instanceId}/f3mf?type=download). The model page itself is a
  Next.js shell with no download links in its HTML, so page scraping cannot
  work; the API returns either a presigned S3 URL ({name, url}) or the 3MF
  binary directly. Downloading requires a logged-in MakerWorld session: pass
  MAKERWORLD_SESSION_COOKIE (the browser's Cookie header value for
  makerworld.com, see .env.example). Without it the API answers "Please log
  in" and we ask for a manual upload. We NEVER attempt to bypass login walls
  or CAPTCHAs. The legacy page scrape is kept as a last-resort fallback.
- Other URLs: direct download with content-type/length sanity + extension or
  magic-byte checks.

Downloaded files land in the job dir as model.3mf / model.stl.
"""
from __future__ import annotations

import logging
import re
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import httpx

log = logging.getLogger("u1-bridge.fetchers")

PRINTABLES_GQL = "https://api.printables.com/graphql/"
PRINTABLES_MODEL_RE = re.compile(r"printables\.com/model/(\d+)", re.I)
MAKERWORLD_RE = re.compile(r"makerworld\.com", re.I)
# /models/{designId}-{slug} with an optional #profileId-{instanceId} fragment
# (or ?profileId= query). The fragment id is the *instance* id, which is what
# the f3mf download endpoint expects -- not the numeric profileId inside the
# design data.
MAKERWORLD_MODEL_RE = re.compile(r"makerworld\.com/(?:[a-z]{2}/)?models/(\d+)", re.I)
MAKERWORLD_INSTANCE_RE = re.compile(r"profileId[-=](\d+)", re.I)
# Anonymous design metadata (title + default instance). Download itself needs
# a logged-in session (see download_makerworld).
BAMBULAB_DESIGN_API = "https://api.bambulab.com/v1/design-service/design/{design_id}"
MAKERWORLD_F3MF_API = "https://makerworld.com/api/v1/design-service/instance/{instance_id}/f3mf"

_MAKERWORLD_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_GQL_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
}

_PRINT_FILES_QUERY = """
query PrintFiles($id: ID!) {
  print(id: $id) {
    name
    stls { id name }
    otherFiles { id name }
  }
}
"""

_DOWNLOAD_MUTATION = """
mutation GetDownloadLink($id: ID!, $printId: ID!, $fileType: DownloadFileTypeEnum, $source: DownloadSourceEnum!) {
  getDownloadLink(id: $id, printId: $printId, fileType: $fileType, source: $source) {
    ok
    output { link }
  }
}
"""


class FetchError(Exception):
    """Download failed in a way the user can act on (message is user-facing)."""


class NeedsManualUpload(FetchError):
    """MakerWorld (or another gated source) needs a manual 3MF upload."""

    def __init__(self, message: str | None = None):
        super().__init__(
            message
            or "MakerWorld needs a manual upload \u2014 download the 3MF in your browser and attach it here."
        )


MAKERWORLD_LOGIN_HINT = (
    "MakerWorld needs a login to download this model \u2014 paste your MakerWorld "
    "session cookie into MAKERWORLD_SESSION_COOKIE (see .env.example) or "
    "download the 3MF in your browser and attach it here."
)


def canonical_printables_url(print_id: str, name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "model"
    return f"https://www.printables.com/model/{print_id}-{slug}"


def _gql(query: str, variables: dict, timeout: float = 20.0) -> dict:
    resp = httpx.post(PRINTABLES_GQL, json={"query": query, "variables": variables},
                      headers=_GQL_HEADERS, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        raise FetchError(f"Printables API error: {data['errors'][0].get('message')}")
    return data["data"]


def _download_stream(url: str, dest: Path, timeout: float = 120.0) -> None:
    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as resp:
        resp.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in resp.iter_bytes(65536):
                fh.write(chunk)


def list_printables_files(url: str, delay: float = 2.0) -> tuple[str, list[dict]]:
    """List downloadable 3MF/STL files on a Printables model page.

    Returns (title, files) with files = [{id, name, kind}] in preference
    order (3MFs first). Raises FetchError on failure.
    """
    m = PRINTABLES_MODEL_RE.search(url)
    if not m:
        raise FetchError("That doesn't look like a Printables model URL.")
    print_id = m.group(1)
    try:
        data = _gql(_PRINT_FILES_QUERY, {"id": print_id})
        time.sleep(delay)
        printer = data["print"]
        title = printer.get("name") or f"Printables {print_id}"
        files = [
            {"id": f["id"], "name": f.get("name") or f["id"], "kind": "3mf"}
            for f in (printer.get("otherFiles") or [])
            if f.get("name", "").lower().endswith(".3mf")
        ] + [
            {"id": f["id"], "name": f.get("name") or f["id"], "kind": "stl"}
            for f in (printer.get("stls") or [])
            if f.get("name", "").lower().endswith((".stl", ".3mf"))
        ]
    except Exception as exc:
        raise FetchError(f"Printables file list failed ({exc}).") from exc
    if not files:
        raise FetchError("No downloadable 3MF/STL files found on that Printables model.")
    return title, files


def download_printables(url: str, job_dir: Path, file_id: str | None = None,
                        delay: float = 2.0) -> tuple[Path, str]:
    """Download the best file (3MF preferred, else STL) from a Printables URL.

    When file_id is given (from list_printables_files), that exact file is
    downloaded instead of the first candidate.
    """
    m = PRINTABLES_MODEL_RE.search(url)
    if not m:
        raise FetchError("That doesn't look like a Printables model URL.")
    print_id = m.group(1)
    try:
        data = _gql(_PRINT_FILES_QUERY, {"id": print_id})
        time.sleep(delay)
        printer = data["print"]
        title = printer.get("name") or f"Printables {print_id}"
        candidates = [
            (f, "3mf") for f in (printer.get("otherFiles") or []) if f.get("name", "").lower().endswith(".3mf")
        ] + [
            (f, "stl") for f in (printer.get("stls") or []) if f.get("name", "").lower().endswith((".stl", ".3mf"))
        ]
        if not candidates:
            raise FetchError("No downloadable 3MF/STL files found on that Printables model.")
        if file_id is not None:
            match = next((c for c in candidates if str(c[0]["id"]) == str(file_id)), None)
            if match is None:
                raise FetchError("The selected file is no longer on that Printables model.")
            file_info, kind = match
        else:
            file_info, kind = candidates[0]
        ext = ".3mf" if file_info["name"].lower().endswith(".3mf") else ".stl"
        # DownloadFileTypeEnum has no 3MF value (only stl/sla/gcode/...); the
        # site serves 3MFs through the "stl" file type, keyed by file id.
        file_type = "stl"
        dl = _gql(
            _DOWNLOAD_MUTATION,
            {"id": file_info["id"], "printId": print_id, "fileType": file_type, "source": "model_detail"},
        )
        time.sleep(delay)
        link = dl["getDownloadLink"]["output"]["link"]
        dest = job_dir / ("model.3mf" if ext == ".3mf" else "model.stl")
        _download_stream(link, dest)
    except Exception as exc:
        raise FetchError(f"Printables download failed ({exc}).") from exc
    return dest, title


def _makerworld_ids(url: str) -> tuple[str | None, str | None]:
    """(design_id, instance_id) parsed from a MakerWorld model URL.

    The instance id comes from the #profileId-{id} fragment (or a profileId
    query param); it is the *instance* id, which is what the f3mf download
    endpoint expects.
    """
    m = MAKERWORLD_MODEL_RE.search(url)
    design_id = m.group(1) if m else None
    parsed = urlparse(url)
    pm = MAKERWORLD_INSTANCE_RE.search(parsed.fragment + "&" + parsed.query)
    instance_id = pm.group(1) if pm else None
    return design_id, instance_id


def _makerworld_design_meta(design_id: str, timeout: float = 20.0) -> dict | None:
    """Anonymous design metadata (title + default instance id).

    Uses the Bambu Cloud design-service endpoint, which answers without auth
    for public designs. Returns None on any failure.
    """
    try:
        resp = httpx.get(
            BAMBULAB_DESIGN_API.format(design_id=design_id),
            headers={"User-Agent": _MAKERWORLD_UA},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception as exc:
        log.info("MakerWorld design metadata lookup failed: %s", exc)
        return None


def _makerworld_f3mf_download(instance_id: str, job_dir: Path,
                              session_cookie: str = "") -> Path:
    """Download a profile 3MF via the design-service f3mf endpoint.

    Returns the saved model.3mf path. Raises NeedsManualUpload when MakerWorld
    demands a login (no/expired session cookie) or returns no usable file,
    and lets other exceptions propagate so the caller can fall back to the
    legacy page scrape.
    """
    headers = {"User-Agent": _MAKERWORLD_UA}
    if session_cookie:
        headers["Cookie"] = session_cookie
    resp = httpx.get(
        MAKERWORLD_F3MF_API.format(instance_id=instance_id),
        params={"type": "download"},
        headers=headers,
        timeout=30.0,
        follow_redirects=True,
    )
    if resp.status_code in (401, 403):
        raise NeedsManualUpload(MAKERWORLD_LOGIN_HINT)
    resp.raise_for_status()
    content_type = resp.headers.get("content-type", "")
    body = resp.content
    if "application/json" in content_type or body[:1] == b"{":
        data = resp.json()
        if not isinstance(data, dict):
            raise NeedsManualUpload()
        if data.get("url"):
            dest = job_dir / "model.3mf"
            _download_stream(str(data["url"]), dest)
            return dest
        err = str(data.get("error") or data.get("error_msg") or "")
        if "log in" in err.lower() or "login" in err.lower():
            raise NeedsManualUpload(MAKERWORLD_LOGIN_HINT)
        raise NeedsManualUpload()
    if body[:2] == b"PK":
        dest = job_dir / "model.3mf"
        dest.write_bytes(body)
        return dest
    raise NeedsManualUpload()


def download_makerworld(url: str, job_dir: Path, session_cookie: str = "", delay: float = 3.0) -> tuple[Path, str]:
    """Best-effort MakerWorld download. Raises NeedsManualUpload on failure.

    Primary path is the design-service f3mf API (needs a logged-in session
    cookie for the actual download; the design metadata lookup is anonymous).
    Falls back to the legacy model-page scrape, then gives up and asks for a
    manual upload. Never bypasses login walls: a login-required response is
    reported as such instead of retried.
    """
    time.sleep(delay)
    design_id, instance_id = _makerworld_ids(url)
    title = "MakerWorld model"
    if design_id:
        meta = _makerworld_design_meta(design_id)
        if meta:
            title = str(meta.get("title") or title)[:120]
            if not instance_id and meta.get("defaultInstanceId"):
                instance_id = str(meta["defaultInstanceId"])
    if instance_id:
        try:
            dest = _makerworld_f3mf_download(instance_id, job_dir, session_cookie)
            return dest, title
        except NeedsManualUpload:
            raise
        except Exception as exc:
            log.info("MakerWorld f3mf download failed (%s); trying page scrape", exc)
    return _download_makerworld_legacy(url, job_dir, session_cookie, title)


def _download_makerworld_legacy(url: str, job_dir: Path, session_cookie: str = "",
                                title: str = "MakerWorld model") -> tuple[Path, str]:
    """Legacy fallback: regex the model page HTML for download links.

    Modern MakerWorld pages are a Next.js shell with no download hrefs, so
    this rarely succeeds; it is kept for older/simple pages.
    """
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
    if session_cookie:
        headers["Cookie"] = session_cookie
    try:
        resp = httpx.get(url, headers=headers, timeout=25.0, follow_redirects=True)
        if resp.status_code in (401, 403):
            raise NeedsManualUpload()
        html = resp.text
        if len(html) < 5000 or "Just a moment" in html or "cf-challenge" in html:
            # Cloudflare challenge page -- not something we may bypass.
            raise NeedsManualUpload()
        title_m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
        title = (title_m.group(1).strip()[:120] if title_m else title)
        links = re.findall(r'''(?:href|src)="([^"]*download[^"]*)"''', html, re.I)
        # Prefer direct 3MF links; drop API-ish login-gated endpoints we can't use.
        links = [l for l in links if not re.search(r"login|signin|captcha", l, re.I)]
        if not links:
            raise NeedsManualUpload()
        from urllib.parse import urljoin

        link = urljoin(url, links[0])
        dest = job_dir / ("model.3mf" if ".3mf" in link.lower() else "model.stl")
        _download_stream(link, dest)
    except NeedsManualUpload:
        raise
    except Exception as exc:  # any failure -> manual upload path
        log.info("MakerWorld best-effort failed: %s", exc)
        raise NeedsManualUpload() from exc
    return dest, title


def download_direct(url: str, job_dir: Path) -> tuple[Path, str]:
    """Direct file download with sanity checks (extension or magic bytes)."""
    parsed = urlparse(url)
    name = Path(parsed.path).name or "model"
    ext = Path(name).suffix.lower()
    try:
        dest = job_dir / ("model.3mf" if ext == ".3mf" else "model.stl" if ext == ".stl" else "model.bin")
        _download_stream(url, dest)
    except Exception as exc:
        raise FetchError(f"Download failed ({exc}).") from exc
    if dest.stat().st_size == 0:
        raise FetchError("Downloaded file is empty.")
    with dest.open("rb") as fh:
        magic = fh.read(4)
    is_zip = magic[:2] == b"PK"
    if ext == ".3mf" or (ext not in (".stl",) and is_zip):
        # Validate it is really a 3MF (zip containing [Content_Types].xml)
        try:
            with zipfile.ZipFile(dest) as zf:
                if "[Content_Types].xml" not in zf.namelist():
                    raise FetchError("That file isn't a valid 3MF archive.")
        except zipfile.BadZipFile as exc:
            raise FetchError("That file isn't a valid 3MF archive.") from exc
        final = job_dir / "model.3mf"
        dest.rename(final)
        return final, Path(name).stem or "Model"
    if ext == ".stl" or magic[:5].lower() == b"solid":
        final = job_dir / "model.stl"
        dest.rename(final)
        return final, Path(name).stem or "Model"
    dest.unlink(missing_ok=True)
    raise FetchError("That URL doesn't look like a 3MF or STL file.")


def detect_source_type(url: str) -> str:
    if PRINTABLES_MODEL_RE.search(url):
        return "printables"
    if MAKERWORLD_RE.search(url):
        return "makerworld"
    return "other"


def download_model(url: str, job_dir: Path, session_cookie: str = "",
                   file_id: str | None = None) -> tuple[Path, str, str]:
    """Download from any supported source. Returns (path, title, source_type)."""
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    source_type = detect_source_type(url)
    if source_type == "printables":
        path, title = download_printables(url, job_dir, file_id=file_id)
    elif source_type == "makerworld":
        path, title = download_makerworld(url, job_dir, session_cookie)
    else:
        path, title = download_direct(url, job_dir)
    return path, title, source_type
