#!/usr/bin/env python3
"""Recreate a directory tree using hardlinks to its regular files.

Linux / Python 3.9+, standard library only.

    python3 recursive-hardlink.py -s /path/to/source -d /path/to/destination -n
    python3 recursive-hardlink.py -s /path/to/source -d /path/to/destination

The contents of SOURCE go directly into DESTINATION, preserving relative paths
and empty directories. Source symlinks and special files are skipped. Existing
correct hardlinks are skipped. Conflicts and known filesystem mismatches abort
preflight before destination changes. Source and destination roots may be given
through symlink aliases, which are resolved before checking tree overlap.

This is a directory-mirroring utility. For matching renamed torrent files to an
organized media library by their contents, use hardlink-matches.py.

Hardlinks share contents and file metadata with their source. New directories
use the process umask. Keep both trees stable during execution. Preflight and
descriptor checks cannot lock out unrelated writers. An execution-time failure
can leave earlier directories and links in place. On Unraid, link() is the final
filesystem compatibility check even when device IDs match.
"""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
import os
from pathlib import Path
import signal
import stat
import sys


DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class HardlinkError(Exception):
    """An input, conflict, or filesystem-state check failed."""


def identity(snapshot):
    return snapshot.st_dev, snapshot.st_ino


def file_state(snapshot):
    return (snapshot.st_dev, snapshot.st_ino, snapshot.st_size,
            snapshot.st_mtime_ns, snapshot.st_ctime_ns, snapshot.st_mode,
            snapshot.st_uid, snapshot.st_gid, snapshot.st_nlink)


def stable_file_state(snapshot):
    # Our own hardlink creation changes ctime and link count for every source alias.
    return (snapshot.st_dev, snapshot.st_ino, snapshot.st_size,
            snapshot.st_mtime_ns, snapshot.st_mode, snapshot.st_uid, snapshot.st_gid)


def regular_file(snapshot, path):
    if not stat.S_ISREG(snapshot.st_mode):
        raise HardlinkError(f"Source is no longer a regular file: {str(path)!r}")


@dataclass
class Plan:
    source: Path
    destination: Path
    directories: dict = field(default_factory=dict)
    files: list = field(default_factory=list)
    file_snapshots: dict = field(default_factory=dict)
    missing_directories: set = field(default_factory=set)
    skipped: int = 0
    already_linked: int = 0
    planned_links: int = 0
    created_links: int = 0
    created_directories: int = 0


