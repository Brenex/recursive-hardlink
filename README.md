# hardlink-matches

Restore hardlink relationships between torrent files and their organized media-library copies on Unraid.

`hardlink-matches.py` recursively matches files in `/mnt/user/data/torrents/` against files anywhere under `/mnt/user/data/media/`. Matching uses file contents, so renamed files and different folder layouts are supported. A saved plan lets you review candidates before replacing media entries with hardlinks to the retained torrent files.

Repair candidates require both paths to exist and contain identical bytes. This covers independent copies left by copy-based imports, restores, or other operations that broke a previous hardlink relationship.

## Scope

| Tree | Default path | Role |
| --- | --- | --- |
| Torrent files | `/mnt/user/data/torrents/` | Retained source files |
| Organized media | `/mnt/user/data/media/` | Existing files eligible for hardlink repair |

The media scan includes every subfolder, including `anime movies`, `anime tv shows`, `audiobooks`, `cartoons`, `movies`, `music`, `tv`, `videos`, and `youtube`. All regular files are eligible, including sidecar files and empty files. Symlinks and special files are skipped.

For example, these paths can refer to the same file despite their different directory structures.

```text
/mnt/user/data/torrents/movies/Primetime.2026.1080p.AMZN.WEB-DL.DDP5.1.Atmos.H.264-CHORTLE.mkv
/mnt/user/data/media/movies/Primetime (2026)/Primetime.2026.1080p.AMZN.WEB-DL.DDP5.1.Atmos.H.264-CHORTLE.mkv
```

An existing match of device ID and inode is reported as `ALREADY_LINKED`. Separate files with identical contents proceed through the repair eligibility checks.

### Relationship to unraid-cross-disk-deduplicator

| Tool | Purpose |
| --- | --- |
| `hardlink-matches` | Repair torrent-to-media hardlink relationships through the two specified directory trees. |
| `unraid-cross-disk-deduplicator` | Consolidate identical files from different physical array disks onto a retained source disk. |

Physical disk consolidation belongs to the cross-disk tool. `hardlink-matches` relies on the filesystem accepting a hardlink at the existing media location. On Unraid, a shared `/mnt/user` namespace can span multiple underlying filesystems, so matching device IDs alone are insufficient to guarantee successful linking.

## Requirements

- Linux, including Unraid.
- Python 3.9 or newer with its standard library, including `sqlite3`.
- Permission to read both trees and create and replace entries in the media directories.
- A filesystem configuration that supports hardlinks for the selected paths.

The application is a standalone Python script. Run it directly on the Unraid host with `python3`.

## Installation

Place `hardlink-matches.py` and this README in a working directory outside both scanned trees. The examples below use `/mnt/user/scripts/hardlink-matches/`.

```bash
mkdir -p /mnt/user/scripts/hardlink-matches
cd /mnt/user/scripts/hardlink-matches
python3 --version
```

Save the script in that directory before continuing. If you already keep it in `/mnt/user/scripts/recursive-hardlink/`, use that directory instead.

## Quick start

### 1. Scan both trees

```bash
python3 hardlink-matches.py scan \
  --torrents /mnt/user/data/torrents \
  --media /mnt/user/data/media \
  --plan hardlink-plan.json
```

The scan reads the torrent and media files and writes a plan and hash cache outside those trees. The terminal summary reports existing hardlinks, repair candidates, review cases, and unmatched files.

These are also the default roots and plan filename, so the equivalent command is shorter.

```bash
python3 hardlink-matches.py scan
```

Large matching files require full reads for SHA-256 verification. Progress messages appear during long hashes. Completed hashes are cached for subsequent scans.

### 2. Review the plan

```bash
python3 hardlink-matches.py show --plan hardlink-plan.json --limit 20
```

The default view shows `READY`, review, cross-filesystem, and error entries. To inspect existing hardlinks or select particular statuses, use repeatable `--status` options.

```bash
python3 hardlink-matches.py show --status ALREADY_LINKED --limit 50
python3 hardlink-matches.py show --status READY --status REVIEW_METADATA --limit 50
```

### 3. Apply a small initial batch

Pause downloads, Sonarr/Radarr imports, Unraid Mover, balancing, and other applications that can write or relocate the participating files. Keep the trees stable throughout application.

