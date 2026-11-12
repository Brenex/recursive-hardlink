#!/bin/bash
# Recursively hardlink regular files, recreating directories (including empty ones).
# Requires Bash 4+ and GNU findutils/coreutils, as supplied on Linux/Unraid.
#
# Existing paths are never replaced. Already-correct hardlinks are skipped.
# Source symlinks and special files are skipped; destination symlinks are refused.
# Directory permissions/timestamps are not copied. New directories use your umask.
# Hardlinks share file contents and metadata: editing either name edits the same file.
# Keep both trees stable while running. Preflight is not a lock or a transaction;
# an execution-time failure can leave directories and links already created.
# A matching device ID is necessary but not sufficient on pooled/virtual filesystems;
# ln is authoritative. No copy fallback is used if linking fails.

set -euo pipefail
export LC_ALL=C

usage() {
    cat <<'USAGE'
Usage: recursive-hardlink.sh -s SOURCE -d DESTINATION [-n] [-y] [-v]

  -s SOURCE       Existing source directory; its contents go directly into DESTINATION.
  -d DESTINATION  Destination directory (created if needed).
  -n             Dry run: validate and show planned changes without creating links/directories.
  -y             Apply without the interactive confirmation (for scheduled jobs).
  -v             Print each directory/link during execution.
  -h             Show this help.

Without -n, changes require typing yes unless -y is supplied.
Conflicts abort preflight before destination changes. Existing correct hardlinks
are skipped; unrelated destination contents are left alone. Overlapping source
and destination trees are refused. Symlinks and special source files are skipped.
Hardlinks require filesystem support and share contents/metadata with the source.
USAGE
}

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
fail_path() { printf 'Error: %s: %q\n' "$1" "$2" >&2; exit 1; }
action() { printf '%s %q\n' "$1" "$2"; }

source_arg=''
dest_arg=''
dry_run=false
assume_yes=false
verbose=false
while getopts ':s:d:nyvh' opt; do
    case "$opt" in
        s) source_arg=$OPTARG ;;
        d) dest_arg=$OPTARG ;;
        n) dry_run=true ;;
        y) assume_yes=true ;;
        v) verbose=true ;;
        h) usage; exit 0 ;;
        :) die "Option -$OPTARG requires an argument." ;;
        \?) die "Unknown option: -$OPTARG (use -h for help)." ;;
    esac
