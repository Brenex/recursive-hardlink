#!/usr/bin/env python3
"""Find identical files across torrent/media trees, then optionally hardlink them.

Linux / Python 3.9+; standard library only. Run --help, scan --help, apply --help.

Quick start (scan never changes files in either tree):
  python3 hardlink-matches.py scan --plan hardlink-plan.json
  python3 hardlink-matches.py show --plan hardlink-plan.json --limit 20
  python3 hardlink-matches.py apply --plan hardlink-plan.json --limit 5
  python3 hardlink-matches.py apply --plan hardlink-plan.json

Defaults: /mnt/user/data/torrents and /mnt/user/data/media, recursively, all regular
files, independent of filename or folder layout. Source symlinks/special files
are skipped. Existing (device, inode) matches need no hashing. Other candidates
are filtered by size and sampled content, then matched with full SHA-256 hashes.
Scan writes a JSON report/plan plus a resumable SQLite hash cache OUTSIDE the trees.
Plan creation refuses to overwrite an existing plan. Use a new filename to rescan.

Apply keeps torrent files as the source, replacing only media directory entries.
It reopens paths without following symlinks, checks recorded file identities,
compares every byte again, and checks the planned hash. It creates a temporary
hardlink in the media directory, verifies it, then atomically replaces the media
name. There is no delete-first step, copying fallback, or cross-disk migration.
Failures before replacement leave the media entry in place. A crash may leave a
temporary .hardlink-match-*.tmp link; inspect it before removing it.

Review statuses are never applied: conflicting ownership/mode/extended attributes,
media files linked elsewhere, or different filesystem device IDs. Matching device
IDs on Unraid do NOT guarantee that a link will work: link() is authoritative.
Unsupported/cross-device operations are logged, without replacing the media file.
The JSONL apply log lives alongside the plan; apply can be rerun safely.

Pause downloads, Sonarr/Radarr imports, mover/balancing, and any other writers
before applying. Scan can run while services are active but may report changed
files; rescan those with the trees stable. File checks/advisory locks are not a
lock against other applications, nor a transaction over the entire tree.

Hardlinks share contents, mode, ownership, xattrs and timestamps. The source's
mtime wins if the two copies had different mtimes. Editing either hardlink in
place edits the same file. Potential savings are logical duplicate bytes, NOT
guaranteed physical space reclaimed (reflinks, sparse files, snapshots, open FDs).
Neither tree is accessible remotely from ChatGPT: run this on your Unraid host.
"""

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import time
import uuid

FORMAT = "hardlink-matches-v1"
BLOCK = 4 * 1024 * 1024
SAMPLE = 64 * 1024
STATE_FIELDS = ("dev", "ino", "size", "mtime_ns", "ctime_ns", "mode", "uid", "gid", "nlink")


def log(message):
    print(message, flush=True)


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def state(st):
    return dict(zip(STATE_FIELDS, (st.st_dev, st.st_ino, st.st_size,
        st.st_mtime_ns, st.st_ctime_ns, st.st_mode, st.st_uid, st.st_gid, st.st_nlink)))


def inode(entry):
    return (entry["state"]["dev"], entry["state"]["ino"])


def assert_state(st, expected, source=False):
    actual = state(st)
    fields = [k for k in STATE_FIELDS if not source or k not in ("ctime_ns", "nlink")]
    if not stat.S_ISREG(st.st_mode) or any(actual[k] != expected[k] for k in fields):
        raise ValueError("File changed since scan/check; rescan before applying")


def inside(path, root):
    return os.path.commonpath((str(path), str(root))) == str(root)


def roots(torrents, media):
    torrent_root = Path(torrents).resolve(strict=True)
    media_root = Path(media).resolve(strict=True)
    if not torrent_root.is_dir() or not media_root.is_dir():
        raise ValueError("Both roots must be directories")
    if inside(torrent_root, media_root) or inside(media_root, torrent_root) or os.path.samefile(torrent_root, media_root):
        raise ValueError("Trees must be distinct and non-overlapping")
    return torrent_root, media_root


