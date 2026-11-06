"""
Git worktree management for branch-based testing.

When a branch already has a worktree checked out (e.g. created by the
ticket system), that existing worktree is reused rather than creating a
duplicate.  If no worktree exists yet, a new one is created under
``aiarena/worktrees/<sanitized_branch>/``.

``git worktree add`` does not check out the submodules of the repo, so a
bot that imports a pinned submodule crashes on import when it is started
from a fresh worktree.  Every worktree handed out by this module therefore
gets its submodules checked out as well (see :func:`ensure_submodules`).

Callers are responsible for cleanup via ``remove_worktree()`` when a
branch is no longer needed.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess

logger = logging.getLogger('test_lab')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT_DIR)))
# Deployment override: the default assumes the original chadspratt layout where
# _REPO_ROOT is writable by the app user. On hosts where it is not (e.g. the
# forge layout where /mnt/forge is root-owned), set TEST_LAB_WORKTREE_BASE_DIR
# to a writable directory; worktrees for branch-based testing live there.
WORKTREE_BASE_DIR = os.environ.get(
    'TEST_LAB_WORKTREE_BASE_DIR',
    os.path.join(_REPO_ROOT, 'bot', 'worktrees'),
)

#: Timeout (seconds) for checking out the submodules of a worktree.  A
#: submodule is a full clone, so this is generous — but it must not block
#: the caller forever.
SUBMODULE_TIMEOUT = 300


class WorktreeError(Exception):
    """Raised when a worktree could not be prepared for a run."""


class SubmoduleError(WorktreeError):
    """Raised when the submodules of a worktree could not be checked out."""


def _sanitize_branch_name(branch: str) -> str:
    """Convert a branch name to a safe directory name.

    Replaces slashes and other unsafe characters with underscores.
    """
    return re.sub(r'[^a-zA-Z0-9._-]', '_', branch)


def _validate_branch_name(branch: str) -> bool:
    """Basic validation that the branch name looks reasonable.

    Rejects empty strings, strings with shell metacharacters, etc.
    """
    if not branch or len(branch) > 200:
        return False
    # Reject obvious shell injection attempts
    if any(c in branch for c in ';|&$`\\\'\"<>(){}'):
        return False
    return True


def get_worktree_path(repo_path: str, branch: str) -> str:
    """Return the worktree directory path for a branch (may not exist yet)."""
    safe_name = _sanitize_branch_name(branch)
    return os.path.join(WORKTREE_BASE_DIR, safe_name)


def _find_existing_worktree(repo_path: str, branch: str) -> str | None:
    """Return the path of an existing worktree for *branch*, or ``None``.

    Checks all worktrees registered with the repository at *repo_path*
    (e.g. those created by the ticket system) so we can reuse them
    instead of creating a conflicting second checkout.
    """
    for wt in list_worktrees(repo_path):
        wt_branch = wt.get('branch', '')
        if wt_branch == f'refs/heads/{branch}' or wt_branch == branch:
            return wt['path']
    return None


def list_submodule_paths(repo_path: str) -> list[str]:
    """Return the submodule paths of the checkout at *repo_path*.

    The paths are read from the index of the checked out commit (a
    submodule is a gitlink, mode ``160000``), so they describe exactly
    the submodules git manages for that commit.  Entries that are only
    mentioned in ``.gitmodules`` without a gitlink are ignored — git
    does not manage them either.
    """
    result = subprocess.run(
        ['git', 'ls-files', '--stage'],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return []

    paths: list[str] = []
    for line in result.stdout.splitlines():
        meta, _, path = line.partition('\t')
        fields = meta.split()
        if len(fields) >= 2 and fields[0] == '160000' and path:
            paths.append(path)
    return paths


def missing_submodules(worktree_path: str, submodule_paths: list[str]) -> list[str]:
    """Return the submodule paths that are not checked out in *worktree_path*.

    A submodule counts as checked out when its directory holds content.
    A missing or empty directory means the submodule was never checked
    out (which is what ``git worktree add`` leaves behind).
    """
    missing = []
    for path in submodule_paths:
        target = os.path.join(worktree_path, path)
        if not os.path.isdir(target):
            missing.append(path)
            continue
        try:
            entries = [
                entry for entry in os.listdir(target) if entry != '.git'
            ]
        except OSError:
            entries = []
        if not entries:
            missing.append(path)
    return missing


def ensure_submodules(worktree_path: str) -> bool:
    """Make sure the submodules of *worktree_path* are checked out.

    ``git worktree add`` does not check out submodules, so a repo with
    pinned submodules would crash on import when the bot is started from
    a fresh worktree.  The checkout is skipped when the submodules are
    already there, so reusing a worktree stays cheap.

    Returns True when a submodule checkout was performed.
    Raises :class:`SubmoduleError` when the checkout fails.
    """
    submodule_paths = list_submodule_paths(worktree_path)
    if not submodule_paths:
        return False

    missing = missing_submodules(worktree_path, submodule_paths)
    if not missing:
        logger.debug('Submodules of %s are already checked out', worktree_path)
        return False

    logger.info(
        'Checking out submodules in %s: %s', worktree_path, ', '.join(missing),
    )
    try:
        result = subprocess.run(
            ['git', 'submodule', 'update', '--init', '--recursive'],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=SUBMODULE_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise SubmoduleError(
            f'Timed out after {SUBMODULE_TIMEOUT}s while checking out the '
            f'submodules of {worktree_path}'
        ) from exc
    except OSError as exc:
        raise SubmoduleError(
            f'Could not run git to check out the submodules of '
            f'{worktree_path}: {exc}'
        ) from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or '').strip()
        raise SubmoduleError(
            f'Failed to check out the submodules of {worktree_path}: {detail}'
        )

    still_missing = missing_submodules(worktree_path, submodule_paths)
    if still_missing:
        raise SubmoduleError(
            f'The submodules of {worktree_path} are still not checked out '
            f'after the update: {", ".join(still_missing)}'
        )
    return True


def get_or_create_worktree(repo_path: str, branch: str) -> str:
    """Return a worktree for *branch*, reusing an existing one if possible.

    If the branch is already checked out in a worktree (e.g. one created
    by the ticket system under the repo's own ``worktrees/`` directory),
    that path is returned directly — no new worktree is created.

    Otherwise a new worktree is created under ``aiarena/worktrees/``.

    The submodules of the worktree are checked out as well, so the bot
    can be started from it (a fresh worktree has no submodules, and a
    bot that imports one would crash on import).

    *repo_path* is the path to the main git repository (e.g. ``bot/``).

    Returns the absolute path to the worktree directory.
    Raises ``ValueError`` if the branch doesn't exist or the repo is invalid.
    Raises :class:`SubmoduleError` if the submodules could not be checked out.
    """
    if not _validate_branch_name(branch):
        raise ValueError(f'Invalid branch name: {branch!r}')

    if not os.path.isdir(repo_path):
        raise ValueError(f'Repository path does not exist: {repo_path}')

    # Verify the branch exists in the repo
    result = subprocess.run(
        ['git', 'rev-parse', '--verify', branch],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise ValueError(
            f'Branch {branch!r} does not exist in {repo_path}: '
            f'{result.stderr.strip()}'
        )

    # Reuse an existing worktree for this branch (e.g. one created by the
    # ticket system) instead of trying to create a second checkout.
    existing = _find_existing_worktree(repo_path, branch)
    if existing:
        logger.info(
            'Using existing worktree for branch %s at %s', branch, existing,
        )
        ensure_submodules(existing)
        return existing

    worktree_path = get_worktree_path(repo_path, branch)

    # If worktree already exists and is valid, just update it
    if os.path.isdir(worktree_path) and os.path.isdir(
        os.path.join(worktree_path, '.git')
    ) or os.path.isfile(os.path.join(worktree_path, '.git')):
        # Pull latest changes for the branch
        try:
            subprocess.run(
                ['git', 'checkout', branch],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            logger.info('Reusing existing worktree for branch %s at %s', branch, worktree_path)
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning('Failed to update worktree for %s: %s', branch, e)
        ensure_submodules(worktree_path)
        return worktree_path

    # Clean up any stale directory that isn't a valid worktree
    if os.path.exists(worktree_path):
        # Prune stale worktree references first
        subprocess.run(
            ['git', 'worktree', 'prune'],
            cwd=repo_path,
            capture_output=True,
            timeout=10,
        )
        # If directory still exists but isn't a worktree, remove it
        if os.path.exists(worktree_path):
            import shutil
            shutil.rmtree(worktree_path)

    os.makedirs(WORKTREE_BASE_DIR, exist_ok=True)

    # Create the worktree
    result = subprocess.run(
        ['git', 'worktree', 'add', worktree_path, branch],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise ValueError(
            f'Failed to create worktree for branch {branch!r}: '
            f'{result.stderr.strip()}'
        )

    logger.info('Created worktree for branch %s at %s', branch, worktree_path)
    ensure_submodules(worktree_path)
    return worktree_path


def remove_worktree(repo_path: str, branch: str) -> bool:
    """Remove a git worktree for *branch*.

    Returns True if the worktree was removed, False if it didn't exist.
    """
    worktree_path = get_worktree_path(repo_path, branch)

    if not os.path.exists(worktree_path):
        return False

    result = subprocess.run(
        ['git', 'worktree', 'remove', worktree_path, '--force'],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        logger.warning(
            'git worktree remove failed for %s: %s',
            worktree_path, result.stderr.strip(),
        )
        # Fall back to manual cleanup
        import shutil
        shutil.rmtree(worktree_path, ignore_errors=True)
        subprocess.run(
            ['git', 'worktree', 'prune'],
            cwd=repo_path,
            capture_output=True,
            timeout=10,
        )

    logger.info('Removed worktree for branch %s', branch)
    return True


def list_worktrees(repo_path: str) -> list[dict[str, str]]:
    """List all git worktrees for a repository.

    Returns a list of dicts with 'path', 'branch', and 'head' keys.
    """
    result = subprocess.run(
        ['git', 'worktree', 'list', '--porcelain'],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return []

    worktrees = []
    current: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if line.startswith('worktree '):
            if current:
                worktrees.append(current)
            current = {'path': line.split(' ', 1)[1]}
        elif line.startswith('HEAD '):
            current['head'] = line.split(' ', 1)[1]
        elif line.startswith('branch '):
            current['branch'] = line.split(' ', 1)[1]
    if current:
        worktrees.append(current)

    return worktrees
