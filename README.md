# hardlink-matches

Recursively find identical files in two directory trees and replace eligible target copies with hardlinks to retained master files.

The script accepts a master directory and a target directory. It compares file contents regardless of filenames or folder layouts. Existing target paths become hardlinks to their matching master files, reducing duplicate storage.

## Requirements

- Linux, including Unraid.
- Python 3.9 or newer with its standard library.
- Permission to read both directory trees and replace target entries.
- Existing, separate directory roots that do not overlap.

Save `hardlink-matches.py` in your scripts directory. Your existing `/mnt/user/scripts/recursive-hardlink/` directory is suitable.

## Usage

Preview the matches and proposed replacements.

```bash
cd /mnt/user/scripts/recursive-hardlink

python3 hardlink-matches.py \
  --master /mnt/user/data/torrents \
  --target /mnt/user/data/media
```

Review the output, pause downloads, media imports, Mover, and other writers, then apply the replacements.

```bash
python3 hardlink-matches.py \
  --master /mnt/user/data/torrents \
  --target /mnt/user/data/media \
  --apply
```

`--apply` authorizes replacements directly. Each invocation builds a fresh inventory and verifies its matches. Hashes are cached in memory for that run.

| Argument | Purpose |
| --- | --- |
| `--master DIRECTORY` | Required. Recursively scanned directory supplying retained files. |
| `--target DIRECTORY` | Required. Recursively scanned directory containing existing duplicates to replace. |
| `--apply` | Apply eligible replacements. The default is a dry run. |
| `-h`, `--help` | Display help. |

This unified interface replaces the earlier two-script workflow and its scan, show, and apply subcommands. Update scheduled invocations to use the directory arguments above.

## How it works

1. Recursively inventory regular files in both directory trees.
2. Recognize existing hardlinks using device IDs and inode numbers.
3. Compare remaining candidates by size, sampled content, and full SHA-256 hashes.
4. Select a matching master file with compatible ownership, permissions, and extended attributes on a compatible device.
5. Display each proposed replacement during a dry run.
6. With `--apply`, read both files again, compare every byte, and verify the scanned SHA-256 hash.
7. Create and verify a temporary hardlink in the target directory, then atomically replace the target entry and check the resulting inode.

For example, the following files can be matched even though they have different names and locations.

```text
Master: /mnt/user/data/torrents/movies/Release.Name.mkv
Target: /mnt/user/data/media/movies/Movie (2026)/Renamed.Movie.mkv
```

After a successful replacement, these paths refer to the same inode. Files throughout every target subdirectory are considered, including movies, TV, anime, music, audiobooks, and sidecar files.

If several master files qualify, selection prefers the one with the most hardlinks, then its path in lexical order.

## Results and skipped files

The script prints progress, proposed or completed replacements, skip reasons, and a final summary.

| Summary field | Meaning |
| --- | --- |
| `eligible` | Target files that passed content and compatibility checks. |
| `linked` | Target entries replaced during this run. |
| `already_linked` | Target files already sharing an inode with a master file. |
| `unmatched` | Target files with no identical master copy. |
| `metadata_mismatch` | Matching contents with differing ownership, permissions, or extended attributes. |
| `target_has_other_links` | Separate target inodes that already have other hardlinks. |
| `different_filesystem` | Matching contents with different filesystem device IDs. |
| `unsupported_link` | Actual hardlink creation was rejected by the filesystem during application. |
| `symlinks_or_special` | Symlinks and special entries skipped across both trees. |

Only applicable fields appear in the summary. Metadata conflicts require review and correction before rerunning. Targets already linked elsewhere are conservatively skipped to preserve their existing link relationships. Symlinked directories are skipped with their contents.

On Unraid, `/mnt/user` can aggregate files from multiple underlying filesystems. Actual link creation remains the final compatibility check, including cases where the presented device IDs match. An unsupported link attempt leaves the existing target entry in place. Physical disk consolidation is handled separately by `unraid-cross-disk-deduplicator`.

## Operational notes

- Pause file writers and Mover while applying. The advisory lock coordinates apply runs using the same target root.
- The master file supplies the shared contents, permissions, ownership, extended attributes, and timestamps. Its modification time becomes the target's modification time.
- Editing either hardlink in place affects the shared file.
- Complete inventory and content matching precede replacements. Scan or hash errors stop that run before application.
- A later execution failure can leave earlier replacements completed. Inspect the reported count and error before rerunning. Successful reruns recognize existing hardlinks automatically.
- A crash may leave a `.hardlink-match-*.tmp` file in a target directory. Inspect its inode and the associated final paths before removing a leftover temporary entry.
- Potential savings are reported as logical duplicate data. Actual recovered space depends on sparse files, reflinks, snapshots, open files, and successful linking.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Scan/application completed. Review or compatibility skips can still be present in the summary. |
| `1` | Filesystem/input error, interrupted application by an error, or an actual unsupported link attempt. |
| `2` | Missing or invalid command-line arguments. |
| `130` | Interrupted by `Ctrl+C` or `SIGTERM`. Earlier completed replacements remain in place. |
