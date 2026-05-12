import base64
import logging
import re
import shutil
import threading
import uuid
import time
from pathlib import Path
from typing import Any
import requests
import config

from flask import Flask, jsonify, render_template, request
from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.id3 import APIC, ID3, TALB, TPE2, TPOS, TRCK
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 512
app.logger.setLevel(logging.DEBUG)
REMOTE_SHARE_ROOT = Path(config.REMOTE_SHARE_ROOT)
LIBRARY_SHARE_ROOT = Path(config.LIBRARY_SHARE_ROOT)
MUSICBRAINZ_USER_AGENT = "AudioMoverUI/1.0.0 (https://github.com/velkrosmaak/audio-mover-ui)"

SUPPORTED_EXTENSIONS = {
    ".aac",
    ".aiff",
    ".alac",
    ".flac",
    ".m4a",
    ".mp3",
    ".mp4",
    ".ogg",
    ".oga",
    ".opus",
    ".wav",
    ".wma",
}

JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
LOSSLESS_EXTENSIONS = {".flac", ".wav", ".aiff", ".aif", ".alac"}
DISC_DIRECTORY_PATTERN = re.compile(r"^(disc|disk|cd|lp)\s*[-_ ]*\d+$", re.IGNORECASE)


def has_supported_audio_extension(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_EXTENSIONS


def is_audio_file(path: Path) -> bool:
    return path.is_file() and has_supported_audio_extension(path)


def safe_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def flatten_tag_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value if item is not None)
    return str(value)


def first_tag_value(tags: Any, *keys: str) -> Any:
    for key in keys:
        try:
            value = tags.get(key)
        except Exception:
            continue
        if value not in (None, "", []):
            return value
    return None


def sanitize_path_component(value: str, fallback: str) -> str:
    cleaned = "".join("_" if char in '<>:"/\\|?*\0' else char for char in value).strip().rstrip(".")
    return cleaned or fallback


def infer_album_root_directory(audio_path: Path, root_directory: Path) -> Path:
    parent_directory = audio_path.parent
    relative_parent = parent_directory.relative_to(root_directory)
    relative_parts = relative_parent.parts

    for index, part in enumerate(relative_parts):
        if DISC_DIRECTORY_PATTERN.match(part):
            if index == 0:
                return root_directory
            return root_directory.joinpath(*relative_parts[:index])

    return parent_directory


def infer_disc_number_from_path(audio_path: Path, album_directory: Path) -> int | None:
    try:
        relative_parent = audio_path.parent.relative_to(album_directory)
    except ValueError:
        return None

    for part in relative_parent.parts:
        match = DISC_DIRECTORY_PATTERN.match(part)
        if match:
            digits = re.search(r"(\d+)", part)
            if digits:
                return safe_int(digits.group(1))
    return None


def split_slash_number(value: Any) -> tuple[int | None, int | None]:
    if value is None:
        return None, None

    if isinstance(value, list) and value:
        return split_slash_number(value[0])

    if isinstance(value, tuple):
        first = safe_int(value[0]) if len(value) > 0 else None
        second = safe_int(value[1]) if len(value) > 1 else None
        return first, second

    text = str(value).strip()
    if not text:
        return None, None

    if "/" in text:
        first, second = text.split("/", 1)
        return safe_int(first.strip()), safe_int(second.strip())

    return safe_int(text), None


def format_number_pair(number: int | None, total: int | None) -> str:
    if number is None:
        return ""
    return f"{number}/{total}" if total else str(number)


def extract_track_fields(tags: Any) -> tuple[str, int | None]:
    number, total = split_slash_number(first_tag_value(tags, "TRCK", "trkn", "tracknumber"))
    return format_number_pair(number, total), number


def extract_disc_fields(tags: Any) -> tuple[str, int | None]:
    number, total = split_slash_number(first_tag_value(tags, "TPOS", "disk", "discnumber"))
    return format_number_pair(number, total), number


def detect_image_mime(image_bytes: bytes) -> str | None:
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    if image_bytes.startswith(b"BM"):
        return "image/bmp"
    return None


def build_artwork_data_url(image_bytes: bytes | None, mime_hint: str | None = None) -> str | None:
    if not image_bytes:
        return None

    mime_type = mime_hint or detect_image_mime(image_bytes) or "image/jpeg"
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def extract_artwork(audio: Any) -> str | None:
    if audio is None:
        return None

    if isinstance(audio, FLAC) and audio.pictures:
        picture = audio.pictures[0]
        return build_artwork_data_url(picture.data, picture.mime)

    if isinstance(audio, MP4):
        covers = audio.tags.get("covr", []) if audio.tags else []
        if covers:
            cover = covers[0]
            mime_hint = "image/png" if getattr(cover, "imageformat", None) == MP4Cover.FORMAT_PNG else "image/jpeg"
            return build_artwork_data_url(bytes(cover), mime_hint)

    if getattr(audio, "tags", None):
        for tag in audio.tags.values():
            if isinstance(tag, APIC):
                return build_artwork_data_url(tag.data, tag.mime)

    return None


