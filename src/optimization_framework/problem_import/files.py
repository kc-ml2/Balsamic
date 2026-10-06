"""Import folders: uploaded documents as extracted text and a read-only copy of a code base.

Layout under ``<workspace>/imports/<id>/``: ``uploads/`` keeps the original files,
``visible/documents`` and ``visible/code`` are the only paths the importer agent can
read. Nothing here executes supplied code. Content is sent to the chosen model, so
likely credentials are skipped and never copied.
"""
from __future__ import annotations

import fnmatch
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

MAX_DOCUMENT_BYTES = 100 * 1024**2
MAX_DOCUMENTS = 20
MAX_ARCHIVE_BYTES = 2 * 1024**3             # one upload, streamed to disk
MAX_CODE_BYTES = 4 * 1024**3                # disk guard for one import's copy; later files are listed as not copied
MAX_CODE_FILES = 50000
LARGE_FILE_BYTES = 1024**2                  # flagged to the agent; usually data, logs or generated code
SEARCH_FILE_BYTES = 2 * 1024**2             # tree-wide searches skip larger files unless searched directly
COUNT_LINES_BYTES = 32 * 1024**2
MAX_READ_LINES = 400
# Imports allow larger papers than literature capture; the extractor stays a bounded subprocess.
PDF_LIMITS = {"max_bytes": MAX_DOCUMENT_BYTES, "memory_mb": 3072, "cpu_seconds": 180, "max_pages": 2000, "max_chars": 8_000_000}
DOCUMENT_TYPES = {".pdf", ".docx", ".md", ".markdown", ".txt", ".tex", ".rst", ".html", ".htm", ".csv", ".json", ".yaml", ".yml"}
SKIP_DIRECTORIES = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
                    ".pytest_cache", ".tox", ".ipynb_checkpoints", ".idea", ".vscode"}
SECRET_NAMES = (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*", "id_ecdsa*",
                ".netrc", ".pypirc", ".npmrc", "credentials*", "*secret*", "auth.json", "*.keystore", "*.kdbx")