done
shift "$((OPTIND - 1))"
[[ $# -eq 0 ]] || die 'Unexpected positional arguments (use -h for help).'
[[ -n $source_arg && -n $dest_arg ]] || die 'Both -s and -d are required.'
for dependency in realpath find stat mktemp mkdir ln rm; do
    command -v "$dependency" >/dev/null 2>&1 || die "Required command missing: $dependency"
done

# NUL output preserves even directory names ending in newlines. Resolve aliases
# before overlap checks. Missing destination components are allowed.
IFS= read -r -d '' source_dir < <(realpath -e -z -- "$source_arg") || die 'Cannot resolve source.'
IFS= read -r -d '' dest_dir < <(realpath -m -z -- "$dest_arg") || die 'Cannot resolve destination.'
[[ -d $source_dir ]] || fail_path 'Source is not a directory' "$source_dir"
[[ $source_dir != / && $dest_dir != / ]] || die 'Filesystem root cannot be used as either tree.'
if [[ $source_dir == "$dest_dir" || $dest_dir == "$source_dir/"* || $source_dir == "$dest_dir/"* ]]; then
    die 'Source and destination must be separate, non-overlapping directory trees.'
fi

# Check every ancestor, not just the immediate parent. This prevents an existing
# destination subdirectory symlink from redirecting operations outside the tree.
# The command-line destination itself is resolved above, so a root alias is allowed.
check_parents() {
    local path=$1
    device_anchor=''
    while :; do
        [[ ! -L $path ]] || fail_path 'Destination ancestor is a symlink' "$path"
        if [[ -e $path ]]; then
            [[ -d $path ]] || fail_path 'Destination ancestor is not a directory' "$path"
            if [[ -z $device_anchor ]]; then device_anchor=$path; fi
        fi
        [[ $path != / ]] || break
        path=${path%/*}
        [[ -n $path ]] || path=/
    done
}

check_directory() {
    local path=$1
    check_parents "$path"
}

# Sets already_linked for the caller. Never follows a destination leaf symlink.
check_file() {
    local src=$1 dst=$2 src_device dst_device
    already_linked=false
    [[ ! -L $src && -f $src ]] || fail_path 'Source is no longer a regular file' "$src"
    check_parents "${dst%/*}"
    [[ ! -L $dst ]] || fail_path 'Destination file is a symlink' "$dst"
    if [[ -e $dst ]]; then
        if [[ -f $dst && $src -ef $dst ]]; then
            already_linked=true
            return
        fi
        fail_path 'Destination conflict (nothing overwritten)' "$dst"
    fi
    src_device=$(stat -c '%d' -- "$src") || fail_path 'Cannot stat source' "$src"
    dst_device=$(stat -c '%d' -- "$device_anchor") || fail_path 'Cannot stat destination filesystem' "$device_anchor"
    [[ $src_device == "$dst_device" ]] || fail_path 'Source and destination are on different filesystems' "$src"
}

manifest=$(mktemp "${TMPDIR:-/tmp}/recursive-hardlink.XXXXXXXX") || die 'Cannot create scan manifest.'
trap 'rm -f -- "$manifest"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Snapshot names and types before any destination mutation; check find's own exit
# status so unreadable subtrees cannot be silently reported as a successful scan.
if ! find -P "$source_dir" -mindepth 1 -printf '%y\0%p\0' > "$manifest"; then
    die 'Source scan failed; destination was not changed.'
fi

planned_links=0
existing_links=0
planned_dirs=0
skipped=0
check_directory "$dest_dir"
if [[ ! -d $dest_dir ]]; then planned_dirs=$((planned_dirs + 1)); fi
while IFS= read -r -d '' kind && IFS= read -r -d '' item; do
    relative=${item#"$source_dir/"}
    target=$dest_dir/$relative
    case "$kind" in
        d)
            [[ ! -L $item && -d $item ]] || fail_path 'Source directory changed during scan' "$item"
            check_directory "$target"
            if [[ ! -d $target ]]; then planned_dirs=$((planned_dirs + 1)); fi
            ;;
        f)
            check_file "$item" "$target"
            if $already_linked; then
                existing_links=$((existing_links + 1))
            else
                planned_links=$((planned_links + 1))
            fi
            ;;
        *) skipped=$((skipped + 1)) ;;
    esac
done < "$manifest"

printf 'Source: %q\nDestination: %q\n' "$source_dir" "$dest_dir"
printf 'Plan: %d links, %d directories; %d already linked; %d symlinks/special entries skipped.\n' \
    "$planned_links" "$planned_dirs" "$existing_links" "$skipped"
if ! $dry_run && ! $assume_yes && (( planned_links + planned_dirs > 0 )); then
    printf 'Hardlinks share file contents and metadata with the source. Type yes to apply: '
    if ! IFS= read -r confirmation || [[ $confirmation != yes ]]; then
        die 'Cancelled; destination was not changed.'
    fi
fi

ensure_directory() {
    local path=$1
    check_directory "$path"
    if [[ ! -d $path ]]; then
        if $dry_run; then
            action 'DRY RUN mkdir -p' "$path"
        else
            mkdir -p -- "$path" || fail_path 'Cannot create destination directory' "$path"
            if $verbose; then action 'Directory' "$path"; fi
        fi
    fi
}

created_links=0
ensure_directory "$dest_dir"
while IFS= read -r -d '' kind && IFS= read -r -d '' item; do
    relative=${item#"$source_dir/"}
    target=$dest_dir/$relative
    case "$kind" in
        d)
            [[ ! -L $item && -d $item ]] || fail_path 'Source directory changed during execution' "$item"
            ensure_directory "$target"
            ;;
        f)
            check_file "$item" "$target"
            if $already_linked; then continue; fi
            if $dry_run; then
                printf 'DRY RUN ln -- %q %q\n' "$item" "$target"
            else
                # -T prevents interpreting a newly appearing directory as a link
                # container. -P avoids dereferencing a newly appearing symlink.
                ln -P -T -- "$item" "$target" || fail_path 'Hardlink failed; earlier changes remain' "$target"
                created_links=$((created_links + 1))
                if $verbose; then printf 'Linked %q -> %q\n' "$item" "$target"; fi
            fi
            ;;
    esac
done < "$manifest"

if $dry_run; then
    printf 'Dry run complete; no destination changes made.\n'
else
    printf 'Complete: %d hardlinks created; %d were already linked; %d entries skipped.\n' \
        "$created_links" "$existing_links" "$skipped"
fi
