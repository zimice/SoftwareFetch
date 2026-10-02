#!/usr/bin/env python3
"""Fetch Windows installers from config.json. Python 3.9+; requests.

This tool DOWNLOADS files only. It does not execute downloaded installers.
Exit status: 0 = all successful, 1 = downloads failed, 2 = invalid input.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOG = logging.getLogger("software-fetch")
# For a PyInstaller build, use a config next to the EXE if present; otherwise
# load the bundled default. Outputs go next to the EXE (or --dest).
ROOT = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
BUNDLED = Path(getattr(sys, "_MEIPASS", ROOT))
DEFAULT_CONFIG = ROOT / "config.json" if (ROOT / "config.json").exists() else BUNDLED / "config.json"
SIGNATURES = {
    ".exe": (b"MZ",),
    ".msi": (bytes.fromhex("D0CF11E0A1B11AE1"),),
    ".zip": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    ".msix": (b"PK\x03\x04",),
    ".7z": (bytes.fromhex("377ABCAF271C"),),
}
TIMEOUT = (12, 75)
MIN_SIZE = 1024
RETRY_ATTEMPTS = 2


class IncompleteDownload(ValueError):
    """The transfer stopped before all bytes arrived."""


@dataclass(frozen=True)
class App:
    name: str
    extension: str
    source: dict
    folder: str = ""
    requires: tuple[str, ...] = ()


class Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)


def https_url(value):
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc) and not parsed.username and not parsed.password


def safe_name(name):
    result = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if not result or result.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *{f"COM{i}" for i in range(1, 10)}, *{f"LPT{i}" for i in range(1, 10)}}:
        raise ValueError("invalid app name for a Windows filename")
    return result


def load_config(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("config must be a JSON object")
    workers = data.get("workers", 6)
    if type(workers) is not int or not 1 <= workers <= 32:
        raise ValueError("workers must be an integer from 1 to 32")
    dest_value = data.get("destination", "install")
    if not isinstance(dest_value, str) or not dest_value.strip():
        raise ValueError("destination must be a nonempty path")
    dest = Path(dest_value).expanduser()
    if not dest.is_absolute():
        dest = ROOT / dest  # predictable, even if config is bundled inside an EXE
    entries = data.get("apps")
    if not isinstance(entries, list) or not entries:
        raise ValueError("apps must be a nonempty list")
    apps, names, filenames = [], set(), set()
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise ValueError(f"apps[{index}] must be an object")
        name, ext, source = entry.get("name"), entry.get("extension"), entry.get("source")
        enabled = entry.get("enabled", True)
        if not isinstance(name, str) or not name.strip() or type(enabled) is not bool:
            raise ValueError(f"apps[{index}] needs a name and a boolean enabled field")
        if not isinstance(source, dict):
            raise ValueError(f"{name}: source must be an object")
        kind = source.get("type")
        if ext not in SIGNATURES and not (kind == "manual" and ext == ".url"):
            raise ValueError(f"{name}: invalid extension {ext}")
        folder = entry.get("folder", "")
        if not isinstance(folder, str) or (folder and (folder in (".", "..") or safe_name(folder) != folder or "/" in folder or "\\" in folder)):
            raise ValueError(f"{name}: folder must be a single safe directory name")
        requires = entry.get("requires", [])
        if not isinstance(requires, list) or not all(isinstance(r, str) and r for r in requires):
            raise ValueError(f"{name}: requires must be a list of app names")
        filename = (folder + "/" + safe_name(name) + ext).casefold()
        if name.casefold() in names or filename in filenames:
            raise ValueError(f"duplicate app name or output file: {name}")
        names.add(name.casefold())
        filenames.add(filename)
        if kind in ("direct", "manual"):
            if not https_url(source.get("url")):
                raise ValueError(f"{name}: {kind} source needs an HTTPS URL")
        elif kind == "page":
            pages = source.get("pages")
            if not isinstance(pages, list) or not pages or not all(https_url(p) for p in pages):
                raise ValueError(f"{name}: pages must contain HTTPS URLs")
            validate_pattern(source.get("filename_regex"), name)
        elif kind == "github_release":
            repo = source.get("repo")
            if not isinstance(repo, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
                raise ValueError(f"{name}: invalid GitHub owner/repo")
            validate_pattern(source.get("filename_regex"), name)
        else:
            raise ValueError(f"{name}: source type must be direct, page, github_release, or manual")
        if enabled:
            apps.append(App(name, ext, source, folder, tuple(requires)))
    enabled_names = {app.name.casefold() for app in apps}
    for app in apps:
        for requirement in app.requires:
            if requirement.casefold() not in enabled_names:
                raise ValueError(f"{app.name}: required app {requirement!r} is missing or disabled")
    return apps, dest, workers


def validate_pattern(pattern, name):
    try:
        if not isinstance(pattern, str):
            raise TypeError("expected string")
        re.compile(pattern)
    except (TypeError, re.error) as exc:
        raise ValueError(f"{name}: invalid filename_regex") from exc


def session_for_app():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SoftwareFetch/3.0",
        "Accept-Encoding": "identity",
    })
    retry = Retry(total=2, connect=2, read=2, status=2, backoff_factor=0.7,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(("GET",)), respect_retry_after_header=True)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    return session


def matching_links(html, page_url, pattern):
    parser = Links()
    parser.feed(html)
    matches = []
    for href in parser.links:
        candidate = urljoin(page_url, href.strip())
        # A /download suffix is common on SourceForge and WinSCP pages.
        segments = [unquote(part) for part in urlparse(candidate).path.split("/") if part]
        if https_url(candidate) and any(pattern.fullmatch(part) for part in segments):
            matches.append(candidate)
    return matches


def resolve_url(session, app):
    source, kind = app.source, app.source["type"]
    if kind in ("direct", "manual"):
        return source["url"]
    pattern = re.compile(source["filename_regex"], re.IGNORECASE)
    if kind == "github_release":
        api = f"https://api.github.com/repos/{source['repo']}/releases/latest"
        with session.get(api, timeout=TIMEOUT, headers={"Accept": "application/vnd.github+json"}) as response:
            response.raise_for_status()
            data = response.json()
        for asset in data.get("assets", []):
            if pattern.fullmatch(asset.get("name", "")) and https_url(asset.get("browser_download_url")):
                return asset["browser_download_url"]
        raise ValueError(f"no matching stable release asset on {api}")
    errors = []
    for page in source["pages"]:
        try:
            with session.get(page, timeout=TIMEOUT) as response:
                response.raise_for_status()
                if "html" not in response.headers.get("Content-Type", "text/html").lower():
                    raise ValueError("download listing is not HTML")
                candidates = matching_links(response.text, response.url, pattern)
                if candidates:
                    return candidates[0]  # vendor normally lists the current stable first
                raise ValueError("no matching installer link found")
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{page}: {exc}")
    raise RuntimeError("; ".join(errors))


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_response(response, output, ext, force):
    length = response.headers.get("Content-Length")
    if length is not None:
        try:
            length = int(length)
        except ValueError as exc:
            raise ValueError("invalid Content-Length header") from exc
        if length < MIN_SIZE:
            raise ValueError(f"server announced only {length} bytes")
    ctype = response.headers.get("Content-Type", "").lower()
    if any(t in ctype for t in ("text/html", "application/json", "text/plain", "application/xml")):
        raise ValueError(f"server returned {ctype} instead of an installer")
    if not https_url(response.url):
        raise ValueError("server redirected to a non-HTTPS URL")
    temp = output.with_name(output.name + ".part")
    digest, size, prefix = hashlib.sha256(), 0, bytearray()
    magic_size = max(map(len, SIGNATURES[ext]))
    try:
        with temp.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                if len(prefix) < magic_size:
                    prefix.extend(chunk[:magic_size - len(prefix)])
                    if len(prefix) == magic_size and not any(prefix.startswith(sig) for sig in SIGNATURES[ext]):
                        raise ValueError(f"response does not have a {ext} file signature")
                handle.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        if size < MIN_SIZE or (length is not None and size != length):
            raise IncompleteDownload(f"incomplete file: received {size} bytes, expected {length or 'at least 1024'}")
        if len(prefix) < magic_size or not any(prefix.startswith(sig) for sig in SIGNATURES[ext]):
            raise ValueError(f"response does not have a {ext} file signature")
        if not force and output.is_file() and output.stat().st_size == size and file_hash(output) == digest.hexdigest():
            return size, False, digest.hexdigest()
        os.replace(temp, output)
        return size, True, digest.hexdigest()
    finally:
        temp.unlink(missing_ok=True)


def fetch_app(app, dest, force):
    target_dir = dest / app.folder
    target_dir.mkdir(parents=True, exist_ok=True)
    output = target_dir / (safe_name(app.name) + app.extension)
    if app.source["type"] == "manual":
        # A .url is an explicit link, not an installer. Office is account/licence-dependent.
        output.write_text("[InternetShortcut]\nURL=" + app.source["url"] + "\n", encoding="utf-8")
        LOG.info("%s: saved official download-page shortcut %s (manual installation)", app.name, output)
        return {"name": app.name, "path": output, "sha256": file_hash(output), "url": app.source["url"], "manual": True}
    with session_for_app() as session:
        url = resolve_url(session, app)
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                with session.get(url, stream=True, timeout=TIMEOUT) as response:
                    response.raise_for_status()
                    # Some vendor 'download' links first show a second landing page
                    # (WinSCP); follow its direct CDN file link when present.
                    if "text/html" in response.headers.get("Content-Type", "").lower() and app.source["type"] == "page":
                        pattern = re.compile(app.source["filename_regex"], re.IGNORECASE)
                        candidates = matching_links(response.text, response.url, pattern)
                        if not candidates:
                            raise ValueError("intermediate landing page has no installer link")
                        with session.get(candidates[0], stream=True, timeout=TIMEOUT) as binary:
                            binary.raise_for_status()
                            size, updated, sha256 = copy_response(binary, output, app.extension, force)
                            final_url = binary.url
                    else:
                        size, updated, sha256 = copy_response(response, output, app.extension, force)
                        final_url = response.url
                    LOG.info("%s: %s %s (%.1f MiB)", app.name, "saved" if updated else "unchanged", output, size / 1048576)
                    return {"name": app.name, "path": output, "sha256": sha256, "url": final_url, "manual": False}
            except (requests.RequestException, IncompleteDownload) as exc:
                if attempt == RETRY_ATTEMPTS:
                    raise RuntimeError(f"{exc} (source: {url})") from exc
                LOG.warning("%s: attempt %d failed: %s; retrying", app.name, attempt, exc)
                time.sleep(attempt)
            except (OSError, ValueError) as exc:
                raise RuntimeError(f"{exc} (source: {url})") from exc
    raise RuntimeError("download exhausted retries")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Download Windows installers (does not install apps)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--dest", type=Path, help="Override output folder from config.json")
    parser.add_argument("--workers", type=int, help="Parallel downloads (1-32)")
    parser.add_argument("--apps", nargs="+", metavar="NAME", help="Only these apps; dependencies included")
    parser.add_argument("--force", action="store_true", help="Replace downloaded files even when identical")
    parser.add_argument("--list", action="store_true", help="List enabled apps")
    parser.add_argument("--verify", action="store_true", help="Validate already-downloaded files against SHA256SUMS.txt")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def select_apps(apps, requested):
    by_name = {app.name.casefold(): app for app in apps}
    selected, visiting = {}, set()

    def include(name):
        key = name.casefold()
        if key in visiting:
            raise ValueError(f"circular app dependency involving {name}")
        if key in selected:
            return
        if key not in by_name:
            raise ValueError(f"unknown/disabled app: {name}")
        visiting.add(key)
        app = by_name[key]
        for requirement in app.requires:
            include(requirement)
        visiting.remove(key)
        selected[key] = app

    for name in requested:
        include(name)
    return list(selected.values())


def write_hashes(dest, results):
    """Write a checksums file containing hashes of successful current-run downloads."""
    lines = []
    for result in sorted(results, key=lambda item: str(item["path"]).lower()):
        rel = result["path"].relative_to(dest).as_posix()
        lines.append(f"{result['sha256']}  {rel}\n")
    (dest / "SHA256SUMS.txt").write_text("".join(lines), encoding="utf-8")


def verify_files(apps, dest):
    hashes = dest / "SHA256SUMS.txt"
    if not hashes.is_file():
        LOG.error("Missing %s; run a download first", hashes)
        return 1
    recorded = {}
    for line in hashes.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"([0-9a-fA-F]{64})  (.+)", line)
        if not match:
            LOG.error("Invalid checksums entry: %s", line)
            return 1
        recorded[match.group(2)] = match.group(1).lower()
    failures = 0
    for app in apps:
        rel = (Path(app.folder) / (safe_name(app.name) + app.extension)).as_posix()
        target = dest / rel
        expected = recorded.get(rel)
        if not target.is_file() or not expected or file_hash(target) != expected:
            LOG.error("MISSING or CHANGED: %s", rel)
            failures += 1
        else:
            LOG.info("OK: %s", rel)
    LOG.info("Verification: %d OK, %d failed", len(apps) - failures, failures)
    return 1 if failures else 0


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    try:
        apps, dest, workers = load_config(args.config)
        workers = args.workers if args.workers is not None else workers
        if type(workers) is not int or not 1 <= workers <= 32:
            raise ValueError("--workers must be between 1 and 32")
        if args.list:
            for app in apps:
                print(app.name + (" [manual]" if app.source["type"] == "manual" else ""))
            return 0
        apps = select_apps(apps, args.apps if args.apps else [app.name for app in apps])
        if args.dest is not None:
            dest = args.dest.expanduser()
        dest.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError) as exc:
        LOG.error("Configuration error: %s", exc)
        return 2
    if args.verify:
        return verify_files(apps, dest)
    LOG.info("Fetching %d entries into %s with %d workers", len(apps), dest.resolve(), workers)
    failures, results = 0, []
    with ThreadPoolExecutor(max_workers=min(workers, len(apps))) as pool:
        tasks = {pool.submit(fetch_app, app, dest, args.force): app for app in apps}
        for future in as_completed(tasks):
            try:
                results.append(future.result())
            except Exception as exc:
                failures += 1
                LOG.error("%s: FAILED: %s", tasks[future].name, exc)
    if results:
        write_hashes(dest, results)
    manual = sum(r["manual"] for r in results)
    LOG.info("Finished: %d downloaded, %d manual shortcuts, %d failed", len(results) - manual, manual, failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