def extract_metadata(audio_path: Path, root_directory: Path) -> dict[str, Any]:
    app.logger.debug("Extracting metadata from %s", audio_path)
    audio = MutagenFile(audio_path)
    if audio is None:
        app.logger.debug("Mutagen could not read audio file: %s", audio_path)
        raise ValueError("Unsupported or unreadable audio file")

    tags = audio.tags or {}
    info = getattr(audio, "info", None)
    track_display, track_number = extract_track_fields(tags)
    disc_display, disc_number = extract_disc_fields(tags)

    album = flatten_tag_value(first_tag_value(tags, "TALB", "\xa9alb", "album"))
    album_artist = flatten_tag_value(first_tag_value(tags, "TPE2", "aART", "albumartist"))
    album_dir = infer_album_root_directory(audio_path, root_directory)
    album_dir_relative = str(album_dir.relative_to(root_directory))

    return {
        "file_name": audio_path.name,
        "relative_path": str(audio_path.relative_to(root_directory)),
        "absolute_path": str(audio_path),
        "title": flatten_tag_value(first_tag_value(tags, "TIT2", "\xa9nam", "title")),
        "artist": flatten_tag_value(first_tag_value(tags, "TPE1", "\xa9ART", "artist")),
        "album": album,
        "album_artist": album_artist,
        "genre": flatten_tag_value(first_tag_value(tags, "TCON", "\xa9gen", "genre")),
        "disc": disc_display,
        "disc_number": disc_number,
        "track": track_display,
        "track_number": track_number,
        "year": flatten_tag_value(first_tag_value(tags, "TDRC", "\xa9day", "date")),
        "duration_seconds": round(getattr(info, "length", 0.0), 2) if info else None,
        "bitrate_kbps": round(getattr(info, "bitrate", 0) / 1000) if getattr(info, "bitrate", None) else None,
        "sample_rate_hz": safe_int(getattr(info, "sample_rate", None)) if info else None,
        "artwork_data_url": extract_artwork(audio),
        "album_dir_relative": album_dir_relative,
        "album_group_key": f"{album_artist}\u241f{album}\u241f{album_dir_relative}",
    }


def get_audio_quality(audio_path: Path) -> dict[str, Any]:
    audio = MutagenFile(audio_path)
    info = getattr(audio, "info", None)
    codec = str(getattr(info, "codec", "") or getattr(info, "codec_description", "") or "").lower()
    bits_per_sample = safe_int(getattr(info, "bits_per_sample", None)) or 0
    sample_rate = safe_int(getattr(info, "sample_rate", None)) or 0
    bitrate = safe_int(getattr(info, "bitrate", None)) or 0
    is_lossless = (
        audio_path.suffix.lower() in LOSSLESS_EXTENSIONS
        or bits_per_sample > 0
        or "lossless" in codec
        or "alac" in codec
    )
    return {
        "is_lossless": 1 if is_lossless else 0,
        "bits_per_sample": bits_per_sample,
        "sample_rate": sample_rate,
        "bitrate": bitrate,
        "file_size": audio_path.stat().st_size,
    }


def compare_audio_quality(source_path: Path, destination_path: Path) -> tuple[int, str]:
    source_quality = get_audio_quality(source_path)
    destination_quality = get_audio_quality(destination_path)

    comparisons = [
        ("is_lossless", "lossless format"),
        ("bits_per_sample", "bit depth"),
        ("sample_rate", "sample rate"),
        ("bitrate", "bitrate"),
        ("file_size", "file size"),
    ]

    for field, label in comparisons:
        source_value = source_quality[field]
        destination_value = destination_quality[field]
        if source_value > destination_value:
            return 1, f"Incoming file has higher {label}."
        if source_value < destination_value:
            return -1, f"Existing library file has higher {label}."

    return 0, "Quality appears equivalent."


