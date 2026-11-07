"""Check the working tree, Git index, or outgoing commits for private data."""

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 10_000_000
PRIVATE_DIRS = {
    ".local", ".cache", ".upstream", ".venv", "browser-profiles", "workspaces",
}
PRIVATE_NAMES = {
    "active-user.json", "diagnostics.json", "launch-url.txt", "prepared-live.json",
    "report.html", "report.md", "id_dsa", "id_ecdsa", "id_ed25519", "id_rsa",
}
PRIVATE_SUFFIXES = {
    ".docx", ".dump", ".har", ".kdbx", ".key", ".log", ".p12", ".pdf",
    ".pem", ".pfx", ".ttf", ".zip",
}
SECRET_PATTERNS = {
    "private key": r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----",
    "GitHub token": r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b",
    "AWS access key": r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    "provider API key": r"\b(?:sk-(?:proj-|ant-api03-)?[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{35})\b",
    "JWT": r"\beyJ[A-Za-z0-9_-]{6,}\.eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{12,}\b",
    "local unlock URL": r"https?://(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?/unlock/[A-Za-z0-9_-]{24,}",
    "Moodle URL token": r"(?:[?&])wstoken=[A-Za-z0-9_-]{10,}",
    "embedded URL credential": r"\b(?:https?|postgres(?:ql)?|mysql)://(?P<value>[^\s:/@]+:[^\s/@]+)@",
    "literal credential": (
        r"""(?<![\w.-])["']?(?:pass(?:word|wd)|wstoken|api[_-]?key|access[_-]?token|"""
        r"""client[_-]?secret|study[_-]?session|token|secret|credential|authorization)["']?\s*"""
        r"""[:=]\s*(?P<quote>["'])(?P<value>[^"'\r\n]{6,})(?P=quote)"""
    ),
    "environment credential": (
        r"(?m)^[ \t]*(?:export[ \t]+)?(?-i:[A-Z0-9_]*(?:PASSWORD|PASSWD|TOKEN|SECRET|API_KEY))"
        r"[ \t]*=[ \t]*(?P<value>[A-Za-z0-9_./+=:@-]{6,})[ \t]*(?:#[^\r\n]*)?\r?$"
    ),
}
PATTERNS = {name: re.compile(pattern, re.IGNORECASE) for name, pattern in SECRET_PATTERNS.items()}
# Only these exact, synthetic fixtures are exempt. Never exempt whole files.
FIXTURES = {
    ("tests/test_p0.py", "literal credential"):
        "2202e402768c1dfc55af2261a53d2580a357900b440fdbfd5b7f11ab844a9d69",
    ("tests/test_p5.py", "literal credential"):
        "fb3426e83bbbece81be6c41ec90e605f2dcf3f0c98a7e8978c01d2c71f4dac3f",
    ("tests/test_sis.py", "embedded URL credential"):
        "c3dfc258687e3b7feac4a554fc02902397b96390d3ed572bb5ec79aa187bdd91",
}
OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class AuditError(Exception):
    pass


def git(*args):
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=False)
    if result.returncode:
        raise AuditError("Git could not read a required object or index entry.")
    return result.stdout


def entries(raw):
    return (entry for entry in raw.decode("utf-8", errors="surrogateescape").split("\0") if entry)


def private_path(name):
    path = PurePosixPath(name.lower())
    return (
        not PRIVATE_DIRS.isdisjoint(path.parts)
        or path.name in PRIVATE_NAMES
        or path.suffix in PRIVATE_SUFFIXES
        or ".sqlite" in path.name
        or (path.name == ".env" or path.name.startswith(".env.")) and path.name != ".env.example"
    )


def decode(data):
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-8-sig", errors="replace")


def content_issues(name, data):
    if data.startswith((b"SQLite format 3\0", b"%PDF-", b"PK\x03\x04")):
        yield "private database, PDF, or archive"
        return
    content = decode(data)
    for kind, pattern in PATTERNS.items():
        for match in pattern.finditer(content):
            value = match.groupdict().get("value") or match.group()
            fingerprint = hashlib.sha256(value.encode()).hexdigest()
            if FIXTURES.get((name, kind)) != fingerprint:
                yield f"{kind}, line {content.count(chr(10), 0, match.start()) + 1}"


def safe_name(name):
    for pattern in PATTERNS.values():
        name = pattern.sub("<redacted>", name)
    return repr(name)


def check_entry(name, mode, read):
    if private_path(name):
        return ["private file or runtime artifact"]
    if mode not in {"100644", "100755"}:
        return ["non-regular Git file"]
    return list(content_issues(name, read()))


def read_blob(oid):
    if int(git("cat-file", "-s", oid)) > MAX_BYTES:
        raise AuditError("A Git blob exceeds the privacy scan size limit.")
    return git("cat-file", "blob", oid)


def check_index_and_worktree():
    findings = set()
    paths = set()
    for entry in entries(git("ls-files", "--stage", "-z")):
        meta, name = entry.split("\t", 1)
        mode, oid, _ = meta.split()
        paths.add(name)
        for issue in check_entry(name, mode, lambda: read_blob(oid)):
            findings.add(("index", name, issue))

    paths.update(entries(git("ls-files", "--others", "--exclude-standard", "-z")))
    root = ROOT.resolve()
    for name in paths:
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            findings.add(("working tree", name, "non-regular or out-of-tree path"))
            continue
        if not path.exists():  # Already-deleted files are still checked in the index.
            continue
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            findings.add(("working tree", name, "non-regular file or exceeds scan size limit"))
            continue
        for issue in check_entry(name, "100644", path.read_bytes):
            findings.add(("working tree", name, issue))
    return findings, f"{len(paths)} tracked or stageable paths"


def outgoing_commits(updates):
    commits = set()
    for line in updates:
        fields = line.split()
        if len(fields) != 4:
            raise AuditError("Unexpected Git pre-push update.")
        _, local, _, remote = fields
        if not OBJECT_ID.fullmatch(local) or not OBJECT_ID.fullmatch(remote):
            raise AuditError("Unexpected Git object ID in pre-push update.")
        if not int(local, 16):  # Deleting a ref sends no new files.
            continue
        local_commit = git("rev-parse", "--verify", local + "^{commit}").decode().strip()
        exclude = ["^" + remote] if int(remote, 16) else []
        commits.update(git("rev-list", local_commit, *exclude).decode().splitlines())
    return commits


def check_outgoing(updates):
    findings = set()
    checked = set()
    commits = outgoing_commits(updates)
    for commit in sorted(commits):
        for entry in entries(git("ls-tree", "-r", "-z", commit)):
            meta, name = entry.split("\t", 1)
            mode, _, oid = meta.split()
            key = (name, mode, oid)
            if key in checked:
                continue
            checked.add(key)
            for issue in check_entry(name, mode, lambda: read_blob(oid)):
                findings.add(("commit " + commit[:12], name, issue))
    return findings, f"{len(commits)} outgoing commits"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pre-push", action="store_true", help="read Git ref updates from stdin")
    options = parser.parse_args()
    try:
        findings, scope = check_outgoing(sys.stdin) if options.pre_push else check_index_and_worktree()
    except (AuditError, OSError, ValueError):
        print("Repository privacy check could not read all inputs. Verify Git objects and file access.",
              file=sys.stderr)
        return 1
    if findings:
        for location, path, issue in sorted(findings):
            print(f"{location}: {safe_name(path)}: {issue}", file=sys.stderr)
        return 1
    print(f"Repository privacy check passed ({scope}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
