#!/usr/bin/env python3
"""Watch a Git worktree and optionally commit and push changes as they settle."""

from __future__ import annotations

import argparse
import datetime as dt
import os
from pathlib import Path
import subprocess
import sys
import time


IGNORED_PARTS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
}
SENSITIVE_NAMES = {
    ".env",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "credentials",
    "credentials.json",
    "secrets",
    "secrets.json",
}
SENSITIVE_SUFFIXES = {".pem", ".p12", ".pfx", ".key"}


class GitError(RuntimeError):
    """A Git command failed and its diagnostic has already been displayed."""


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        raise GitError(f"git {' '.join(args)} 失敗：{detail}")
    return result


def repository_root(start: Path) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        raise GitError(f"目前目錄不是 Git 專案：{detail}")
    return Path(os.fsdecode(result.stdout.strip())).resolve()


def is_sensitive(path: str) -> bool:
    parts = Path(path).parts
    for part in parts:
        name = part.casefold()
        if name in SENSITIVE_NAMES or name.endswith(SENSITIVE_SUFFIXES):
            return True
        if name.startswith(".env.") and name not in {".env.example", ".env.sample"}:
            return True
        if "secret" in name or "credential" in name:
            return True
    return False


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    result = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    paths = result.stdout.split(b"\0")
    files: dict[str, tuple[int, int]] = {}
    for raw_path in paths:
        relative = os.fsdecode(raw_path)
        path = Path(relative)
        if any(part in IGNORED_PARTS for part in path.parts) or is_sensitive(relative):
            continue
        try:
            info = (root / path).stat()
        except FileNotFoundError:
            continue
        if path.is_file():
            files[relative] = (info.st_mtime_ns, info.st_size)
    return files


def changed_paths(
    previous: dict[str, tuple[int, int]],
    current: dict[str, tuple[int, int]],
) -> set[str]:
    return {
        path
        for path in previous.keys() | current.keys()
        if previous.get(path) != current.get(path)
    }


def task_description(paths: set[str]) -> str:
    for path in sorted(paths):
        parts = Path(path).parts
        if len(parts) >= 4 and parts[:2] == ("tasks", "features"):
            return parts[3][:40]
    if len(paths) == 1:
        return Path(next(iter(paths))).stem[:40] or "檔案更新"
    return "多檔案更新"


def commit_message(paths: set[str]) -> str:
    taipei = dt.timezone(dt.timedelta(hours=8))
    timestamp = dt.datetime.now(taipei).strftime("%Y-%m-%d_%H-%M")
    return f"{timestamp}_{task_description(paths)}"


def index_is_clean(root: Path) -> bool:
    result = git(root, "diff", "--cached", "--quiet", check=False)
    if result.returncode not in (0, 1):
        detail = result.stderr.decode(errors="replace").strip()
        raise GitError(f"無法檢查 Git 暫存區：{detail}")
    return result.returncode == 0


def process_batch(root: Path, paths: set[str], push_enabled: bool) -> None:
    message = commit_message(paths)
    if not push_enabled:
        print(f"[乾跑] 將提交：{message}", flush=True)
        for path in sorted(paths):
            print(f"  - {path}", flush=True)
        return

    if not index_is_clean(root):
        raise GitError("暫存區已有其他變更；為避免混合提交，請先整理暫存區後重新啟動。")

    git(root, "add", "-A", "--", *sorted(paths))
    if index_is_clean(root):
        print("[略過] 變更已不存在或沒有可提交內容。", flush=True)
        return

    staged = {
        os.fsdecode(path)
        for path in git(root, "diff", "--cached", "--name-only", "-z").stdout.split(b"\0")
        if path
    }
    if not staged.issubset(paths):
        raise GitError("暫存內容超出本次監看檔案；已停止，請檢查 Git 暫存區。")

    git(
        root,
        "commit",
        "-m",
        message,
        "-m",
        "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>",
    )
    print(f"[已提交] {message}", flush=True)
    git(root, "push")
    print("[已推送] 已推送至目前分支設定的 upstream。", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="監看 Git 檔案變更；預設乾跑，加入 --push 才會 commit 並 push。"
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="啟用實際 commit 與 push（預設只顯示乾跑結果）",
    )
    parser.add_argument("--interval", type=float, default=1.0, help="掃描間隔秒數，預設 1")
    parser.add_argument("--debounce", type=float, default=3.0, help="停止變更多久後處理，預設 3 秒")
    args = parser.parse_args()
    if args.interval <= 0 or args.debounce < 0:
        parser.error("--interval 必須大於 0，且 --debounce 不可小於 0")

    try:
        root = repository_root(Path.cwd())
        baseline = snapshot(root)
    except GitError as error:
        print(error, file=sys.stderr)
        return 1

    mode = "commit/push" if args.push else "乾跑"
    print(f"[監看中] {root}（{mode}；Ctrl+C 停止）", flush=True)
    if args.push:
        print("[提醒] 只監看啟動後的新變更；推送前請確認目前分支與遠端。", flush=True)

    pending: set[str] = set()
    last_change = 0.0
    try:
        while True:
            time.sleep(args.interval)
            current = snapshot(root)
            differences = changed_paths(baseline, current)
            if differences:
                pending.update(differences)
                last_change = time.monotonic()
                baseline = current
                continue
            if pending and time.monotonic() - last_change >= args.debounce:
                batch = pending
                try:
                    process_batch(root, batch, args.push)
                except GitError as error:
                    print(f"[錯誤] {error}", file=sys.stderr, flush=True)
                    print("[停止] 請檢查 Git 狀態並處理後再重新啟動監看器。", file=sys.stderr, flush=True)
                    return 1
                pending = set()
    except KeyboardInterrupt:
        print("\n[停止] 監看器已停止。", flush=True)
        if pending:
            print(f"[提醒] 尚有 {len(pending)} 個變更未處理。", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