@contextmanager
def directory_fd(path, plan=None, verbose=False):
    """Open a directory from /, rejecting symlinks in each path component.

    Supplying a plan enables creation of missing destination components.
    """
    fd = os.open("/", DIRECTORY_FLAGS)
    current = Path("/")
    try:
        for component in path.parts[1:]:
            current = current / component
            try:
                next_fd = os.open(component, DIRECTORY_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if plan is None:
                    raise
                try:
                    os.mkdir(component, dir_fd=fd)
                except FileExistsError:
                    # A concurrent creator still has to pass O_NOFOLLOW/O_DIRECTORY.
                    pass
                else:
                    plan.created_directories += 1
                    if verbose:
                        print(f"Created directory: {str(current)!r}", flush=True)
                next_fd = os.open(component, DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


@contextmanager
def source_directory_fd(root_fd, relative, expected_directories):
    fd = os.dup(root_fd)
    walked = Path(".")
    try:
        if identity(os.fstat(fd)) != identity(expected_directories[walked]):
            raise HardlinkError("Source root changed")
        for component in relative.parts:
            next_fd = os.open(component, DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            walked = walked / component
            if identity(os.fstat(fd)) != identity(expected_directories[walked]):
                raise HardlinkError(f"Source directory changed: {str(walked)!r}")
        yield fd
    finally:
        os.close(fd)


def inspect_destination_directory(path):
    """Return the nearest existing ancestor's device and missing directory paths."""
    current = Path("/")
    missing = []
    fd = os.open("/", DIRECTORY_FLAGS)
    try:
        for component in path.parts[1:]:
            current = current / component
            if missing:
                missing.append(current)
                continue
            try:
                next_fd = os.open(component, DIRECTORY_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                missing.append(current)
                continue
            os.close(fd)
            fd = next_fd
        return os.fstat(fd).st_dev, missing
    finally:
        os.close(fd)


def scan_source(plan, root_fd):
    """Build a fixed inventory, with directory identities checked during traversal."""
    plan.directories[Path(".")] = os.fstat(root_fd)
    pending = [Path(".")]
    while pending:
        relative = pending.pop()
        with source_directory_fd(root_fd, relative, plan.directories) as parent:
            with os.scandir(parent) as iterator:
                names = sorted(entry.name for entry in iterator)
            children = []
            for name in names:
                item = relative / name
                snapshot = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if stat.S_ISDIR(snapshot.st_mode):
                    plan.directories[item] = snapshot
                    children.append(item)
                elif stat.S_ISREG(snapshot.st_mode):
                    key = identity(snapshot)
                    previous = plan.file_snapshots.get(key)
                    if previous is not None and file_state(previous) != file_state(snapshot):
                        raise HardlinkError(f"Source changed during inventory: {str(plan.source / item)!r}")
                    plan.file_snapshots[key] = snapshot
                    plan.files.append((item, key))
                else:
                    plan.skipped += 1
            pending.extend(reversed(children))


def checked_source(plan, root_fd, relative, key):
    with source_directory_fd(root_fd, relative.parent, plan.directories) as parent:
        actual = os.stat(relative.name, dir_fd=parent, follow_symlinks=False)
    regular_file(actual, plan.source / relative)
    if file_state(actual) != file_state(plan.file_snapshots[key]):
        raise HardlinkError(f"Source file changed: {str(plan.source / relative)!r}")
    return actual


def destination_file(plan, relative, source_snapshot):
    target = plan.destination / relative
    parent_device, missing = inspect_destination_directory(target.parent)
    plan.missing_directories.update(missing)
    if not missing:
        with directory_fd(target.parent) as parent:
            try:
                existing = os.stat(target.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
        if existing is not None:
            if stat.S_ISREG(existing.st_mode) and identity(existing) == identity(source_snapshot):
                return True
            raise HardlinkError(f"Destination conflict: {str(target)!r}")
    if parent_device != source_snapshot.st_dev:
        raise HardlinkError(f"Source and destination are on different filesystems: {str(target)!r}")
    return False


def preflight(plan, root_fd):
    for relative in plan.directories:
        with source_directory_fd(root_fd, relative, plan.directories):
            pass
        _, missing = inspect_destination_directory(plan.destination / relative)
        plan.missing_directories.update(missing)
    for relative, key in plan.files:
        source_snapshot = checked_source(plan, root_fd, relative, key)
        if destination_file(plan, relative, source_snapshot):
            plan.already_linked += 1
        else:
            plan.planned_links += 1


def apply_plan(plan, root_fd, verbose):
    # A root renamed or substituted after preflight must still match its pinned FD.
    with directory_fd(plan.source) as current_root:
        if identity(os.fstat(current_root)) != identity(os.fstat(root_fd)):
            raise HardlinkError("Source root path changed after preflight")
    for relative in plan.directories:
        with source_directory_fd(root_fd, relative, plan.directories):
            pass
        with directory_fd(plan.destination / relative, plan=plan, verbose=verbose):
            pass
    for relative, key in plan.files:
        source_path = plan.source / relative
        target_path = plan.destination / relative
        with source_directory_fd(root_fd, relative.parent, plan.directories) as source_parent:
            before = os.stat(relative.name, dir_fd=source_parent, follow_symlinks=False)
            regular_file(before, source_path)
            if file_state(before) != file_state(plan.file_snapshots[key]):
                raise HardlinkError(f"Source file changed: {str(source_path)!r}")
            if destination_file(plan, relative, before):
                continue
            with directory_fd(target_path.parent) as destination_parent:
                os.link(relative.name, target_path.name, src_dir_fd=source_parent,
                        dst_dir_fd=destination_parent, follow_symlinks=False)
                plan.created_links += 1
                linked = os.stat(target_path.name, dir_fd=destination_parent, follow_symlinks=False)
                after = os.stat(relative.name, dir_fd=source_parent, follow_symlinks=False)
                if (not stat.S_ISREG(linked.st_mode) or identity(linked) != key
                        or stable_file_state(after) != stable_file_state(before)):
                    raise HardlinkError(f"Post-link verification failed. Inspect created entry: {str(target_path)!r}")
                plan.file_snapshots[key] = after
            if verbose:
                print(f"Linked {str(source_path)!r} -> {str(target_path)!r}", flush=True)


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-s", "--source", required=True, help="existing source directory")
    parser.add_argument("-d", "--destination", required=True, help="destination directory, created as needed")
    parser.add_argument("-n", "--dry-run", action="store_true", help="validate and display planned changes")
    parser.add_argument("-y", "--yes", action="store_true", help="apply without typing yes")
    parser.add_argument("-v", "--verbose", action="store_true", help="print created directories and links")
    return parser.parse_args(argv)


def interrupted(signum, frame):
    raise KeyboardInterrupt(signum)


def main(argv=None):
    args = arguments(argv)
    phase = "preflight"
    plan = None
    signal.signal(signal.SIGTERM, interrupted)
    try:
        source = Path(args.source).resolve(strict=True)
        destination = Path(args.destination).resolve(strict=False)
        if not source.is_dir():
            raise HardlinkError("Source must be an existing directory")
        if source == Path("/") or destination == Path("/"):
            raise HardlinkError("Filesystem root cannot be used as either tree")
        if source == destination or source in destination.parents or destination in source.parents:
            raise HardlinkError("Source and destination must be separate, non-overlapping trees")
        if destination.is_dir() and os.path.samefile(source, destination):
            raise HardlinkError("Source and destination refer to the same directory")
        plan = Plan(source, destination)
        with directory_fd(source) as root_fd:
            scan_source(plan, root_fd)
            preflight(plan, root_fd)
            print(f"Source: {str(source)!r}\nDestination: {str(destination)!r}", flush=True)
            print(f"Plan: {plan.planned_links} links, {len(plan.missing_directories)} directories. "
                  f"Already linked: {plan.already_linked}. Skipped symlinks/special entries: {plan.skipped}.", flush=True)
            if args.dry_run:
                for path in sorted(plan.missing_directories, key=lambda value: (len(value.parts), str(value))):
                    print(f"DRY RUN mkdir: {str(path)!r}")
                for relative, key in plan.files:
                    current = checked_source(plan, root_fd, relative, key)
                    if not destination_file(plan, relative, current):
                        print(f"DRY RUN hardlink: {str(source / relative)!r} -> {str(destination / relative)!r}")
                print("Dry run complete.")
                return 0
            if not args.yes and (plan.planned_links or plan.missing_directories):
                confirmation = input("Hardlinks share source contents and metadata. Type yes to apply: ")
                if confirmation != "yes":
                    raise HardlinkError("Operation cancelled")
            phase = "execution"
            apply_plan(plan, root_fd, args.verbose)
        print(f"Complete: {plan.created_links} hardlinks and {plan.created_directories} directories created. "
              f"Previously linked: {plan.already_linked}. Skipped entries: {plan.skipped}.", flush=True)
        return 0
    except KeyboardInterrupt as exc:
        signum = exc.args[0] if exc.args else signal.SIGINT
        print("Interrupted.", file=sys.stderr)
        if phase == "execution" and plan is not None:
            print(f"Created before interruption: {plan.created_links} links, {plan.created_directories} directories.", file=sys.stderr)
        return 143 if signum == signal.SIGTERM else 130
    except (OSError, RuntimeError, HardlinkError, EOFError) as exc:
        print(f"Error during {phase}: {str(exc)!r}", file=sys.stderr)
        if phase == "execution" and plan is not None:
            print(f"Created before failure: {plan.created_links} links, {plan.created_directories} directories. "
                  "Inspect these entries before retrying.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
