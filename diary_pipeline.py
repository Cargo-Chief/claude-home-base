#!/usr/bin/env python3
"""Prepare, validate, and promote one quarantined daily-diary candidate set."""

import argparse
import hashlib
import json
import os
import shutil
import stat
from pathlib import Path

from agent_identity import AGENT_FILES, FOUNDING_PRINCIPLES_FILE, IdentityError, validate_store


PROHIBITED = (
    "pii",
    "customer_specific_facts",
    "credentials",
    "sensitive_production_state",
    "raw_quotations_or_transcripts",
    "task_status",
    "authoritative_company_product_platform_claims",
    "copied_authorization",
)
REVIEWED_FILES = (*AGENT_FILES, "diary.md")


def _regular_private_file(path, maximum=32 * 1024):
    if path.is_symlink() or not path.is_file():
        raise IdentityError(f"candidate must be a regular non-symlink file: {path.name}")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise IdentityError(f"candidate must not be group/world accessible: {path.name}")
    if path.stat().st_size > maximum:
        raise IdentityError(f"candidate exceeds {maximum} bytes: {path.name}")


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stage_dir(root, date):
    return root / ".diary-staging" / date


def prepare(root, date):
    root = Path(root).expanduser().resolve()
    validate_store(root)
    staging_root = root / ".diary-staging"
    if staging_root.is_symlink():
        raise IdentityError("diary staging root must not be a symlink")
    staging_root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(staging_root, 0o700)
    stage = stage_dir(root, date)
    if stage.exists() or stage.is_symlink():
        raise IdentityError(f"diary staging already exists for {date}")
    stage.mkdir(mode=0o700)
    for name in AGENT_FILES:
        destination = stage / name
        shutil.copyfile(root / name, destination)
        os.chmod(destination, 0o600)
    state = {
        "date": date,
        "principles_sha256": _sha256(root / FOUNDING_PRINCIPLES_FILE),
        "principles_mode": stat.S_IMODE((root / FOUNDING_PRINCIPLES_FILE).stat().st_mode),
    }
    state_path = stage / "state.json"
    state_path.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(state_path, 0o600)
    return stage


def validate_review(root, date):
    root = Path(root).expanduser().resolve()
    validate_store(root)
    stage = stage_dir(root, date)
    if stage.is_symlink() or not stage.is_dir():
        raise IdentityError("diary staging directory is missing or unsafe")
    if stat.S_IMODE(stage.stat().st_mode) & 0o077:
        raise IdentityError("diary staging directory must not be group/world accessible")

    state_path = stage / "state.json"
    _regular_private_file(state_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    principles = root / FOUNDING_PRINCIPLES_FILE
    if state != {
        "date": date,
        "principles_sha256": _sha256(principles),
        "principles_mode": stat.S_IMODE(principles.stat().st_mode),
    } or state["principles_mode"] != 0o400:
        raise IdentityError("founding principles changed during diary generation")

    for name in REVIEWED_FILES:
        _regular_private_file(stage / name)
        if not (stage / name).read_text(encoding="utf-8").strip():
            raise IdentityError(f"candidate must not be empty: {name}")

    receipt_path = stage / "review.json"
    _regular_private_file(receipt_path, maximum=8 * 1024)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    expected_keys = {"status", "prohibited", "reviewed_files"}
    if set(receipt) != expected_keys or receipt["status"] != "pass":
        raise IdentityError("diary review receipt is not an exact pass receipt")
    if receipt["reviewed_files"] != list(REVIEWED_FILES):
        raise IdentityError("diary review receipt does not cover every candidate")
    prohibited = receipt["prohibited"]
    if set(prohibited) != set(PROHIBITED) or any(value is not False for value in prohibited.values()):
        raise IdentityError("diary review found or omitted a prohibited category")
    return stage


def discard(root, date):
    root = Path(root).expanduser().resolve()
    stage = stage_dir(root, date)
    staging_root = root / ".diary-staging"
    if staging_root.is_symlink() or stage.is_symlink():
        raise IdentityError("refusing to discard a symlinked diary staging path")
    if stage.exists():
        if not stage.is_dir() or stage.parent != staging_root:
            raise IdentityError("refusing to discard an unsafe diary staging path")
        shutil.rmtree(stage)


def promote(root, date):
    root = Path(root).expanduser().resolve()
    stage = validate_review(root, date)
    diary_target = root / "diary" / f"{date}.md"
    if diary_target.exists() or diary_target.is_symlink():
        raise IdentityError(f"diary entry already exists for {date}")

    backups = stage / "backups"
    backups.mkdir(mode=0o700)
    for name in AGENT_FILES:
        shutil.copyfile(root / name, backups / name)
        os.chmod(backups / name, 0o600)

    replaced = []
    try:
        for name in AGENT_FILES:
            temporary = root / f".{name}.diary-new"
            shutil.copyfile(stage / name, temporary)
            os.chmod(temporary, 0o600)
            os.replace(temporary, root / name)
            replaced.append(name)
        diary_temporary = root / "diary" / f".{date}.md.diary-new"
        shutil.copyfile(stage / "diary.md", diary_temporary)
        os.chmod(diary_temporary, 0o600)
        os.replace(diary_temporary, diary_target)
        validate_store(root)
    except Exception:
        for name in replaced:
            os.replace(backups / name, root / name)
        if diary_target.exists() and not diary_target.is_symlink():
            diary_target.unlink()
        raise

    shutil.rmtree(stage)
    return diary_target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "validate", "promote", "discard"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--date", required=True)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        print(prepare(args.root, args.date))
    elif args.command == "validate":
        print(validate_review(args.root, args.date))
    elif args.command == "promote":
        print(promote(args.root, args.date))
    else:
        discard(args.root, args.date)


if __name__ == "__main__":
    main()