def save_disc_and_track_values(audio_path: Path, disc_number: int, disc_total: int, track_number: int, track_total: int) -> None:
    audio = MutagenFile(audio_path)
    if audio is None:
        raise ValueError(f"Unsupported or unreadable audio file: {audio_path}")

    if isinstance(audio, MP4):
        if audio.tags is None:
            audio.add_tags()
        audio.tags["disk"] = [(disc_number, disc_total)]
        audio.tags["trkn"] = [(track_number, track_total)]
        audio.save()
        return

    if isinstance(audio, FLAC | OggVorbis | OggOpus):
        if audio.tags is None:
            audio.add_tags()
        audio["discnumber"] = [str(disc_number)]
        audio["disctotal"] = [str(disc_total)]
        audio["tracknumber"] = [str(track_number)]
        audio["tracktotal"] = [str(track_total)]
        audio.save()
        return

    if getattr(audio, "tags", None) is None and hasattr(audio, "add_tags"):
        audio.add_tags()

    if isinstance(audio.tags, ID3):
        audio.tags.delall("TPOS")
        audio.tags.delall("TRCK")
        audio.tags.add(TPOS(encoding=3, text=[f"{disc_number}/{disc_total}"]))
        audio.tags.add(TRCK(encoding=3, text=[f"{track_number}/{track_total}"]))
        audio.save()
        return

    if audio.tags is not None:
        audio.tags["discnumber"] = [str(disc_number)]
        audio.tags["disctotal"] = [str(disc_total)]
        audio.tags["tracknumber"] = [str(track_number)]
        audio.tags["tracktotal"] = [str(track_total)]
        audio.save()
        return

    raise ValueError(f"Disc/track editing is not supported for: {audio_path.suffix.lower()}")


