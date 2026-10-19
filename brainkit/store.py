"""Storage backends.

Two implementations of one small interface:
- LocalStore: a git working tree on disk (CLI, GitHub Actions, coding agents).
- GitHubStore: the GitHub REST API (the hosted gateway; no local clone).

Events are only ever *created* (`create` fails if the path exists), which is
what makes concurrent writers safe: unique paths never conflict.
"""

from __future__ import annotations

import base64
import os
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path

import httpx

try:  # POSIX
    import fcntl

    def _lock_file(fh) -> None:
        fcntl.flock(fh, fcntl.LOCK_EX)

    def _unlock_file(fh) -> None:
        fcntl.flock(fh, fcntl.LOCK_UN)
except ImportError:  # Windows
    import msvcrt

    def _lock_file(fh) -> None:
        fh.seek(0)
        while True:
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:
                time.sleep(0.05)

    def _unlock_file(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)


# Commits made by the hosted gateway carry this identity. CI skips re-validating a push
# only when its single commit has this author (the gateway validated it already).
GATEWAY_COMMITTER = os.environ.get("BRAIN_GATEWAY_COMMITTER",
                                   "brain-gateway <brain-gateway@users.noreply.github.com>")


class StoreError(RuntimeError):
    pass


class Store(ABC):
    @abstractmethod
    def read(self, path: str) -> str | None: ...

    @abstractmethod
    def create(self, path: str, content: str, message: str) -> None:
        """Create a new file. Raise FileExistsError if it exists."""

    @abstractmethod
    def put(self, path: str, content: str, message: str) -> None:
        """Create or overwrite (generated files and state only)."""

    @abstractmethod
    def list(self, prefix: str, fresh: bool = False) -> list[str]:
        """All file paths under prefix (recursive), '/'-separated."""

    def exists(self, path: str) -> bool:
        return self.read(path) is not None


def _safe_rel(path: str) -> str:
    p = path.replace("\\", "/").lstrip("/")
    parts = p.split("/")
    if any(x in ("", ".", "..") for x in parts) or p.startswith(".git"):
        raise StoreError(f"unsafe path {path!r}")
    return p


class LocalStore(Store):
    def __init__(self, root: str | os.PathLike[str], commit: bool = False, author: str | None = None):
        self.root = Path(root).resolve()
        self.commit = commit
        self.author = author or "agent-brain <brain@localhost>"
        self._tlock = threading.Lock()

    def _abs(self, path: str) -> Path:
        p = (self.root / _safe_rel(path)).resolve()
        if self.root not in p.parents and p != self.root:
            raise StoreError(f"path escapes root: {path}")
        return p

    @contextmanager
    def _lock(self):
        lock_dir = self.root / ".git" if (self.root / ".git").is_dir() else self.root
        with self._tlock, open(lock_dir / ".brain.lock", "a+") as fh:
            _lock_file(fh)
            try:
                yield
            finally:
                _unlock_file(fh)

    def read(self, path: str) -> str | None:
        p = self._abs(path)
        return p.read_text(encoding="utf-8") if p.is_file() else None

    def _git_commit(self, paths: list[str], message: str) -> None:
        if not self.commit:
            return
        env = {**os.environ}
        name, _, email = self.author.partition("<")
        env.update(
            GIT_AUTHOR_NAME=name.strip(), GIT_COMMITTER_NAME=name.strip(),
            GIT_AUTHOR_EMAIL=email.rstrip(">").strip(), GIT_COMMITTER_EMAIL=email.rstrip(">").strip(),
        )
        run = lambda *a: subprocess.run(["git", "-C", str(self.root), *a], check=True, capture_output=True, env=env)
        run("add", "--", *paths)
        diff = subprocess.run(["git", "-C", str(self.root), "diff", "--cached", "--quiet"], env=env)
        if diff.returncode != 0:
            run("commit", "-q", "-m", message, "--", *paths)

    def create(self, path: str, content: str, message: str) -> None:
        p = self._abs(path)
        with self._lock():
            p.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
            self._git_commit([_safe_rel(path)], message)

    def put(self, path: str, content: str, message: str) -> None:
        self.put_many({path: content}, message)

    def put_many(self, files: dict[str, str | None], message: str) -> list[str]:
        """Write/delete several files in one commit. Returns changed paths."""
        changed: list[str] = []
        with self._lock():
            for path, content in files.items():
                p = self._abs(path)
                if content is None:
                    if p.exists():
                        p.unlink()
                        changed.append(_safe_rel(path))
                    continue
                if p.is_file() and p.read_text(encoding="utf-8") == content:
                    continue
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.with_suffix(p.suffix + ".tmp")
                tmp.write_text(content, encoding="utf-8", newline="\n")
                tmp.replace(p)
                changed.append(_safe_rel(path))
            if changed:
                self._git_commit(changed, message)
        return changed

    def list(self, prefix: str, fresh: bool = False) -> list[str]:
        base = self.root / _safe_rel(prefix) if prefix.strip("/") else self.root
        if not base.exists():
            return []
        out = []
        for p in base.rglob("*"):
            if p.is_file() and ".git" not in p.relative_to(self.root).parts:
                out.append(p.relative_to(self.root).as_posix())
        return sorted(out)