CREDENTIAL_DIRECTORIES = {".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".password-store"}
SENSITIVE_HOME = (".ssh", ".gnupg", ".config", ".aws", ".azure", ".kube", ".docker", ".local/share/keyrings",
                  ".password-store", ".mozilla", ".thunderbird", ".claude", ".codex", ".pi")


class ImportError_(ValueError):
    """A rejected upload or source; the message is safe to show to the researcher."""


def safe_name(name: str) -> str:
    base = PurePosixPath(name.replace("\\", "/")).name
    cleaned = re.sub(r"[^A-Za-z0-9._ ()+-]", "_", base).strip(" .")
    if not cleaned:
        raise ImportError_("A file needs a name")
    return cleaned[:150]


def skipped(relative: PurePosixPath) -> str | None:
    """Why a path is left out of the agent's copy, or None to keep it."""
    if any(part in CREDENTIAL_DIRECTORIES for part in relative.parts[:-1]):
        return "credential directory"
    if any(part in SKIP_DIRECTORIES for part in relative.parts[:-1]):
        return "tooling or version-control directory"
    if any(fnmatch.fnmatch(relative.name.lower(), pattern) for pattern in SECRET_NAMES):
        return "possible credential file"
    return None


class Budget:
    def __init__(self):
        self.files, self.bytes, self.large, self.skipped, self.not_copied = 0, 0, 0, [], 0

    def admit(self, relative, size):
        reason = skipped(relative)
        if reason is None and (self.files + 1 > MAX_CODE_FILES or self.bytes + size > MAX_CODE_BYTES):
            reason = "import size limit reached (50,000 files or 4 GB)"
            self.not_copied += 1
        if reason is not None:
            if len(self.skipped) < 200:
                self.skipped.append({"path": str(relative), "reason": reason, "bytes": size})
            return False
        self.files += 1
        self.bytes += size
        self.large += size > LARGE_FILE_BYTES
        return True

    def summary(self):
        return {"files": self.files, "bytes": self.bytes, "large_files": self.large, "not_copied": self.not_copied,
                "skipped": self.skipped}


class ImportFolder:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.uploads = self.root / "uploads"
        self.visible = self.root / "visible"
        self.documents = self.visible / "documents"
        self.code = self.visible / "code"

    def create(self):
        for path in (self.uploads, self.documents, self.code):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self

    # Documents ---------------------------------------------------------------
    def add_document(self, name: str, raw: bytes) -> dict:
        name = safe_name(name)
        suffix = Path(name).suffix.lower()
        if suffix not in DOCUMENT_TYPES:
            raise ImportError_(f"{name}: supported documents are " + ", ".join(sorted(DOCUMENT_TYPES)))
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise ImportError_(f"{name} exceeds the 100 MB document limit")
        if sum(1 for _ in self.uploads.glob("document-*")) >= MAX_DOCUMENTS:
            raise ImportError_("An import accepts at most 20 documents")
        (self.uploads / ("document-" + name)).write_bytes(raw)
        text, pages, limitations = extract_text(name, raw)
        (self.documents / (name + ".txt")).write_text(text)
        return {"name": name, "bytes": len(raw), "text_file": "documents/" + name + ".txt", "pages": pages,
                "characters": len(text), "limitations": limitations}

    # Code --------------------------------------------------------------------
    def archive_upload(self, name: str) -> Path:
        """Where an archive upload is streamed before unpacking."""
        return self.uploads / ("code-" + safe_name(name))

    def add_archive(self, name: str, upload: Path) -> dict:
        name = safe_name(name)
        self._reset_code()
        budget = Budget()
        if zipfile.is_zipfile(upload):
            with zipfile.ZipFile(upload) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    if stat.S_ISLNK(info.external_attr >> 16):
                        budget.skipped.append({"path": info.filename, "reason": "symbolic link"})
                        continue
                    relative = member_path(info.filename)
                    if budget.admit(relative, info.file_size):
                        with archive.open(info) as source:
                            self._copy(relative, source)
        else:
            try:
                archive = tarfile.open(upload, mode="r:*")
            except tarfile.TarError:
                raise ImportError_("Upload a .zip or .tar(.gz/.bz2/.xz) archive") from None
            with archive:
                for info in archive:
                    if info.isdir():
                        continue
                    if not info.isfile():
                        budget.skipped.append({"path": info.name, "reason": "link or special file"})
                        continue
                    relative = member_path(info.name)
                    if budget.admit(relative, info.size):
                        self._copy(relative, archive.extractfile(info))
        return {"kind": "archive", "source": name, **budget.summary()}

    def add_git(self, url: str, ref: str = "") -> dict:
        check_git_url(url)
        if ref and not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", ref):
            raise ImportError_("A git reference may contain only letters, digits, '.', '_', '/' and '-'")
        self._reset_code()
        checkout = self.root / "git-checkout"
        shutil.rmtree(checkout, ignore_errors=True)
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1", "GIT_ASKPASS": "/bin/true"}
        safe = ["-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=never", "-c", "protocol.ext.allow=never",
                "-c", "submodule.recurse=false"]
        def git(*args, cwd=None):
            try:
                return subprocess.run(["git", *safe, *args], cwd=cwd, env=env, capture_output=True, text=True,
                                      timeout=300, check=True).stdout.strip()
            except subprocess.TimeoutExpired:
                raise ImportError_("The git fetch took longer than 5 minutes") from None
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or "").strip().splitlines()[-1:] or ["git failed"]
                raise ImportError_("git: " + detail[0][:300]) from None
        try:
            git("init", "--quiet", str(checkout))
            git("remote", "add", "origin", url, cwd=checkout)
            git("fetch", "--quiet", "--depth", "1", "--no-tags", "origin", ref or "HEAD", cwd=checkout)
            git("checkout", "--quiet", "--detach", "FETCH_HEAD", cwd=checkout)
            commit = git("rev-parse", "HEAD", cwd=checkout)
            summary = self._copy_tree(checkout)
        finally:
            shutil.rmtree(checkout, ignore_errors=True)
        return {"kind": "git", "source": url, "ref": ref or "HEAD", "commit": commit, **summary}

    def add_folder(self, path: str, home: Path | None = None) -> dict:
        source = Path(os.path.expanduser(path)).resolve()
        home = (home or Path.home()).resolve()
        if not source.is_dir():
            raise ImportError_("The folder does not exist on this machine")
        if not source.is_relative_to(home):
            raise ImportError_(f"Only folders under {home} can be imported")
        if source == home:
            raise ImportError_("Choose the project folder itself, not your home folder")
        if any(source.is_relative_to(home / sensitive) for sensitive in SENSITIVE_HOME):
            raise ImportError_("That folder holds account settings or credentials; choose a project folder")
        if self.root.resolve().is_relative_to(source):
            raise ImportError_("The folder contains the import workspace itself")
        self._reset_code()
        return {"kind": "folder", "source": str(source), **self._copy_tree(source)}

    def _copy_tree(self, source: Path) -> dict:
        budget = Budget()
        for directory, names, files in os.walk(source, followlinks=False):
            names[:] = sorted(name for name in names if name not in SKIP_DIRECTORIES | CREDENTIAL_DIRECTORIES)
            for name in sorted(files):
                path = Path(directory) / name
                relative = PurePosixPath(path.relative_to(source).as_posix())
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode):
                    budget.skipped.append({"path": str(relative), "reason": "link or special file"})
                    continue
                if budget.admit(relative, info.st_size):
                    with path.open("rb") as source:
                        self._copy(relative, source)
        return budget.summary()

    def _reset_code(self):
        shutil.rmtree(self.code, ignore_errors=True)
        self.code.mkdir(parents=True, mode=0o700)

    def _copy(self, relative: PurePosixPath, source):
        target = (self.code / relative).resolve()
        if not target.is_relative_to(self.code.resolve()):
            raise ImportError_("An archive entry escapes the import folder")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as handle:
            shutil.copyfileobj(source, handle, 1024**2)

    # Agent access --------------------------------------------------------------
    def resolve(self, path: str) -> Path:
        relative = PurePosixPath((path or "").strip().lstrip("/") or ".")
        if ".." in relative.parts:
            raise ValueError("Paths must stay inside the import folder")
        target = (self.visible / relative).resolve()
        if not target.is_relative_to(self.visible.resolve()):
            raise ValueError("Paths must stay inside the import folder")
        if not target.exists():
            raise ValueError(f"No such path: {relative}")
        return target

    def listing(self, path: str = "", depth: int = 2) -> dict:
        start = self.resolve(path)
        if start.is_file():
            return {"path": self._relative(start), "type": "file", **describe(start)}
        entries, truncated = [], False
        base_depth = len(start.relative_to(self.visible).parts)
        for directory, names, files in os.walk(start):
            names.sort()
            current = Path(directory)
            level = len(current.relative_to(self.visible).parts) - base_depth
            if level >= depth:
                names[:] = []
            for name in names:
                entries.append({"path": self._relative(current / name) + "/", "type": "directory"})
            for name in sorted(files):
                entries.append({"path": self._relative(current / name), "type": "file", **describe(current / name)})
            if len(entries) > 500:
                entries, truncated = entries[:500], True
                break
        return {"path": self._relative(start) + "/", "entries": entries, "truncated": truncated}

    def read(self, path: str, start_line: int = 1, max_lines: int = 200) -> dict:
        target = self.resolve(path)
        if not target.is_file():
            raise ValueError("Read a file, not a directory; list directories with files_list")
        size = target.stat().st_size
        start = max(1, int(start_line))
        count = max(1, min(int(max_lines), MAX_READ_LINES))
        chunk, more = [], False
        with target.open("rb") as handle:
            if b"\0" in handle.read(8192):
                return {"path": self._relative(target), "binary": True, "bytes": size, "text": ""}
            handle.seek(0)
            for number, line in enumerate(handle, 1):
                if number < start:
                    continue
                if len(chunk) >= count:
                    more = True
                    break
                chunk.append(line.decode("utf-8", errors="replace").rstrip("\r\n")[:2000])
        total = None
        if size <= COUNT_LINES_BYTES:
            with target.open("rb") as handle:
                total = sum(1 for _ in handle)
        text = "\n".join(f"{start + index}: {line}" for index, line in enumerate(chunk))
        return {"path": self._relative(target), "bytes": size, "total_lines": total, "start_line": start,
                "end_line": start + len(chunk) - 1, "more": more, "text": text[:120000]}

    def search(self, pattern: str, path: str = "", max_results: int = 50) -> dict:
        try:
            expression = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"Invalid regular expression: {exc}") from None
        start = self.resolve(path)
        files = [start] if start.is_file() else sorted(p for p in start.rglob("*") if p.is_file())
        matches, large, limit = [], [], max(1, min(int(max_results), 200))
        for file in files:
            size = file.stat().st_size
            if size > SEARCH_FILE_BYTES and file != start:
                large.append({"path": self._relative(file), "bytes": size})
                continue
            with file.open("rb") as handle:
                if b"\0" in handle.read(8192):
                    continue
                handle.seek(0)
                for number, raw in enumerate(handle, 1):
                    line = raw.decode("utf-8", errors="replace")
                    if expression.search(line):
                        matches.append({"path": self._relative(file), "line": number, "text": line.strip()[:300]})
                        if len(matches) >= limit:
                            return {"matches": matches, "truncated": True, "skipped_large_files": large[:50]}
        result = {"matches": matches, "truncated": False}
        if large:
            result["skipped_large_files"] = large[:50]
            result["note"] = "Files over 2 MB were not searched; search one directly by passing its path."
        return result

    def _relative(self, path: Path) -> str:
        relative = path.resolve().relative_to(self.visible.resolve()).as_posix()
        return "" if relative == "." else relative


