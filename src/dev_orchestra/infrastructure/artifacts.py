"""Host-only, bounded public downloads pinned by dependency checksums."""

import base64
import hashlib
import io
import re
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dev_orchestra.domain.models import PipelineError, StopRequested

HOSTS = {
    "registry.npmjs.org",
    "registry.yarnpkg.com",
    "repo.maven.apache.org",
    "codeload.github.com",
}


class DownloadBudget:
    def __init__(self, limit=2 * 2**30):
        self.limit, self.size = limit, 0
        self.lock = threading.Lock()

    def consume(self, size):
        with self.lock:
            self.size += size
            if self.size > self.limit:
                raise PipelineError("dependency downloads exceed total safety limit")


def public_url(url):
    value = urllib.parse.urlsplit(url)
    if (
        value.scheme != "https"
        or value.hostname not in HOSTS
        or value.port not in {None, 443}
        or value.username
        or value.password
        or value.query
        or value.fragment
        or (
            value.hostname == "codeload.github.com"
            and not re.fullmatch(
                r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/tar\.gz/[0-9a-f]{40}", value.path
            )
        )
    ):
        raise PipelineError(
            "automatic preparation requires a checksum-pinned public HTTPS artifact"
        )
    return url


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(newurl)
        if (
            req
            and urllib.parse.urlsplit(req.full_url).hostname == "codeload.github.com"
            and urllib.parse.urlsplit(newurl).hostname != "codeload.github.com"
        ):
            raise PipelineError("GitHub source archives must remain on the official archive host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def check_stop(stop):
    if value := stop():
        raise StopRequested(value)


def fetch(url, stop=lambda: None, limit=128 * 2**20, budget=None):
    public_url(url)
    check_stop(stop)
    deadline = time.monotonic() + 120
    # Ignore user proxy/auth configuration; no repository code is executed online.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), PublicRedirect())
    try:
        with opener.open(url, timeout=15) as response:
            public_url(response.url)
            chunks, size = [], 0
            while chunk := response.read(65536):
                check_stop(stop)
                size += len(chunk)
                if size > limit or time.monotonic() > deadline:
                    raise PipelineError("public artifact exceeds download safety limit")
                if budget:
                    budget.consume(len(chunk))
                chunks.append(chunk)
        return b"".join(chunks)
    except (OSError, urllib.error.URLError) as error:
        raise PipelineError("public artifact download failed; retry preparation") from error


def checksum(integrity):
    # SRI requires the strongest supported algorithm when several are supplied.
    values = []
    for token in integrity.split():
        algorithm, _, encoded = token.partition("-")
        if algorithm not in {"sha512", "sha384", "sha256", "sha1"}:
            continue
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError:
            continue
        if len(raw) == hashlib.new(algorithm).digest_size:
            values.append((algorithm, raw))
    if not values:
        raise PipelineError("artifact checksum missing or unsupported")
    return max(values, key=lambda p: hashlib.new(p[0]).digest_size)


def verified(url, integrity, directory, stop=lambda: None, budget=None):
    public_url(url)
    algorithm, expected = checksum(integrity)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if any(p.is_symlink() for p in [directory, *directory.parents]):
        raise PipelineError("artifact cache symlinks denied")
    path = directory / (algorithm + "-" + expected.hex() + ".tgz")
    if path.is_symlink():
        raise PipelineError("artifact cache symlinks denied")
    if path.is_file() and path.stat().st_size <= 128 * 2**20:
        if hashlib.new(algorithm, path.read_bytes()).digest() == expected:
            check_stop(stop)
            return path
    data = fetch(url, stop, 128 * 2**20, budget)
    if hashlib.new(algorithm, data).digest() != expected:
        raise PipelineError("public artifact checksum mismatch")
    from tempfile import NamedTemporaryFile

    with NamedTemporaryFile(dir=directory, delete=False) as file:
        temporary = Path(file.name)
        file.write(data)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def yarn_entries(text):
    """Read Yarn v1's literal resolved/integrity fields, without evaluating YAML."""
    if len(text) > 16 * 2**20 or re.search(r"(?m)^__metadata:", text):
        raise PipelineError("automatic Yarn preparation supports Classic v1 lockfiles only")
    entries = []
    for block in re.split(r"(?m)^(?=\S)", text):
        resolved = re.search(r'^  resolved "([^"\n]+)"$', block, re.M)
        if not resolved:
            continue
        url, _, sha1 = resolved[1].partition("#")
        git = re.fullmatch(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\.git", url)
        archive = re.fullmatch(
            r"https://codeload\.github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/tar\.gz/([0-9a-f]{40})",
            url,
        )
        if git and re.fullmatch(r"[0-9a-f]{40}", sha1):
            entries.append(
                (
                    f"https://codeload.github.com/{git[1]}/{git[2]}/tar.gz/{sha1}",
                    "git-" + sha1,
                    git[2] + ".git-" + sha1,
                )
            )
            continue
        if archive and "integrity" not in block:
            entries.append((url, "githubtar-" + archive[3], archive[3]))
            continue
        public_url(url)
        match = re.search(r"^  integrity ([^\n]+)$", block, re.M)
        integrity = match[1].strip().strip('"') if match else ""
        if not integrity and re.fullmatch(r"[a-fA-F0-9]{40}", sha1):
            integrity = "sha1-" + base64.b64encode(bytes.fromhex(sha1)).decode()
        checksum(integrity)
        path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
        name = path.rsplit("/", 1)[-1]
        if path.startswith("/@"):
            name = path.split("/", 2)[1] + "-" + name
        if not re.fullmatch(r"@?[A-Za-z0-9_.+-]+\.tgz", name):
            raise PipelineError("unsupported Yarn mirror artifact name")
        entries.append((url, integrity, name))
    if not entries or len(entries) > 10000:
        raise PipelineError("Yarn lockfile has no usable entries or exceeds safety limit")
    return list(dict.fromkeys(entries))


def github_archive(url, integrity, directory, stop, budget=None):
    """Only exact GitHub commits, verified against the official git-archive metadata."""
    public_url(url)
    if not re.fullmatch(r"(?:git|githubtar)-[0-9a-f]{40}", integrity):
        raise PipelineError("GitHub dependency requires a full commit ID")
    commit = integrity.rsplit("-", 1)[1]
    if not url.endswith("/" + commit):
        raise PipelineError("GitHub archive commit mismatch")
    directory.mkdir(parents=True, exist_ok=True)
    if any(p.is_symlink() for p in [directory, *directory.parents]):
        raise PipelineError("artifact cache symlinks denied")
    original = directory / (hashlib.sha256(url.encode()).hexdigest() + ".github.tgz")
    if original.is_symlink():
        raise PipelineError("artifact cache symlinks denied")
    data = (
        original.read_bytes()
        if original.is_file() and original.stat().st_size <= 128 * 2**20
        else fetch(url, stop, 128 * 2**20, budget)
    )
    target = directory / (hashlib.sha256((url + integrity).encode()).hexdigest() + ".tgz")
    if target.is_symlink():
        raise PipelineError("artifact cache symlinks denied")
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as source:
            if source.pax_headers.get("comment") != commit:
                raise PipelineError("GitHub git-archive commit metadata mismatch")
            size, count = 0, 0
            output = io.BytesIO()
            # Yarn's GitFetcher consumes a raw git-archive stream (no gzip layer).
            with tarfile.open(
                fileobj=output, mode="w" if integrity.startswith("git-") else "w:gz"
            ) as destination:
                root = None
                for member in source:
                    check_stop(stop)
                    parts = Path(member.name).parts
                    size += member.size
                    count += 1
                    if (
                        not parts
                        or parts[0] == "/"
                        or ".." in parts
                        or "\\" in member.name
                        or not (member.isfile() or member.isdir())
                        or size > 256 * 2**20
                        or count > 20000
                    ):
                        raise PipelineError("unsafe GitHub dependency archive")
                    root = root or parts[0]
                    if parts[0] != root:
                        raise PipelineError("GitHub archive root mismatch")
                    if integrity.startswith("git-"):
                        if len(parts) == 1:
                            continue
                        member.name = "/".join(parts[1:])
                        member.pax_headers.pop("path", None)
                    destination.addfile(
                        member, source.extractfile(member) if member.isfile() else None
                    )
            target.write_bytes(output.getvalue())
    except (tarfile.TarError, OSError) as error:
        raise PipelineError("invalid GitHub dependency archive") from error
    original.write_bytes(data)
    return target
