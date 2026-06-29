from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SOURCE_URL = "https://www.kagawa-nct.ac.jp/dormitoryE/kondate.pdf"

ROOT = Path(__file__).resolve().parents[1]
LATEST = ROOT / "latest.pdf"
STATE = ROOT / "state.json"
ARCHIVE_ROOT = ROOT / "archive"

ARCHIVE_RE = re.compile(
    r"^(?P<seq>\d{4})_"
    r"(?P<start>\d{4}-\d{2}-\d{2})_"
    r"(?P<end>\d{4}-\d{2}-\d{2})"
    r"\.pdf$"
)


@dataclass(frozen=True)
class ArchiveEntry:
    seq: int
    week_start: date
    week_end: date
    path: Path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()

    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)

    return h.hexdigest()


def now_jst() -> datetime:
    return datetime.now(ZoneInfo("Asia/Tokyo"))


def monday_of_week(d: date) -> date:
    return d - timedelta(days=d.weekday())


def parse_date(s: str) -> date:
    return date.fromisoformat(s)


def archive_entries() -> list[ArchiveEntry]:
    entries: list[ArchiveEntry] = []

    if not ARCHIVE_ROOT.exists():
        return entries

    for path in ARCHIVE_ROOT.glob("*/*.pdf"):
        m = ARCHIVE_RE.match(path.name)
        if not m:
            continue

        try:
            seq = int(m.group("seq"))
            week_start = parse_date(m.group("start"))
            week_end = parse_date(m.group("end"))
        except ValueError:
            continue

        entries.append(
            ArchiveEntry(
                seq=seq,
                week_start=week_start,
                week_end=week_end,
                path=path,
            )
        )

    return sorted(entries, key=lambda e: (e.seq, e.week_start, e.path.as_posix()))


def latest_archive_entry() -> ArchiveEntry | None:
    entries = archive_entries()
    if not entries:
        return None

    return entries[-1]


def find_same_hash_in_archive(target_hash: str) -> ArchiveEntry | None:
    for entry in archive_entries():
        if sha256_file(entry.path) == target_hash:
            return entry

    return None


def archive_path(seq: int, week_start: date) -> Path:
    week_end = week_start + timedelta(days=6)
    filename = f"{seq:04d}_{week_start.isoformat()}_{week_end.isoformat()}.pdf"
    return ARCHIVE_ROOT / str(week_start.year) / filename


def load_state() -> dict:
    if not STATE.exists():
        return {}

    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(
    *,
    latest_hash: str,
    latest_archive: ArchiveEntry | None,
    latest_path: Path | None,
    changed: bool,
    mode: str,
) -> None:
    latest_pdf_hash = sha256_file(LATEST) if LATEST.exists() else ""

    state = {
        "source_url": SOURCE_URL,
        "latest_pdf_path": "latest.pdf",
        "latest_pdf_hash": latest_pdf_hash,
        "latest_hash": latest_hash,
        "updated_at": now_jst().isoformat(),
        "changed": changed,
        "mode": mode,
    }

    if latest_archive is not None:
        state.update(
            {
                "latest_seq": latest_archive.seq,
                "latest_week_start": latest_archive.week_start.isoformat(),
                "latest_week_end": latest_archive.week_end.isoformat(),
                "latest_archive_path": str(latest_archive.path.relative_to(ROOT)),
            }
        )
    elif latest_path is not None:
        state.update(
            {
                "latest_archive_path": str(latest_path.relative_to(ROOT)),
            }
        )

    STATE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def initial_week_start() -> date:
    raw = os.environ.get("INITIAL_WEEK_START", "").strip()

    if raw:
        return parse_date(raw)

    return monday_of_week(now_jst().date())


def next_archive_destination() -> tuple[int, date, Path]:
    latest = latest_archive_entry()

    if latest is None:
        seq = 1
        week_start = initial_week_start()
    else:
        seq = latest.seq + 1
        week_start = latest.week_start + timedelta(days=7)

    return seq, week_start, archive_path(seq, week_start)


def download_pdf() -> bytes:
    request = urllib.request.Request(
        SOURCE_URL,
        headers={
            "User-Agent": "github-actions dormitory-menu-fetcher",
        },
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read()

    if not data.startswith(b"%PDF"):
        raise RuntimeError("Downloaded file is not a PDF")

    return data


def copy_tmp_to_latest(tmp: Path) -> None:
    shutil.copyfile(tmp, LATEST)


def main() -> None:
    mode = os.environ.get("MODE", "auto").strip() or "auto"

    if mode not in {"auto", "replace-latest-archive", "no-archive"}:
        raise ValueError(f"Invalid MODE: {mode}")

    data = download_pdf()
    remote_hash = sha256_bytes(data)

    latest_hash = sha256_file(LATEST) if LATEST.exists() else ""

    if remote_hash == latest_hash:
        print("skip: school kondate.pdf equals latest.pdf")

        same_archive = find_same_hash_in_archive(remote_hash)
        save_state(
            latest_hash=remote_hash,
            latest_archive=same_archive,
            latest_path=same_archive.path if same_archive else None,
            changed=False,
            mode=mode,
        )
        return

    tmp = ROOT / "kondate.tmp.pdf"
    tmp.write_bytes(data)

    try:
        copy_tmp_to_latest(tmp)
        print("updated: latest.pdf")

        if mode == "no-archive":
            print("archive skipped: mode=no-archive")
            save_state(
                latest_hash=remote_hash,
                latest_archive=latest_archive_entry(),
                latest_path=None,
                changed=True,
                mode=mode,
            )
            return

        same_archive = find_same_hash_in_archive(remote_hash)
        if same_archive is not None:
            print(f"archive already has same hash: {same_archive.path.relative_to(ROOT)}")
            save_state(
                latest_hash=remote_hash,
                latest_archive=same_archive,
                latest_path=same_archive.path,
                changed=True,
                mode=mode,
            )
            return

        latest_entry = latest_archive_entry()

        if mode == "replace-latest-archive":
            if latest_entry is None:
                print("replace requested, but archive is empty; falling back to auto")
            else:
                shutil.copyfile(tmp, latest_entry.path)
                print(f"replaced archive: {latest_entry.path.relative_to(ROOT)}")
                save_state(
                    latest_hash=remote_hash,
                    latest_archive=latest_entry,
                    latest_path=latest_entry.path,
                    changed=True,
                    mode=mode,
                )
                return

        seq, week_start, target = next_archive_destination()
        week_end = week_start + timedelta(days=6)

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(tmp, target)

        new_entry = ArchiveEntry(
            seq=seq,
            week_start=week_start,
            week_end=week_end,
            path=target,
        )

        print(f"archived: {target.relative_to(ROOT)}")
        print(f"sha256: {remote_hash}")

        save_state(
            latest_hash=remote_hash,
            latest_archive=new_entry,
            latest_path=target,
            changed=True,
            mode=mode,
        )

    finally:
        tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    main()