def describe(path: Path) -> dict:
    size = path.stat().st_size
    info = {"bytes": size}
    if size > LARGE_FILE_BYTES:
        info["large"] = True
    with path.open("rb") as handle:
        if b"\0" in handle.read(8192):
            info["binary"] = True
    return info


def member_path(name: str) -> PurePosixPath:
    relative = PurePosixPath(name.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ImportError_(f"Archive entry {name!r} escapes the archive root")
    return relative


def check_git_url(url: str):
    if not (re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d+)?/[^\s]+", url)
            or re.fullmatch(r"ssh://[A-Za-z0-9._-]+@[A-Za-z0-9.-]+(:\d+)?/[^\s]+", url)
            or re.fullmatch(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^\s]+", url)):
        raise ImportError_("Use an https:// or ssh git URL; local paths and other transports are not fetched")
    if url.startswith("-") or "::" in url:
        raise ImportError_("Unsupported git URL")


def extract_text(name: str, raw: bytes) -> tuple[str, int | None, list[str]]:
    """Plain text with page or paragraph structure kept for citations."""
    suffix = Path(name).suffix.lower()
    if suffix == ".pdf":
        try:
            result = subprocess.run([sys.executable, "-m", "optimization_framework.research.pdf_text", json.dumps(PDF_LIMITS)],
                input=raw, capture_output=True, timeout=PDF_LIMITS["cpu_seconds"] * 2, check=True)
            parsed = json.loads(result.stdout)
        except (subprocess.SubprocessError, ValueError):
            raise ImportError_(f"{name}: PDF text extraction failed or exceeded its resource limit") from None
        if not any(page["text"].strip() for page in parsed["pages"]):
            raise ImportError_(f"{name}: the PDF has no extractable text (scanned pages need OCR first)")
        text = "\n\n".join(f"=== page {page['page']} ===\n{page['text']}" for page in parsed["pages"])
        limitations = ["PDF extraction can lose equations, tables and reading order; figures are not read."]
        if parsed.get("truncated"):
            limitations.append("Extraction stopped at 2,000 pages or 8 million characters.")
        return text, parsed.get("page_count"), limitations
    if suffix == ".docx":
        return docx_text(name, raw), None, ["DOCX extraction keeps paragraphs and table cells; equations and figures are not read."]
    return raw.decode("utf-8", errors="replace"), None, []


def docx_text(name: str, raw: bytes) -> str:
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > 64 * 1024**2:
                raise ImportError_(f"{name}: the document body is too large to extract")
            root = ElementTree.fromstring(archive.read(info))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError):
        raise ImportError_(f"{name}: not a readable .docx file") from None
    lines = []
    for paragraph in root.iter(namespace + "p"):
        text = "".join(node.text or "" for node in paragraph.iter(namespace + "t"))
        if text.strip():
            lines.append(text)
    return "\n".join(f"[para {index}] {line}" for index, line in enumerate(lines, 1))
