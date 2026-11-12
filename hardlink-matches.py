#!/usr/bin/env python3
"""Recursively hardlink identical target files to files in a master directory.

Linux / Python 3.9+, standard library only.

Preview:
  python3 hardlink-matches.py --master /path/to/master --target /path/to/target
Apply:
  python3 hardlink-matches.py --master /path/to/master --target /path/to/target --apply

Both paths must be existing, separate directories. Matching uses contents,
independently of filenames and folder layouts. Eligible target entries become
hardlinks to retained master files at their existing target paths.

Matching uses size, sampled content and full SHA-256, followed by another full
byte comparison immediately before replacement. Existing links, metadata
conflicts and targets already linked elsewhere are skipped. On Unraid, actual
link creation is the final filesystem compatibility check. Pause writers and
Mover while applying. Earlier replacements remain if a later operation fails.
Hardlinks share contents, file metadata and timestamps.
"""

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import signal
import stat
import sys
import time
from typing import NamedTuple
import uuid

BLOCK = 4 * 1024 * 1024
SAMPLE = 64 * 1024
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


class FileState(NamedTuple):
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int
    mode: int
    uid: int
    gid: int
    links: int


class LinkUnavailable(Exception):
    """Hardlink creation failed before replacement."""


def log(message):
    print(message, flush=True)


def state(snapshot):
    return FileState(snapshot.st_dev, snapshot.st_ino, snapshot.st_size,
            snapshot.st_mtime_ns, snapshot.st_ctime_ns, snapshot.st_mode,
            snapshot.st_uid, snapshot.st_gid, snapshot.st_nlink)


def inode(entry):
    return entry["state"].device, entry["state"].inode


def check_state(snapshot, expected, after_link=False):
    actual = state(snapshot)
    if after_link:
        # Creating our link changes these two fields on the retained master inode.
        actual = actual._replace(changed_ns=expected.changed_ns, links=expected.links)
    if not stat.S_ISREG(snapshot.st_mode) or actual != expected:
        raise ValueError("File changed during the run. Stabilize the trees and rerun.")