```bash
python3 hardlink-matches.py apply --plan hardlink-plan.json --limit 5
```

Type `LINK` when prompted. Each selected candidate receives a fresh byte-for-byte comparison and SHA-256 check before replacement.

### 4. Apply the full plan

```bash
python3 hardlink-matches.py apply --plan hardlink-plan.json
```

Previously repaired entries are recognized as `ALREADY_LINKED` when the plan is rerun. `--limit` selects the first N `READY` rows in the saved plan, including rows repaired in a previous run. Repeating `--limit 5` revisits the same five rows. Omit the limit to process the full plan.

For an already reviewed plan in a controlled maintenance window, `--yes` skips the interactive prompt.

```bash
python3 hardlink-matches.py apply --plan hardlink-plan.json --yes
```

## How matching works

1. Recursively inventory regular files in both trees.
2. Identify existing hardlinks by device ID and inode.
3. Group remaining candidates by exact file size.
4. Compare sampled content from the beginning, middle, and end.
5. Compute full SHA-256 hashes for candidates whose samples match.
6. Check ownership, permissions, extended attributes, and existing media link counts.
7. Save one result per media file, including the selected torrent source and matching torrent paths where applicable.

When several torrent copies match, source selection prefers a matching device ID, compatible metadata, a higher link count, and finally the path in lexical order.

## Scan statuses

| Status | Meaning | Apply behavior |
| --- | --- | --- |
| `ALREADY_LINKED` | Media and torrent entries share a device ID and inode. | Already repaired. |
| `READY` | Contents match and the candidate passes the scan's eligibility checks. | Recheck and attempt repair. |
| `REVIEW_METADATA` | Ownership, permissions, or extended attributes differ. | Requires review. |
| `REVIEW_MULTIPLE_LINKS` | The separate media inode already has more than one hardlink. | Requires review of its existing link relationships. |
| `CROSS_FILESYSTEM` | The selected source and media entry have different device IDs. | Requires a compatible storage layout. |
| `NO_SIZE_MATCH` | No torrent file has the same size. | No repair candidate. |
| `NO_CONTENT_MATCH` | Same-size candidates have different sampled content or full hashes. | No repair candidate. |
| `ERROR` | A file changed or another scan/hash operation failed. | The scan is incomplete and application is blocked. |

Permission repair is handled through a separate maintenance process. After resolving a review case, create a fresh scan plan so its recorded file state reflects the correction.

## How application works

For each selected `READY` entry, the script opens the existing torrent and media files through directory descriptors, rejects symlink traversal, and checks their recorded identities and metadata. It then reads both files again, compares every byte, and verifies the content against the scanned SHA-256 hash.

The script creates a temporary hardlink in the media directory, confirms that it refers to the torrent inode, and replaces the media directory entry using `os.replace`. The result retains the organized media pathname while sharing the torrent file's inode.

The torrent's modification time becomes the media file's modification time. Both names then share file contents, permissions, ownership, extended attributes, and timestamps. In-place modifications through either name affect that shared file.

Application records each attempt in an append-only JSONL log. Completed replacements persist if a later operation fails. The advisory lock coordinates this script's apply runs using the same media root. Other applications must be paused separately.

## Generated files

With the default plan filename, the working directory contains the following files.

| File | Purpose |
| --- | --- |
| `hardlink-plan.json` | Scan results, file snapshots, hashes, summary, and repair candidates. |
| `hardlink-hashes.sqlite3` | Persistent cache of sampled and full hashes, keyed by recorded file state. |
| `hardlink-plan.json.apply.jsonl` | Append-only application audit log. |

Keep plans, caches, and logs outside both scanned trees. Plan paths are relative to the current working directory unless an absolute path is supplied. The default cache resides beside the plan, and the application log uses the plan's pathname with `.apply.jsonl` appended.

The reported potential savings are the logical sizes of `READY` duplicate files. Actual recovered disk space depends on filesystem allocation, sparse files, reflinks, snapshots, open files, and successful linking.

### Rescan after changes

Choose a new plan filename for each scan. Existing plans are protected against overwriting. The default hash cache is shared by plans in the same directory, allowing unchanged files to reuse cached results.