def fetch_musicbrainz_tracks(artist: str, album: str) -> list[dict[str, Any]]:
    """Queries MusicBrainz for the tracklist of a given album and artist."""
    if not artist or not album or artist == "Unknown Artist" or album == "Unknown Album":
        return []

    try:
        headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}
        search_url = "https://musicbrainz.org/ws/2/release/"
        params = {"query": f'artist:"{artist}" AND release:"{album}"', "fmt": "json"}
        
        response = requests.get(search_url, params=params, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if not data.get("releases"):
            return []
            
        # Take the best match release ID
        release_id = data["releases"][0]["id"]
        lookup_url = f"https://musicbrainz.org/ws/2/release/{release_id}"
        params = {"inc": "recordings", "fmt": "json"}
        
        # MusicBrainz prefers a 1s delay between requests to avoid rate limiting
        time.sleep(1.0)
        response = requests.get(lookup_url, params=params, headers=headers, timeout=10)
        response.raise_for_status()
        release_data = response.json()
        
        tracks = []
        for media in release_data.get("media", []):
            disc_number = media.get("position", 1)
            for track in media.get("tracks", []):
                tracks.append({
                    "title": track.get("title"),
                    "track_number": safe_int(track.get("position")),
                    "disc_number": safe_int(disc_number),
                    "duration_seconds": (track.get("length") or 0) / 1000,
                })
        return tracks
    except Exception as exc:
        app.logger.warning("MusicBrainz lookup failed for %s - %s: %s", artist, album, exc)
        return []


def inject_missing_tracks(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Identifies missing tracks in an album by comparing against MusicBrainz."""
    if not items:
        return []

    album_groups = {}
    for item in items:
        key = item.get("album_group_key")
        if key not in album_groups:
            album_groups[key] = []
        album_groups[key].append(item)

    all_items = list(items)
    for key, group_items in album_groups.items():
        # Use metadata from the first file in the group to perform the search
        artist = group_items[0].get("album_artist") or group_items[0].get("artist")
        album = group_items[0].get("album")
        
        mb_tracks = fetch_musicbrainz_tracks(artist, album)
        if not mb_tracks:
            for it in group_items:
                it["mb_lookup_failed"] = True
            continue

        local_map = {(it.get("disc_number"), it.get("track_number")) for it in group_items}
        for mb_t in mb_tracks:
            if (mb_t["disc_number"], mb_t["track_number"]) not in local_map:
                all_items.append({
                    "file_name": "[MISSING]",
                    "relative_path": "Not found in directory",
                    "title": mb_t["title"],
                    "artist": artist,
                    "album": album,
                    "album_artist": artist,
                    "disc_number": mb_t["disc_number"],
                    "disc": str(mb_t["disc_number"]),
                    "track_number": mb_t["track_number"],
                    "track": str(mb_t["track_number"]),
                    "duration_seconds": mb_t["duration_seconds"],
                    "is_missing": True,
                    "album_dir_relative": group_items[0].get("album_dir_relative"),
                    "album_group_key": key,
                })
    return sort_metadata_rows(all_items)


def sort_metadata_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda item: (
            item.get("disc_number") if item.get("disc_number") is not None else 9999,
            item.get("track_number") if item.get("track_number") is not None else 9999,
            item.get("relative_path", "").lower(),
            item.get("file_name", "").lower(),
        ),
    )


def save_album_artist(audio_path: Path, album_artist: str) -> None:
    app.logger.info("Updating album artist for %s -> %s", audio_path, album_artist)
    audio = MutagenFile(audio_path)
    if audio is None:
        raise ValueError(f"Unsupported or unreadable audio file: {audio_path}")

    if isinstance(audio, MP4):
        if audio.tags is None:
            audio.add_tags()
        audio.tags["aART"] = [album_artist]
        audio.save()
        return

    if isinstance(audio, FLAC | OggVorbis | OggOpus):
        if audio.tags is None:
            audio.add_tags()
        audio["albumartist"] = [album_artist]
        audio.save()
        return

    if getattr(audio, "tags", None) is None and hasattr(audio, "add_tags"):
        audio.add_tags()

    if isinstance(audio.tags, ID3):
        audio.tags.delall("TPE2")
        audio.tags.add(TPE2(encoding=3, text=[album_artist]))
        audio.save()
        return

    if audio.tags is not None:
        audio.tags["albumartist"] = [album_artist]
        audio.save()
        return

    raise ValueError(f"Album artist editing is not supported for: {audio_path.suffix.lower()}")


def save_album_title(audio_path: Path, album_title: str) -> None:
    app.logger.info("Updating album title for %s -> %s", audio_path, album_title)
    audio = MutagenFile(audio_path)
    if audio is None:
        raise ValueError(f"Unsupported or unreadable audio file: {audio_path}")

    if isinstance(audio, MP4):
        if audio.tags is None:
            audio.add_tags()
        audio.tags["\xa9alb"] = [album_title]
        audio.save()
        return

    if isinstance(audio, FLAC | OggVorbis | OggOpus):
        if audio.tags is None:
            audio.add_tags()
        audio["album"] = [album_title]
        audio.save()
        return

    if getattr(audio, "tags", None) is None and hasattr(audio, "add_tags"):
        audio.add_tags()

    if isinstance(audio.tags, ID3):
        audio.tags.delall("TALB")
        audio.tags.add(TALB(encoding=3, text=[album_title]))
        audio.save()
        return

    if audio.tags is not None:
        audio.tags["album"] = [album_title]
        audio.save()
        return

    raise ValueError(f"Album title editing is not supported for: {audio_path.suffix.lower()}")


def resolve_remote_share_root() -> Path:
    return REMOTE_SHARE_ROOT.expanduser().resolve()


def resolve_library_share_root() -> Path:
    return LIBRARY_SHARE_ROOT.expanduser().resolve()


def resolve_remote_browser_path(relative_path: str = "") -> Path:
    root = resolve_remote_share_root()
    if not relative_path:
        return root

    candidate = (root / relative_path).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("Requested path is outside the configured remote share root.")
    return candidate


def resolve_library_target_path(album_artist: str, album: str) -> Path:
    root = resolve_library_share_root()
    artist_dir = sanitize_path_component(album_artist or "Unknown Artist", "Unknown Artist")
    album_dir = sanitize_path_component(album or "Unknown Album", "Unknown Album")
    return root / artist_dir / album_dir


def list_audio_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.rglob("*") if is_audio_file(path))


def list_remote_directories(relative_path: str = "") -> dict[str, Any]:
    current_path = resolve_remote_browser_path(relative_path)
    root = resolve_remote_share_root()
    if not current_path.exists() or not current_path.is_dir():
        raise FileNotFoundError(f"Directory not found: {current_path}")

    entries = []
    for child in sorted(current_path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        if not child.is_dir():
            continue
        child_relative = str(child.relative_to(root))
        entries.append(
            {
                "name": child.name,
                "relative_path": child_relative,
                "audio_file_count": len(list_audio_files(child)),
                "subdirectory_count": len([path for path in child.iterdir() if path.is_dir()]),
            }
        )

    current_relative = "" if current_path == root else str(current_path.relative_to(root))
    parent_relative = ""
    if current_path != root:
        parent_relative = str(current_path.parent.relative_to(root)) if current_path.parent != root else ""

    return {
        "root": str(root),
        "current_relative_path": current_relative,
        "current_display_path": str(current_path),
        "parent_relative_path": parent_relative,
        "entries": entries,
    }


def create_job(job_type: str, source_relative_path: str, source_label: str) -> str:
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "job_type": job_type,
            "status": "queued",
            "message": "Queued",
            "phase": "queued",
            "progress_current": 0,
            "progress_total": 0,
            "progress_percent": 0,
            "source": source_label,
            "source_relative_path": source_relative_path,
            "editable": True,
            "source_type": "remote",
            "items": [],
            "errors": [],
            "reports": [],
            "updated_count": 0,
            "moved_album_dir_relative": "",
        }
    return job_id


def update_job(job_id: str, **changes: Any) -> None:
    with JOBS_LOCK:
        job = JOBS[job_id]
        job.update(changes)
        current = job.get("progress_current", 0)
        total = job.get("progress_total", 0)
        job["progress_percent"] = int((current / total) * 100) if total else 0


def append_job_error(job_id: str, message: str) -> None:
    with JOBS_LOCK:
        JOBS[job_id]["errors"].append(message)


def append_job_report(job_id: str, message: str) -> None:
    with JOBS_LOCK:
        JOBS[job_id]["reports"].append(message)


def get_job_snapshot(job_id: str) -> dict[str, Any] | None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return None
        return dict(job)


def collect_metadata_rows(job_id: str, directory: Path, audio_files: list[Path], start_index: int = 0, total_override: int | None = None) -> list[dict[str, Any]]:
    rows = []
    total = total_override if total_override is not None else len(audio_files)

    for index, audio_file in enumerate(audio_files, start=1):
        current = start_index + index
        update_job(
            job_id,
            status="running",
            phase="scanning",
            message=f"Reading metadata: {audio_file.name}",
            progress_current=current,
            progress_total=total,
        )
        try:
            rows.append(extract_metadata(audio_file, directory))
        except Exception as exc:
            app.logger.exception("Failed to extract metadata for %s", audio_file)
            append_job_error(job_id, f"{audio_file.name}: {type(exc).__name__}: {exc}")

    return sort_metadata_rows(rows)


def run_analysis_job(job_id: str, directory: Path, source_label: str, source_relative_path: str) -> None:
    try:
        app.logger.info("Starting analysis job %s for %s", job_id, directory)
        audio_files = list_audio_files(directory)
        update_job(
            job_id,
            status="running",
            phase="counting",
            message=f"Found {len(audio_files)} supported audio files.",
            progress_current=0,
            progress_total=max(len(audio_files), 1),
        )

        if not audio_files:
          update_job(
              job_id,
              status="completed",
              phase="completed",
              message="No supported audio files were found in that directory.",
              progress_current=0,
              progress_total=0,
              items=[],
              source=source_label,
              source_relative_path=source_relative_path,
          )
          return

        items = collect_metadata_rows(job_id, directory, audio_files)
        items = inject_missing_tracks(items)
        update_job(
            job_id,
            status="completed",
            phase="completed",
            message=f"Analysis complete for {len(items)} audio files.",
            progress_current=len(audio_files),
            progress_total=len(audio_files),
            items=items,
            source=source_label,
            source_relative_path=source_relative_path,
        )
    except Exception as exc:
        app.logger.exception("Analysis job failed for %s", directory)
        update_job(job_id, status="failed", phase="failed", message=f"Analysis failed: {exc}")


def run_album_artist_update_job(job_id: str, directory: Path, source_label: str, source_relative_path: str, album_artist: str) -> None:
    try:
        app.logger.info("Starting album artist update job %s for %s", job_id, directory)
        audio_files = list_audio_files(directory)
        if not audio_files:
            update_job(job_id, status="failed", phase="failed", message="No supported audio files were found to update.")
            return

        total_steps = len(audio_files) * 2
        updated_count = 0
        update_job(
            job_id,
            status="running",
            phase="writing",
            message=f"Updating album artist on {len(audio_files)} files...",
            progress_current=0,
            progress_total=total_steps,
        )

        for index, audio_file in enumerate(audio_files, start=1):
            update_job(
                job_id,
                status="running",
                phase="writing",
                message=f"Writing tags: {audio_file.name}",
                progress_current=index,
                progress_total=total_steps,
            )
            try:
                save_album_artist(audio_file, album_artist)
                updated_count += 1
            except Exception as exc:
                app.logger.exception("Failed to update album artist for %s", audio_file)
                append_job_error(job_id, f"{audio_file.name}: {type(exc).__name__}: {exc}")

        items = collect_metadata_rows(job_id, directory, audio_files, start_index=len(audio_files), total_override=total_steps)
        items = inject_missing_tracks(items)
        update_job(
            job_id,
            status="completed",
            phase="completed",
            message=f"Updated album artist on {updated_count} audio files.",
            progress_current=total_steps,
            progress_total=total_steps,
            items=items,
            updated_count=updated_count,
            source=source_label,
            source_relative_path=source_relative_path,
        )
    except Exception as exc:
        app.logger.exception("Album artist update job failed for %s", directory)
        update_job(job_id, status="failed", phase="failed", message=f"Album artist update failed: {exc}")


def run_album_title_update_job(job_id: str, directory: Path, source_label: str, source_relative_path: str, album_title: str) -> None:
    try:
        app.logger.info("Starting album title update job %s for %s", job_id, directory)
        audio_files = list_audio_files(directory)
        if not audio_files:
            update_job(job_id, status="failed", phase="failed", message="No supported audio files were found to update.")
            return

        total_steps = len(audio_files) * 2
        updated_count = 0
        update_job(
            job_id,
            status="running",
            phase="writing",
            message=f"Updating album title on {len(audio_files)} files...",
            progress_current=0,
            progress_total=total_steps,
        )

        for index, audio_file in enumerate(audio_files, start=1):
            update_job(
                job_id,
                status="running",
                phase="writing",
                message=f"Writing album title: {audio_file.name}",
                progress_current=index,
                progress_total=total_steps,
            )
            try:
                save_album_title(audio_file, album_title)
                updated_count += 1
            except Exception as exc:
                app.logger.exception("Failed to update album title for %s", audio_file)
                append_job_error(job_id, f"{audio_file.name}: {type(exc).__name__}: {exc}")

        items = collect_metadata_rows(job_id, directory, audio_files, start_index=len(audio_files), total_override=total_steps)
        items = inject_missing_tracks(items)
        update_job(
            job_id,
            status="completed",
            phase="completed",
            message=f"Updated album title on {updated_count} audio files.",
            progress_current=total_steps,
            progress_total=total_steps,
            items=items,
            updated_count=updated_count,
            source=source_label,
            source_relative_path=source_relative_path,
        )
    except Exception as exc:
        app.logger.exception("Album title update job failed for %s", directory)
        update_job(job_id, status="failed", phase="failed", message=f"Album title update failed: {exc}")


def rename_album_tracks(album_directory: Path, root_directory: Path, audio_files: list[Path], job_id: str, start_index: int, total_steps: int) -> None:
    rename_plan: list[tuple[Path, Path, Path]] = []

    for index, audio_file in enumerate(audio_files, start=1):
        metadata = extract_metadata(audio_file, root_directory)
        track_number = metadata.get("track_number")
        title = metadata.get("title") or audio_file.stem
        padded_track = f"{track_number:02d}" if isinstance(track_number, int) else "00"
        safe_title = sanitize_path_component(str(title), audio_file.stem)
        target_name = f"{padded_track} - {safe_title}{audio_file.suffix.lower()}"
        target_path = audio_file.with_name(target_name)
        temp_path = audio_file.with_name(f".apu-tmp-{uuid.uuid4().hex}{audio_file.suffix.lower()}")

        update_job(
            job_id,
            status="running",
            phase="renaming",
            message=f"Renaming track: {audio_file.name}",
            progress_current=start_index + index,
            progress_total=total_steps,
        )

        if target_path.exists() and target_path not in audio_files:
            raise FileExistsError(f"Target track already exists: {target_path.name}")

        rename_plan.append((audio_file, temp_path, target_path))

    for source_path, temp_path, target_path in rename_plan:
        if source_path == target_path:
            continue
        source_path.rename(temp_path)

    for _, temp_path, target_path in rename_plan:
        if temp_path == target_path:
            continue
        temp_path.rename(target_path)


def normalize_multi_disc_tags(album_directory: Path, audio_files: list[Path], job_id: str) -> None:
    disc_buckets: dict[int, list[Path]] = {}
    for audio_file in audio_files:
        inferred_disc = infer_disc_number_from_path(audio_file, album_directory) or 1
        disc_buckets.setdefault(inferred_disc, []).append(audio_file)

    if len(disc_buckets) <= 1:
        return

    total_discs = max(disc_buckets)
    append_job_report(job_id, f"Normalizing multi-disc tags across {len(disc_buckets)} discs.")

    for disc_number, disc_files in sorted(disc_buckets.items()):
        disc_files_sorted = sorted(
            disc_files,
            key=lambda path: (
                extract_metadata(path, album_directory).get("track_number") if extract_metadata(path, album_directory).get("track_number") is not None else 9999,
                path.name.lower(),
            ),
        )
        track_total = len(disc_files_sorted)
        for track_index, audio_file in enumerate(disc_files_sorted, start=1):
            metadata = extract_metadata(audio_file, album_directory)
            track_number = metadata.get("track_number") or track_index
            save_disc_and_track_values(audio_file, disc_number, total_discs, track_number, track_total)


def cleanup_empty_directories(start_directory: Path, stop_directory: Path) -> None:
    current = start_directory
    while current != stop_directory and stop_directory in current.parents:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def find_existing_destination_variant(destination_file: Path) -> Path | None:
    if destination_file.exists():
        return destination_file

    for candidate in destination_file.parent.glob(f"{destination_file.stem}.*"):
        if candidate.is_file() and has_supported_audio_extension(candidate):
            return candidate
    return None


def merge_album_into_library(job_id: str, source_album_directory: Path, destination_album_directory: Path, total_steps: int) -> None:
    source_files = list_audio_files(source_album_directory)
    for index, source_file in enumerate(source_files, start=1):
        update_job(
            job_id,
            status="running",
            phase="moving",
            message=f"Comparing and moving: {source_file.name}",
            progress_current=len(source_files) + 1 + index,
            progress_total=total_steps,
        )

        relative_file = source_file.relative_to(source_album_directory)
        destination_file = destination_album_directory / relative_file
        destination_file.parent.mkdir(parents=True, exist_ok=True)

        existing_destination = find_existing_destination_variant(destination_file)
        if existing_destination is not None:
            comparison, reason = compare_audio_quality(source_file, existing_destination)
            if comparison > 0:
                existing_destination.unlink()
                shutil.move(str(source_file), str(destination_file))
                append_job_report(job_id, f"Replaced {existing_destination.name} with {destination_file.name}: {reason}")
            else:
                append_job_report(job_id, f"Kept existing {existing_destination.name}: {reason}")
                source_file.unlink()
        else:
            shutil.move(str(source_file), str(destination_file))
            append_job_report(job_id, f"Added new file {destination_file.name}.")

    cleanup_empty_directories(source_album_directory, source_album_directory.parent)


def run_move_album_job(
    job_id: str,
    source_root_directory: Path,
    source_root_label: str,
    source_relative_path: str,
    album_dir_relative: str,
) -> None:
    try:
        album_directory = (source_root_directory / album_dir_relative).resolve()
        if album_directory != source_root_directory and source_root_directory not in album_directory.parents:
            raise ValueError("Requested album directory is outside the processed folder.")
        if not album_directory.exists() or not album_directory.is_dir():
            raise FileNotFoundError(f"Album directory not found: {album_directory}")

        audio_files = list_audio_files(album_directory)
        if not audio_files:
            update_job(job_id, status="failed", phase="failed", message="No supported audio files were found in that album.")
            return

        sample_metadata = extract_metadata(audio_files[0], source_root_directory)
        album_artist = sample_metadata.get("album_artist") or "Unknown Artist"
        album = sample_metadata.get("album") or album_directory.name
        destination = resolve_library_target_path(str(album_artist), str(album))
        destination.parent.mkdir(parents=True, exist_ok=True)

        total_steps = (len(audio_files) * 2) + 2
        update_job(
            job_id,
            status="running",
            phase="preparing",
            message=f"Preparing to move {album_directory.name}...",
            progress_current=0,
            progress_total=total_steps,
            moved_album_dir_relative=album_dir_relative,
        )

        normalize_multi_disc_tags(album_directory, audio_files, job_id)
        rename_album_tracks(album_directory, source_root_directory, audio_files, job_id, 0, total_steps)

        update_job(
            job_id,
            status="running",
            phase="moving",
            message=f"Moving album to library: {destination}",
            progress_current=len(audio_files) + 1,
            progress_total=total_steps,
        )

        if destination.exists():
            merge_album_into_library(job_id, album_directory, destination, total_steps)
            move_message = f"Merged album into existing library folder: {destination}"
        else:
            album_directory.rename(destination)
            move_message = f"Moved album to library: {destination}"

        if source_root_directory.exists():
            remaining_files = list_audio_files(source_root_directory)
            update_job(
                job_id,
                status="running",
                phase="scanning",
                message="Refreshing remaining folder metadata...",
                progress_current=0,
                progress_total=max(len(remaining_files), 1),
            )
            items = collect_metadata_rows(job_id, source_root_directory, remaining_files, start_index=0, total_override=max(len(remaining_files), 1))
            items = inject_missing_tracks(items)
            editable = bool(source_root_directory.exists())
        else:
            items = []
            editable = False

        final_progress_total = len(remaining_files) if source_root_directory.exists() and remaining_files else total_steps
        final_progress_current = final_progress_total
        update_job(
            job_id,
            status="completed",
            phase="completed",
            message=move_message,
            progress_current=final_progress_current,
            progress_total=final_progress_total,
            items=items,
            editable=editable,
            source=source_root_label,
            source_relative_path=source_relative_path,
        )
    except Exception as exc:
        app.logger.exception("Album move job failed for %s", album_dir_relative)
        update_job(job_id, status="failed", phase="failed", message=f"Album move failed: {exc}")


def launch_background_job(target: Any, *args: Any) -> str:
    thread = threading.Thread(target=target, args=args, daemon=True)
    thread.start()
    return args[0]


@app.get("/")
def index() -> str:
    return render_template("index.html")


@app.get("/api/remote-browser")
def remote_browser():
    relative_path = (request.args.get("path") or "").strip()
    app.logger.info("Browsing remote share path=%s", relative_path)

    try:
        payload = list_remote_directories(relative_path)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        app.logger.exception("Unable to browse remote share")
        return jsonify({"error": f"Unable to browse remote share: {exc}"}), 500

    return jsonify(payload)


@app.post("/api/process-remote-folder")
def process_remote_folder():
    payload = request.get_json(silent=True) or {}
    relative_path = (payload.get("relative_path") or "").strip()
    app.logger.info("Processing remote folder relative_path=%s", relative_path)
    if not relative_path:
        return jsonify({"error": "Please choose a folder to process."}), 400

    try:
        source_dir = resolve_remote_browser_path(relative_path)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if not source_dir.exists() or not source_dir.is_dir():
        return jsonify({"error": f"Folder not found: {source_dir}"}), 404

    job_id = create_job("analysis", relative_path, source_dir.name)
    launch_background_job(run_analysis_job, job_id, source_dir, source_dir.name, relative_path)
    return jsonify({"job_id": job_id})


@app.post("/api/update-album-artist")
def update_album_artist():
    payload = request.get_json(silent=True) or {}
    relative_path = (payload.get("relative_path") or "").strip()
    new_album_artist = (payload.get("album_artist") or "").strip()
    app.logger.info("Queueing bulk album artist update for relative_path=%s new_album_artist=%s", relative_path, new_album_artist)

    if not relative_path:
        return jsonify({"error": "A processed remote folder is required for bulk editing."}), 400
    if not new_album_artist:
        return jsonify({"error": "Please provide a new album artist value."}), 400

    try:
        directory = resolve_remote_browser_path(relative_path)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if not directory.exists() or not directory.is_dir():
        return jsonify({"error": f"Folder not found: {directory}"}), 404

    job_id = create_job("album_artist_update", relative_path, directory.name)
    launch_background_job(run_album_artist_update_job, job_id, directory, directory.name, relative_path, new_album_artist)
    return jsonify({"job_id": job_id})


@app.post("/api/update-album-title")
def update_album_title():
    payload = request.get_json(silent=True) or {}
    relative_path = (payload.get("relative_path") or "").strip()
    new_album_title = (payload.get("album_title") or "").strip()
    app.logger.info("Queueing bulk album title update for relative_path=%s new_album_title=%s", relative_path, new_album_title)

    if not relative_path:
        return jsonify({"error": "A processed remote folder is required for bulk editing."}), 400
    if not new_album_title:
        return jsonify({"error": "Please provide a new album title value."}), 400

    try:
        directory = resolve_remote_browser_path(relative_path)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if not directory.exists() or not directory.is_dir():
        return jsonify({"error": f"Folder not found: {directory}"}), 404

    job_id = create_job("album_title_update", relative_path, directory.name)
    launch_background_job(run_album_title_update_job, job_id, directory, directory.name, relative_path, new_album_title)
    return jsonify({"job_id": job_id})


@app.post("/api/move-album")
def move_album():
    payload = request.get_json(silent=True) or {}
    relative_path = (payload.get("relative_path") or "").strip()
    album_dir_relative = (payload.get("album_dir_relative") or "").strip()
    app.logger.info("Queueing move-album for relative_path=%s album_dir_relative=%s", relative_path, album_dir_relative)

    if not relative_path:
        return jsonify({"error": "A processed remote folder is required before moving an album."}), 400
    if not album_dir_relative:
        return jsonify({"error": "Please choose an album to move."}), 400

    try:
        directory = resolve_remote_browser_path(relative_path)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if not directory.exists() or not directory.is_dir():
        return jsonify({"error": f"Folder not found: {directory}"}), 404

    job_id = create_job("move_album", relative_path, directory.name)
    launch_background_job(run_move_album_job, job_id, directory, directory.name, relative_path, album_dir_relative)
    return jsonify({"job_id": job_id})


@app.get("/api/jobs/<job_id>")
def get_job(job_id: str):
    snapshot = get_job_snapshot(job_id)
    if snapshot is None:
        return jsonify({"error": "Job not found."}), 404
    return jsonify(snapshot)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5123, debug=True)