def output_path(path, tree_roots):
    path = Path(path).absolute()
    resolved = path.resolve()
    if any(inside(resolved, root) for root in tree_roots):
        raise ValueError("Keep plans, logs and cache files outside the scanned trees")
    if path.is_symlink():
        raise ValueError("Output path must not be a symlink: " + repr(str(path)))
    if not path.parent.is_dir():
        raise ValueError("Output parent directory does not exist: " + repr(str(path.parent)))
    return path


def open_directory(path):
    """Open an absolute directory path, rejecting symlinks in every component."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in Path(path).parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def parent_fd(root, path):
    parts = Path(path).relative_to(root).parts
    if not parts or any(part in ("..", ".") for part in parts):
        raise ValueError("Invalid file path beneath root")
    fd = open_directory(root)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd, parts[-1]
    finally:
        os.close(fd)


@contextmanager
def open_entry(root, entry):
    with parent_fd(root, entry["path"]) as (directory, name):
        # O_NONBLOCK ensures a regular-file-to-FIFO race cannot hang the process.
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            assert_state(os.fstat(fd), entry["state"])
            yield fd
            assert_state(os.fstat(fd), entry["state"])
        finally:
            os.close(fd)


def xattrs(fd):
    """Digest all extended attributes, including ACLs, without storing their values."""
    digest = hashlib.sha256()
    try:
        names = sorted(os.listxattr(fd))
    except OSError as exc:
        if exc.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
            names = []
        else:
            raise
    for name in names:
        encoded = os.fsencode(name)
        value = os.getxattr(fd, name)
        digest.update(len(encoded).to_bytes(8, "big") + encoded)
        digest.update(len(value).to_bytes(8, "big") + value)
    return digest.hexdigest()


def metadata(root, entry):
    with open_entry(root, entry) as fd:
        attrs = xattrs(fd)
    s = entry["state"]
    return {"mode": s["mode"], "uid": s["uid"], "gid": s["gid"], "xattrs": attrs}


def inventory(root, errors):
    entries = []
    skipped = 0
    def walk_error(exc):
        errors.append({"path": str(exc.filename), "error": str(exc)})
    for directory, directories, names in os.walk(root, followlinks=False, onerror=walk_error):
        directories.sort()
        for name in directories[:]:
            path = Path(directory) / name
            if path.is_symlink():
                directories.remove(name)
                skipped += 1
        for name in sorted(names):
            path = Path(directory) / name
            try:
                st = path.lstat()
                if stat.S_ISREG(st.st_mode):
                    entries.append({"path": str(path), "state": state(st)})
                    if len(entries) % 5000 == 0:
                        log("Inventory: {:,} regular files under {}".format(len(entries), root))
                else:
                    skipped += 1
            except OSError as exc:
                errors.append({"path": str(path), "error": str(exc)})
    log("Inventory: {:,} files, {:,} symlinks/special entries skipped: {}".format(len(entries), skipped, root))
    return entries, skipped


class Hashes:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path))
        self.db.execute("CREATE TABLE IF NOT EXISTS hashes (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.memory = {}
        self.last_progress = time.monotonic()
        self.cache_hits = 0
        self.bytes_read = 0

    def close(self):
        self.db.close()

    def get(self, root, entry, kind):
        key = json.dumps(["sha256-v1", kind, entry["state"]], sort_keys=True)
        with open_entry(root, entry) as fd:
            if key in self.memory:
                self.cache_hits += 1
                return self.memory[key]
            cached = self.db.execute("SELECT value FROM hashes WHERE key=?", (key,)).fetchone()
            if cached:
                self.cache_hits += 1
                self.memory[key] = cached[0]
                return cached[0]
            digest = hashlib.sha256()
            size = entry["state"]["size"]
            if kind == "sample":
                for offset in sorted({0, max(0, size // 2 - SAMPLE // 2), max(0, size - SAMPLE)}):
                    data = os.pread(fd, min(SAMPLE, size - offset), offset)
                    if len(data) != min(SAMPLE, size - offset):
                        raise ValueError("Short read while sampling " + repr(entry["path"]))
                    digest.update(offset.to_bytes(8, "big") + data)
                    self.bytes_read += len(data)
            else:
                total = 0
                while True:
                    block = os.read(fd, BLOCK)
                    if not block:
                        break
                    digest.update(block)
                    total += len(block)
                    self.bytes_read += len(block)
                    if time.monotonic() - self.last_progress >= 15:
                        log("Hashing {:.2f}/{:.2f} GiB: {!r}".format(total / 2**30, size / 2**30, entry["path"]))
                        self.last_progress = time.monotonic()
                if total != size:
                    raise ValueError("File size changed during hashing")
            assert_state(os.fstat(fd), entry["state"])
            result = digest.hexdigest()
            self.db.execute("INSERT OR REPLACE INTO hashes VALUES (?, ?)", (key, result))
            self.db.commit()
            self.memory[key] = result
            return result


def save_plan(path, plan):
    # Exclusive publication: never overwrite a reviewed plan, even if it appears
    # during scanning. The temporary report is on the same filesystem as the plan.
    fd, temporary = tempfile.mkstemp(prefix=".hardlink-plan-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(plan, handle, indent=2, ensure_ascii=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path, follow_symlinks=False)
    finally:
        os.unlink(temporary)


def scan(args):
    torrent_root, media_root = roots(args.torrents, args.media)
    plan_path = output_path(args.plan, (torrent_root, media_root))
    if plan_path.exists():
        raise ValueError("Plan already exists; choose a new --plan filename")
    cache_path = output_path(args.cache or str(plan_path.parent / "hardlink-hashes.sqlite3"), (torrent_root, media_root))
    if cache_path == plan_path:
        raise ValueError("Plan and cache must have different paths")
    errors = []
    torrents, torrent_skips = inventory(torrent_root, errors)
    media, media_skips = inventory(media_root, errors)
    by_inode = defaultdict(list)
    by_size = defaultdict(list)
    for entry in torrents:
        by_inode[inode(entry)].append(entry)
        by_size[entry["state"]["size"]].append(entry)
    sample_index = {}
    source_metadata = {}
    rows = []
    hasher = Hashes(cache_path)
    try:
        for number, target in enumerate(media, 1):
            row = {"media": target, "status": "NO_SIZE_MATCH"}
            try:
                if inode(target) in by_inode:
                    row.update(status="ALREADY_LINKED", torrent=by_inode[inode(target)][0])
                elif target["state"]["size"] in by_size:
                    size = target["state"]["size"]
                    if size not in sample_index:
                        groups = defaultdict(list)
                        for source in by_size[size]:
                            groups[hasher.get(torrent_root, source, "sample")].append(source)
                        sample_index[size] = groups
                    candidates = sample_index[size].get(hasher.get(media_root, target, "sample"), [])
                    row["status"] = "NO_CONTENT_MATCH"
                    if candidates:
                        digest = hasher.get(media_root, target, "full")
                        matches = [s for s in candidates if hasher.get(torrent_root, s, "full") == digest]
                        if matches:
                            target_metadata = metadata(media_root, target)
                            for source in matches:
                                key = json.dumps(source["state"], sort_keys=True)
                                if key not in source_metadata:
                                    source_metadata[key] = metadata(torrent_root, source)
                            def preference(source):
                                key = json.dumps(source["state"], sort_keys=True)
                                return (source["state"]["dev"] != target["state"]["dev"],
                                    source_metadata[key] != target_metadata,
                                    -source["state"]["nlink"], source["path"])
                            source = min(matches, key=preference)
                            attrs = source_metadata[json.dumps(source["state"], sort_keys=True)]
                            status = "READY"
                            if source["state"]["dev"] != target["state"]["dev"]:
                                status = "CROSS_FILESYSTEM"
                            elif target["state"]["nlink"] > 1:
                                status = "REVIEW_MULTIPLE_LINKS"
                            elif attrs != target_metadata:
                                status = "REVIEW_METADATA"
                            row.update(status=status, torrent=source, sha256=digest,
                                torrent_metadata=attrs, media_metadata=target_metadata,
                                matching_torrents=[s["path"] for s in matches])
            except (OSError, ValueError) as exc:
                row.update(status="ERROR", error=str(exc))
                errors.append({"path": target["path"], "error": str(exc)})
            rows.append(row)
            if number % 250 == 0 or time.monotonic() - hasher.last_progress >= 15:
                log("Matched {:,}/{:,} media files: {}".format(number, len(media), dict(Counter(r["status"] for r in rows))))
                hasher.last_progress = time.monotonic()
        summary = dict(Counter(row["status"] for row in rows))
        potential = sum(row["media"]["state"]["size"] for row in rows if row["status"] == "READY")
        plan = {"format": FORMAT, "created_utc": timestamp(), "complete": not errors,
            "torrents_root": str(torrent_root), "media_root": str(media_root),
            "torrent_files": len(torrents), "media_files": len(media),
            "skipped_entries": torrent_skips + media_skips, "summary": summary,
            "potential_duplicate_bytes": potential,
            "hash_cache_hits": hasher.cache_hits, "bytes_read_for_hashes": hasher.bytes_read,
            "errors": errors, "files": rows}
        save_plan(plan_path, plan)
    finally:
        hasher.close()
    log("Summary: " + json.dumps(summary, sort_keys=True))
    log("READY logical duplicate bytes: {:,} ({:.2f} GiB); actual reclaimed space may differ".format(potential, potential / 2**30))
    log("Plan: {}\nCache: {}\nNo torrent or media files were changed.".format(plan_path, cache_path))
    if errors:
        log("INCOMPLETE: {} errors. Apply is blocked; resolve errors and rescan to a new plan.".format(len(errors)))
        return 2
    return 0


def load_plan(path):
    with open(path, encoding="utf-8") as handle:
        plan = json.load(handle)
    if plan.get("format") != FORMAT or not isinstance(plan.get("files"), list):
        raise ValueError("Unrecognized plan format")
    return plan


def show(args):
    plan = load_plan(args.plan)
    log("Complete: {}\nSummary: {}".format(plan["complete"], json.dumps(plan["summary"], sort_keys=True)))
    log("Potential logical duplicate bytes: {:,}".format(plan["potential_duplicate_bytes"]))
    statuses = set(args.status or ["READY", "REVIEW_METADATA", "REVIEW_MULTIPLE_LINKS", "CROSS_FILESYSTEM", "ERROR"])
    rows = [row for row in plan["files"] if row["status"] in statuses]
    for row in rows[:args.limit]:
        log("\n{}  {:,} bytes\n  torrent: {!r}\n  media:   {!r}".format(row["status"],
            row["media"]["state"]["size"], row.get("torrent", {}).get("path"), row["media"]["path"]))
        if "error" in row:
            log("  error: " + row["error"])
        if row["status"] == "REVIEW_METADATA":
            log("  Ownership, permissions or extended attributes differ; not applied.")
        elif row["status"] == "REVIEW_MULTIPLE_LINKS":
            log("  Media inode already has other links; not applied.")
    log("\nShowing {} of {} selected rows. Use --status ALREADY_LINKED to inspect existing links.".format(min(len(rows), args.limit), len(rows)))
    for error in plan.get("errors", [])[:args.limit]:
        log("Scan error: {!r}: {}".format(error["path"], error["error"]))
    return 0


def audit(handle, event):
    handle.write(json.dumps(dict(event, utc=timestamp()), ensure_ascii=True) + "\n")
    handle.flush()
    os.fsync(handle.fileno())


def compare_files(source_fd, target_fd, expected_hash, source_path):
    digest = hashlib.sha256()
    total = 0
    last = time.monotonic()
    while True:
        left = os.read(source_fd, BLOCK)
        right = os.read(target_fd, BLOCK)
        if left != right:
            raise ValueError("Files are no longer byte-for-byte identical; nothing replaced")
        if not left:
            break
        digest.update(left)
        total += len(left)
        if time.monotonic() - last >= 15:
            log("Rechecking {:.2f} GiB: {!r}".format(total / 2**30, source_path))
            last = time.monotonic()
    if digest.hexdigest() != expected_hash:
        raise ValueError("Content does not match the scanned SHA-256; nothing replaced")


def replace_one(row, torrent_root, media_root):
    source = row["torrent"]
    target = row["media"]
    temporary = ".hardlink-match-" + uuid.uuid4().hex + ".tmp"
    created = False
    replaced = False
    with parent_fd(torrent_root, source["path"]) as (src_dir, src_name), \
         parent_fd(media_root, target["path"]) as (dst_dir, dst_name):
        src_fd = os.open(src_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=src_dir)
        try:
            dst_fd = os.open(dst_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dst_dir)
            try:
                before_source = os.fstat(src_fd)
                before_target = os.fstat(dst_fd)
                assert_state(before_source, source["state"], source=True)
                if (before_source.st_dev, before_source.st_ino) == (before_target.st_dev, before_target.st_ino):
                    return "ALREADY_LINKED", ""
                assert_state(before_target, target["state"])
                if before_target.st_nlink != 1:
                    raise ValueError("Media file acquired another hardlink; rescan")
                if before_source.st_dev != before_target.st_dev:
                    raise ValueError("Files are now on different filesystems")
                if row["torrent_metadata"] != row["media_metadata"]:
                    raise ValueError("Plan has conflicting metadata; rescan")
                if xattrs(src_fd) != row["torrent_metadata"]["xattrs"] or xattrs(dst_fd) != row["media_metadata"]["xattrs"]:
                    raise ValueError("Extended attributes changed; rescan")
                compare_files(src_fd, dst_fd, row["sha256"], source["path"])
                assert_state(os.fstat(src_fd), state(before_source))
                assert_state(os.fstat(dst_fd), state(before_target))
                os.link(src_name, temporary, src_dir_fd=src_dir, dst_dir_fd=dst_dir, follow_symlinks=False)
                created = True
                linked = os.stat(temporary, dir_fd=dst_dir, follow_symlinks=False)
                if (linked.st_dev, linked.st_ino) != (before_source.st_dev, before_source.st_ino):
                    raise ValueError("Torrent pathname changed during linking; nothing replaced")
                # link() changes source ctime/link count, so ignore those two fields
                # after our own link creation, while still checking the other fields.
                assert_state(os.fstat(src_fd), state(before_source), source=True)
                assert_state(os.stat(src_name, dir_fd=src_dir, follow_symlinks=False), state(before_source), source=True)
                assert_state(os.fstat(dst_fd), state(before_target))
                assert_state(os.stat(dst_name, dir_fd=dst_dir, follow_symlinks=False), state(before_target))
                if xattrs(src_fd) != row["torrent_metadata"]["xattrs"]:
                    raise ValueError("Source attributes changed during linking; nothing replaced")
                os.replace(temporary, dst_name, src_dir_fd=dst_dir, dst_dir_fd=dst_dir)
                replaced = True
                created = False
                after = os.stat(dst_name, dir_fd=dst_dir, follow_symlinks=False)
                if (after.st_dev, after.st_ino) != (before_source.st_dev, before_source.st_ino):
                    raise ValueError("Post-replacement inode verification failed")
                try:
                    os.fsync(dst_dir)
                except OSError as exc:
                    return "LINKED", "Directory fsync unsupported/failed: " + str(exc)
                return "LINKED", ""
            finally:
                os.close(dst_fd)
        except Exception as exc:
            if replaced:
                return "APPLIED_NEEDS_REVIEW", str(exc)
            raise
        finally:
            os.close(src_fd)
            if created:
                os.unlink(temporary, dir_fd=dst_dir)


def apply(args):
    plan = load_plan(args.plan)
    if not plan.get("complete") or plan.get("errors"):
        raise ValueError("Incomplete/error-containing plan cannot be applied; rescan")
    torrent_root, media_root = roots(plan["torrents_root"], plan["media_root"])
    if str(torrent_root) != plan["torrents_root"] or str(media_root) != plan["media_root"]:
        raise ValueError("Root paths changed since scan; rescan")
    plan_path = output_path(args.plan, (torrent_root, media_root))
    log_path = output_path(str(plan_path) + ".apply.jsonl", (torrent_root, media_root))
    rows = [row for row in plan["files"] if row["status"] == "READY"]
    if args.limit is not None:
        rows = rows[:args.limit]
    seen = set()
    for row in rows:
        target = row["media"]["path"]
        if target in seen:
            raise ValueError("Plan contains duplicate media destinations")
        seen.add(target)
        # Validate all selected paths BEFORE starting any replacements.
        with parent_fd(torrent_root, row["torrent"]["path"]), parent_fd(media_root, target):
            pass
        if row["media"]["state"]["nlink"] != 1:
            raise ValueError("READY entry unexpectedly has multiple media hardlinks")
    if not rows:
        log("No READY entries selected. No changes made.")
        return 0
    log("Selected {} candidates. Torrent files remain the source; media entries will be replaced.".format(len(rows)))
    if not args.yes:
        log("Pause downloads, imports, mover and other writers before continuing.")
        if input("Type LINK to apply this plan: ") != "LINK":
            raise ValueError("Cancelled; no changes made")
    lock_fd = open_directory(media_root)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        log_fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        counts = Counter()
        with os.fdopen(log_fd, "a", encoding="utf-8") as handle:
            for row in rows:
                event = {"torrent": row["torrent"]["path"], "media": row["media"]["path"], "sha256": row["sha256"]}
                audit(handle, dict(event, status="STARTED"))
                try:
                    status, detail = replace_one(row, torrent_root, media_root)
                except (OSError, ValueError) as exc:
                    status = "LINK_UNSUPPORTED" if isinstance(exc, OSError) and exc.errno in (errno.EXDEV, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS) else "ERROR"
                    detail = str(exc)
                counts[status] += 1
                audit(handle, dict(event, status=status, detail=detail))
                log("{}: {!r}{}".format(status, row["media"]["path"], "; " + detail if detail else ""))
        log("Apply summary: {}\nAudit log: {}".format(dict(counts), log_path))
        return 2 if any(counts[s] for s in ("ERROR", "LINK_UNSUPPORTED", "APPLIED_NEEDS_REVIEW")) else 0
    finally:
        os.close(lock_fd)


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    scan_parser = commands.add_parser("scan", help="Read-only recursive content scan; write a plan and cache")
    scan_parser.add_argument("--torrents", default="/mnt/user/data/torrents")
    scan_parser.add_argument("--media", default="/mnt/user/data/media")
    scan_parser.add_argument("--plan", default="hardlink-plan.json")
    scan_parser.add_argument("--cache", help="SQLite hash cache (default: hardlink-hashes.sqlite3 beside plan)")
    scan_parser.set_defaults(func=scan)
    show_parser = commands.add_parser("show", help="Show summary and selected plan entries")
    show_parser.add_argument("--plan", default="hardlink-plan.json")
    show_parser.add_argument("--limit", type=positive, default=20)
    show_parser.add_argument("--status", action="append", help="Filter status; repeat to select several")
    show_parser.set_defaults(func=show)
    apply_parser = commands.add_parser("apply", help="Recheck and apply only READY entries in a saved plan")
    apply_parser.add_argument("--plan", default="hardlink-plan.json")
    apply_parser.add_argument("--limit", type=positive, help="Maximum candidates to process this run")
    apply_parser.add_argument("--yes", action="store_true", help="Skip the LINK confirmation prompt")
    apply_parser.set_defaults(func=apply)
    args = parser.parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        log("Interrupted. Earlier completed replacements remain; inspect the audit log before rerunning.")
        return 130
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, EOFError) as exc:
        print("Error: " + str(exc), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