```bash
python3 hardlink-matches.py scan --plan hardlink-plan-2.json
python3 hardlink-matches.py show --plan hardlink-plan-2.json
python3 hardlink-matches.py apply --plan hardlink-plan-2.json
```

### Custom roots and cache

```bash
python3 hardlink-matches.py scan \
  --torrents /mnt/user/data/torrents/movies \
  --media /mnt/user/data/media/movies \
  --plan movies-plan.json \
  --cache movies-hashes.sqlite3
```

The roots must be existing, distinct, non-overlapping directories.

## Command reference

| Command | Option | Default | Purpose |
| --- | --- | --- | --- |
| `scan` | `--torrents PATH` | `/mnt/user/data/torrents` | Torrent source tree. |
| `scan` | `--media PATH` | `/mnt/user/data/media` | Organized media tree. |
| `scan` | `--plan FILE` | `hardlink-plan.json` | New plan filename. |
| `scan` | `--cache FILE` | `hardlink-hashes.sqlite3` beside the plan | Persistent hash cache. |
| `show` | `--plan FILE` | `hardlink-plan.json` | Plan to inspect. |
| `show` | `--limit N` | `20` | Maximum number of selected rows displayed. |
| `show` | `--status STATUS` | Ready, review, cross-filesystem, and error entries | Repeatable status filter. |
| `apply` | `--plan FILE` | `hardlink-plan.json` | Plan to apply. |
| `apply` | `--limit N` | All saved `READY` rows | Maximum candidate rows selected. |
| `apply` | `--yes` | Interactive confirmation | Skip the `LINK` prompt. |

Use `--help` for the complete application description or command-specific help.

```bash
python3 hardlink-matches.py --help
python3 hardlink-matches.py scan --help
python3 hardlink-matches.py show --help
python3 hardlink-matches.py apply --help
```

## Troubleshooting and recovery

| Result | Action |
| --- | --- |
| Plan already exists | Select a new `--plan` filename. |
| File changed since scan/check | Stabilize the participating files and create a fresh plan. |
| Incomplete scan | Resolve the recorded scan errors, then rescan to a new plan. |
| `REVIEW_METADATA` | Inspect ownership, permissions, and extended attributes, correct them as appropriate, then rescan. |
| `REVIEW_MULTIPLE_LINKS` | Inspect the media file's other hardlink relationships before choosing a repair strategy. |
| `LINK_UNSUPPORTED` during apply | Inspect filesystem compatibility and Unraid share placement. The failed link attempt leaves the existing media entry in place. |
| `APPLIED_NEEDS_REVIEW` | Replacement occurred, but a subsequent check failed. Inspect that file's inode and the audit log before continuing. |
| `LINKED` with a directory-fsync warning | The link was created and verified. Directory synchronization reported a filesystem error or unsupported operation. Inspect the logged detail. |
| Interrupted run | Inspect the audit log and participating paths before rerunning the saved plan. |

A crash or abrupt termination may leave a `.hardlink-match-*.tmp` entry in a media directory. Inspect its inode together with the torrent and final media paths before removing a leftover temporary entry. A `STARTED` audit event without a corresponding result requires checking the current filesystem state.

### Verify a repaired pair

```bash
stat -c 'device=%d inode=%i links=%h path=%n' \
  '/mnt/user/data/torrents/movies/Primetime.2026.1080p.AMZN.WEB-DL.DDP5.1.Atmos.H.264-CHORTLE.mkv' \
  '/mnt/user/data/media/movies/Primetime (2026)/Primetime.2026.1080p.AMZN.WEB-DL.DDP5.1.Atmos.H.264-CHORTLE.mkv'
```

Matching device IDs and inode numbers identify the shared file. The link count normally reflects at least the two directory entries in this example.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Command completed successfully, including runs with no eligible work. Review statuses may still be present. |
| `1` | A handled command-level error, such as an existing plan, rejected path, or cancelled confirmation. |
| `2` | Incomplete scan, apply entry errors, unsupported links, or an applied entry requiring review. Argument parsing errors also use this code. |
| `130` | Interrupted by `Ctrl+C`. Inspect the audit log for any completed replacements. |