class GitHubStore(Store):
    """GitHub Contents/Trees API. Each create = one commit on `branch`."""

    def __init__(self, repo: str, token: str, branch: str = "main",
                 api: str = "https://api.github.com", client: httpx.Client | None = None,
                 tree_ttl: float = 20.0, committer: str | None = None):
        if "/" not in repo:
            raise StoreError("repo must be 'owner/name'")
        self.repo, self.branch, self.api = repo, branch, api.rstrip("/")
        self.client = client or httpx.Client(timeout=30.0)
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agent-brain",
        }
        name, _, email = (committer or GATEWAY_COMMITTER).partition("<")
        self.committer = {"name": name.strip(), "email": email.rstrip(">").strip()}
        self._tree: tuple[float, list[str]] | None = None
        self._tree_ttl = tree_ttl
        self._cache: dict[str, tuple[float, str | None]] = {}
        self._cache_ttl = 10.0

    def _url(self, path: str) -> str:
        return f"{self.api}/repos/{self.repo}/contents/{_safe_rel(path)}"

    def _req(self, method: str, url: str, **kw) -> httpx.Response:
        for attempt in range(5):
            r = self.client.request(method, url, headers=self.headers, **kw)
            if r.status_code in (500, 502, 503, 504) or (
                r.status_code in (403, 429) and ("rate limit" in r.text.lower() or "secondary" in r.text.lower())
            ):
                time.sleep(min(2 ** attempt, 20))
                continue
            return r
        return r

    def read(self, path: str) -> str | None:
        now = time.monotonic()
        hit = self._cache.get(path)
        if hit and now - hit[0] < self._cache_ttl:
            return hit[1]
        r = self._req("GET", self._url(path), params={"ref": self.branch})
        if r.status_code == 404:
            self._cache[path] = (now, None)
            return None
        if r.status_code != 200:
            raise StoreError(f"GitHub read {path}: {r.status_code} {r.text[:200]}")
        data = r.json()
        if isinstance(data, list):
            raise StoreError(f"{path} is a directory")
        if data.get("encoding") == "base64" and data.get("content") is not None:
            text = base64.b64decode(data["content"]).decode("utf-8")
        else:  # >1 MB files: fetch raw
            raw = self.client.get(self._url(path), params={"ref": self.branch},
                                  headers={**self.headers, "Accept": "application/vnd.github.raw+json"})
            raw.raise_for_status()
            text = raw.text
        self._cache[path] = (now, text)
        return text

    def _sha(self, path: str) -> str | None:
        r = self._req("GET", self._url(path), params={"ref": self.branch})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("sha")

    def _write(self, path: str, content: str, message: str, sha: str | None) -> httpx.Response:
        body = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": self.branch,
        }
        if sha:
            body["sha"] = sha
        body["committer"] = body["author"] = self.committer
        return self._req("PUT", self._url(path), json=body)

    def create(self, path: str, content: str, message: str) -> None:
        for attempt in range(6):
            r = self._write(path, content, message, sha=None)
            if r.status_code == 201:
                self._invalidate(path, content)
                return
            if r.status_code == 422 and "sha" in r.text.lower():
                raise FileExistsError(path)
            if r.status_code == 409:  # branch moved under us; retry
                self._tree = None
                time.sleep(0.3 * (attempt + 1))
                continue
            raise StoreError(f"GitHub create {path}: {r.status_code} {r.text[:300]}")
        raise StoreError(f"GitHub create {path}: gave up after repeated 409 conflicts")

    def put(self, path: str, content: str, message: str) -> None:
        for attempt in range(6):
            r = self._write(path, content, message, sha=self._sha(path))
            if r.status_code in (200, 201):
                self._invalidate(path, content)
                return
            if r.status_code in (409, 422):
                time.sleep(0.3 * (attempt + 1))
                continue
            raise StoreError(f"GitHub put {path}: {r.status_code} {r.text[:300]}")
        raise StoreError(f"GitHub put {path}: gave up")

    def _invalidate(self, path: str, content: str) -> None:
        self._cache[path] = (time.monotonic(), content)
        if self._tree is not None:
            self._tree = (self._tree[0], sorted(set(self._tree[1]) | {_safe_rel(path)}))

    def list(self, prefix: str, fresh: bool = False) -> list[str]:
        now = time.monotonic()
        if fresh or self._tree is None or now - self._tree[0] > self._tree_ttl:
            r = self._req("GET", f"{self.api}/repos/{self.repo}/git/trees/{self.branch}", params={"recursive": "1"})
            if r.status_code != 200:
                raise StoreError(f"GitHub tree: {r.status_code} {r.text[:200]}")
            data = r.json()
            if data.get("truncated"):
                raise StoreError("GitHub tree listing truncated (>100k entries); archive old events")
            self._tree = (now, sorted(t["path"] for t in data.get("tree", []) if t.get("type") == "blob"))
        pre = prefix.strip("/")
        if not pre:
            return list(self._tree[1])
        return [p for p in self._tree[1] if p == pre or p.startswith(pre + "/")]


def store_from_env() -> Store:
    kind = os.environ.get("BRAIN_STORE", "local").lower()
    if kind == "github":
        repo = os.environ.get("BRAIN_GITHUB_REPO")
        token = os.environ.get("BRAIN_GITHUB_TOKEN")
        if not repo or not token:
            raise StoreError("BRAIN_STORE=github needs BRAIN_GITHUB_REPO and BRAIN_GITHUB_TOKEN")
        return GitHubStore(repo, token, os.environ.get("BRAIN_GITHUB_BRANCH", "main"),
                           os.environ.get("BRAIN_GITHUB_API", "https://api.github.com"))
    root = os.environ.get("BRAIN_ROOT") or _find_root(Path.cwd())
    commit = os.environ.get("BRAIN_GIT_COMMIT", "1") not in ("0", "false", "no")
    return LocalStore(root, commit=commit, author=os.environ.get("BRAIN_GIT_AUTHOR"))


def _find_root(start: Path) -> str:
    for p in [start, *start.parents]:
        if (p / "_registry" / "customers.yaml").is_file():
            return str(p)
    raise StoreError("not inside a brain repo (no _registry/customers.yaml found); set BRAIN_ROOT")