def open_directory(path):
    fd = os.open("/", DIRECTORY_FLAGS)
    try:
        for component in Path(path).parts[1:]:
            next_fd = os.open(component, DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def parent_fd(root, path):
    parts = Path(path).relative_to(root).parts
    if not parts or ".." in parts:
        raise ValueError("File path must be beneath its directory root")
    fd = open_directory(root)
    try:
        for component in parts[:-1]:
            next_fd = os.open(component, DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd, parts[-1]
    finally:
        os.close(fd)


@contextmanager
def open_entry(root, entry):
    with parent_fd(root, entry["path"]) as (parent, name):
        fd = os.open(name, FILE_FLAGS, dir_fd=parent)
        try:
            check_state(os.fstat(fd), entry["state"])
            yield fd
            check_state(os.fstat(fd), entry["state"])
        finally:
            os.close(fd)


def xattrs(fd):
    digest = hashlib.sha256()
    try:
        names = sorted(os.listxattr(fd))
    except OSError as exc:
        if exc.errno not in (errno.ENOTSUP, errno.EOPNOTSUPP):
            raise
        names = []
    for name in names:
        encoded = os.fsencode(name)
        value = os.getxattr(fd, name)
        digest.update(len(encoded).to_bytes(8, "big") + encoded)
        digest.update(len(value).to_bytes(8, "big") + value)
    return digest.hexdigest()


def entry_xattrs(root, entry):
    with open_entry(root, entry) as fd:
        return xattrs(fd)


def inventory(root, counts):
    entries = []

    def scan_error(exc):
        raise exc

    for directory, directories, names in os.walk(root, followlinks=False, onerror=scan_error):
        directories.sort()
        for name in directories[:]:
            if (Path(directory) / name).is_symlink():
                directories.remove(name)
                counts["symlinks_or_special"] += 1
        for name in sorted(names):
            path = Path(directory) / name
            snapshot = path.lstat()
            if stat.S_ISREG(snapshot.st_mode):
                entries.append({"path": str(path), "state": state(snapshot)})
                if len(entries) % 5000 == 0:
                    log(f"Scanned {len(entries):,} files under {str(root)!r}")
            else:
                counts["symlinks_or_special"] += 1
    log(f"Scanned {len(entries):,} files under {str(root)!r}")
    return entries


class Hashes:
    """Reuse hashes of unchanged inodes within this run."""

    def __init__(self):
        self.cache = {}
        self.last_progress = time.monotonic()

    def get(self, root, entry, sample=False):
        key = (sample, entry["state"])
        with open_entry(root, entry) as fd:
            if key in self.cache:
                return self.cache[key]
            digest = hashlib.sha256()
            size = entry["state"].size
            if sample:
                offsets = {0, max(0, size // 2 - SAMPLE // 2), max(0, size - SAMPLE)}
                for offset in sorted(offsets):
                    data = os.pread(fd, min(SAMPLE, size - offset), offset)
                    if len(data) != min(SAMPLE, size - offset):
                        raise ValueError(f"Short read while sampling {entry['path']!r}")
                    digest.update(offset.to_bytes(8, "big") + data)
            else:
                total = 0
                while True:
                    data = os.read(fd, BLOCK)
                    if not data:
                        break
                    digest.update(data)
                    total += len(data)
                    if time.monotonic() - self.last_progress >= 15:
                        log(f"Hashing {total / 2**30:.2f}/{size / 2**30:.2f} GiB: {entry['path']!r}")
                        self.last_progress = time.monotonic()
                if total != size:
                    raise ValueError(f"File size changed while hashing {entry['path']!r}")
            check_state(os.fstat(fd), entry["state"])
            self.cache[key] = digest.hexdigest()
            return self.cache[key]


def find_matches(master_root, target_root, masters, targets, counts):
    by_size = defaultdict(list)
    master_inodes = {inode(entry) for entry in masters}
    for entry in masters:
        by_size[entry["state"].size].append(entry)
    samples = {}
    hashes = Hashes()
    matches = []
    for number, target in enumerate(targets, 1):
        if number % 250 == 0:
            log(f"Matching target files: {number:,}/{len(targets):,}")
        if inode(target) in master_inodes:
            counts["already_linked"] += 1
            continue
        size = target["state"].size
        if size not in by_size:
            counts["unmatched"] += 1
            continue
        if size not in samples:
            groups = defaultdict(list)
            for master in by_size[size]:
                groups[hashes.get(master_root, master, sample=True)].append(master)
            samples[size] = groups
        candidates = samples[size].get(hashes.get(target_root, target, sample=True), [])
        matching = []
        digest = None
        if candidates:
            digest = hashes.get(target_root, target)
            matching = [entry for entry in candidates if hashes.get(master_root, entry) == digest]
        if not matching:
            counts["unmatched"] += 1
            continue
        same_device = [entry for entry in matching if entry["state"].device == target["state"].device]
        if not same_device:
            counts["different_filesystem"] += 1
            log(f"SKIP different filesystem: {target['path']!r}")
            continue
        if target["state"].links > 1:
            counts["target_has_other_links"] += 1
            log(f"SKIP target already has other hardlinks: {target['path']!r}")
            continue
        attributes = entry_xattrs(target_root, target)
        target_permissions = (target["state"].mode, target["state"].uid, target["state"].gid)
        compatible = []
        for master in same_device:
            master_permissions = (master["state"].mode, master["state"].uid, master["state"].gid)
            if (master_permissions == target_permissions
                    and entry_xattrs(master_root, master) == attributes):
                compatible.append(master)
        if not compatible:
            counts["metadata_mismatch"] += 1
            log(f"SKIP ownership, permissions or extended attributes differ: {target['path']!r}")
            continue
        master = min(compatible, key=lambda entry: (-entry["state"].links, entry["path"]))
        matches.append({"master": master, "target": target, "sha256": digest, "xattrs": attributes})
    return matches


def compare_files(master_fd, target_fd, expected_hash, path):
    digest = hashlib.sha256()
    last_progress = time.monotonic()
    total = 0
    while True:
        master_data = os.read(master_fd, BLOCK)
        target_data = os.read(target_fd, BLOCK)
        if master_data != target_data:
            raise ValueError(f"Contents changed before replacement: {path!r}")
        if not master_data:
            break
        digest.update(master_data)
        total += len(master_data)
        if time.monotonic() - last_progress >= 15:
            log(f"Rechecking {total / 2**30:.2f} GiB: {path!r}")
            last_progress = time.monotonic()
    if digest.hexdigest() != expected_hash:
        raise ValueError(f"Content changed since matching: {path!r}")


def replace_match(master_root, target_root, match, expected_master, counts):
    master = match["master"]
    target = match["target"]
    temporary = ".hardlink-match-" + uuid.uuid4().hex + ".tmp"
    with parent_fd(master_root, master["path"]) as (master_parent, master_name), \
         parent_fd(target_root, target["path"]) as (target_parent, target_name):
        master_fd = os.open(master_name, FILE_FLAGS, dir_fd=master_parent)
        try:
            target_fd = os.open(target_name, FILE_FLAGS, dir_fd=target_parent)
            try:
                master_before = os.fstat(master_fd)
                target_before = os.fstat(target_fd)
                check_state(master_before, expected_master)
                check_state(target_before, target["state"])
                if xattrs(master_fd) != match["xattrs"] or xattrs(target_fd) != match["xattrs"]:
                    raise ValueError(f"Attributes changed before replacement: {target['path']!r}")
                compare_files(master_fd, target_fd, match["sha256"], target["path"])
                check_state(os.fstat(master_fd), state(master_before))
                check_state(os.fstat(target_fd), state(target_before))
                try:
                    os.link(master_name, temporary, src_dir_fd=master_parent,
                            dst_dir_fd=target_parent, follow_symlinks=False)
                except OSError as exc:
                    if exc.errno in (errno.EXDEV, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS):
                        raise LinkUnavailable(str(exc)) from exc
                    raise
                try:
                    linked = os.stat(temporary, dir_fd=target_parent, follow_symlinks=False)
                    if not stat.S_ISREG(linked.st_mode) or (linked.st_dev, linked.st_ino) != inode(master):
                        raise ValueError("Master pathname changed while creating the hardlink")
                    check_state(os.fstat(master_fd), state(master_before), after_link=True)
                    check_state(os.stat(master_name, dir_fd=master_parent, follow_symlinks=False), state(master_before), after_link=True)
                    check_state(os.fstat(target_fd), state(target_before))
                    check_state(os.stat(target_name, dir_fd=target_parent, follow_symlinks=False), state(target_before))
                    if xattrs(master_fd) != match["xattrs"]:
                        raise ValueError("Master attributes changed during linking")
                    os.replace(temporary, target_name, src_dir_fd=target_parent, dst_dir_fd=target_parent)
                    counts["linked"] += 1
                    after = os.stat(target_name, dir_fd=target_parent, follow_symlinks=False)
                    if (after.st_dev, after.st_ino) != inode(master):
                        raise ValueError(f"Replacement needs inspection: {target['path']!r}")
                    try:
                        os.fsync(target_parent)
                    except OSError as exc:
                        log(f"WARNING directory synchronization failed after linking {target['path']!r}: {exc}")
                    return state(os.fstat(master_fd))
                finally:
                    try:
                        os.unlink(temporary, dir_fd=target_parent)
                    except FileNotFoundError:
                        pass
            finally:
                os.close(target_fd)
        finally:
            os.close(master_fd)


def interrupted(signum, frame):
    raise KeyboardInterrupt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--master", required=True, help="retained master directory, scanned recursively")
    parser.add_argument("--target", required=True, help="directory containing existing files to deduplicate")
    parser.add_argument("--apply", action="store_true", help="replace eligible target copies with hardlinks (default: preview)")
    args = parser.parse_args(argv)
    counts = Counter()
    lock_fd = None
    signal.signal(signal.SIGTERM, interrupted)
    try:
        master_root = Path(args.master).resolve(strict=True)
        target_root = Path(args.target).resolve(strict=True)
        if not master_root.is_dir() or not target_root.is_dir():
            raise ValueError("Both --master and --target must be existing directories")
        if (master_root == target_root or master_root in target_root.parents
                or target_root in master_root.parents or os.path.samefile(master_root, target_root)):
            raise ValueError("Master and target must be separate, non-overlapping trees")
        if args.apply:
            lock_fd = open_directory(target_root)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        log(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}\nMaster: {str(master_root)!r}\nTarget: {str(target_root)!r}")
        masters = inventory(master_root, counts)
        targets = inventory(target_root, counts)
        matches = find_matches(master_root, target_root, masters, targets, counts)
        counts["eligible"] = len(matches)
        potential = sum(match["target"]["state"].size for match in matches)
        log(f"Eligible: {len(matches):,} files, {potential / 2**30:.2f} GiB of logical duplicate data")
        master_states = {inode(entry): entry["state"] for entry in masters}
        for match in matches:
            master = match["master"]
            target = match["target"]
            if not args.apply:
                log(f"WOULD LINK target {target['path']!r} to master {master['path']!r}")
                continue
            try:
                updated = replace_match(master_root, target_root, match, master_states[inode(master)], counts)
                master_states[inode(master)] = updated
                log(f"LINKED target {target['path']!r} to master {master['path']!r}")
            except LinkUnavailable as exc:
                counts["unsupported_link"] += 1
                log(f"SKIP hardlink unavailable for {target['path']!r}: {exc}")
        log("Summary: " + ", ".join(f"{name}={number}" for name, number in sorted(counts.items())))
        return 1 if counts["unsupported_link"] else 0
    except KeyboardInterrupt:
        print(f"Interrupted. Completed replacements: {counts['linked']}.", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc!s}\nCompleted replacements: {counts['linked']}. Earlier replacements remain in place.", file=sys.stderr)
        return 1
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
