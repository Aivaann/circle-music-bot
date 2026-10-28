"""Наложение фоновой музыки на видеосообщения («кружочки») через ffmpeg.

Модуль ничего не знает про Telegram: на вход — файлы, на выход — готовый mp4.
Скачивание кружочка и отправку результата делает main.py.

Почему ffmpeg: в mp4 нельзя «подложить» отдельную дорожку, которую клиент
Telegram проиграет поверх видео. Музыку нужно смикшировать с голосом и
закодировать в AAC прямо в файл.

Требования Telegram к кружочку: квадратное видео, до 60 секунд, mp4.
Видеодорожку не трогаем (`-c:v copy`), меняется только звук — поэтому
перекодирование быстрое даже на слабом ноутбуке.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Какие файлы считаются музыкой
AUDIO_EXTENSIONS = frozenset(
    {".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".flac"}
)

# Bot API отдаёт файлы через getFile только до 20 МБ
MAX_VIDEO_NOTE_BYTES = 20 * 1024 * 1024

# Ограничение Telegram на длительность кружочка
MAX_VIDEO_NOTE_SECONDS = 60.0

# Громкость музыки относительно голоса (0.18 = 18%)
DEFAULT_VOLUME = 0.18

FADE_IN = 0.8
FADE_OUT = 1.2

SAMPLE_RATE = 48000
AUDIO_BITRATE = "128k"


class FFmpegMissing(RuntimeError):
    """ffmpeg/ffprobe не установлены или не найдены."""


class FFmpegError(RuntimeError):
    """ffmpeg запустился, но не смог обработать файл."""


@dataclass(frozen=True)
class MixResult:
    path: Path
    duration: float
    voice_kept: bool


# --- Поиск ffmpeg -----------------------------------------------------------

_TOOL_CACHE: dict[str, str] = {}


def _winget_candidates(name: str) -> list[Path]:
    """winget ставит ffmpeg в LOCALAPPDATA, но PATH обновляется не сразу."""
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return []
    base = Path(local) / "Microsoft" / "WinGet"
    candidates = [base / "Links" / f"{name}.exe"]
    packages = base / "Packages"
    if packages.is_dir():
        try:
            candidates.extend(sorted(packages.glob(f"Gyan.FFmpeg*/**/bin/{name}.exe")))
        except OSError:
            pass
    return candidates


def find_tool(name: str, configured: str | None = None) -> str:
    """Находит ffmpeg/ffprobe: .env → PATH → стандартная папка winget."""
    cache_key = f"{name}|{configured or ''}"
    cached = _TOOL_CACHE.get(cache_key)
    if cached:
        return cached

    candidates: list[Path] = []
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            candidates.append(path)
        else:
            candidates.append(path / f"{name}.exe")
            candidates.append(path / name)

    found = shutil.which(name)
    if found:
        candidates.append(Path(found))

    candidates.extend(_winget_candidates(name))

    for candidate in candidates:
        if candidate.is_file():
            _TOOL_CACHE[cache_key] = str(candidate)
            return str(candidate)

    raise FFmpegMissing(
        f"Не найден {name}. Установите ffmpeg "
        "(Windows: winget install Gyan.FFmpeg) или укажите FFMPEG_PATH в .env."
    )


# --- Медиа ------------------------------------------------------------------


def probe(media: Path, ffprobe_path: str | None = None) -> dict:
    """Возвращает разобранный JSON ffprobe (потоки + формат)."""
    ffprobe = find_tool("ffprobe", ffprobe_path)
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(media),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except OSError as exc:
        raise FFmpegError(f"Не удалось запустить ffprobe: {exc}") from exc

    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        raise FFmpegError("ffprobe: " + (" | ".join(tail) or "неизвестная ошибка"))

    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise FFmpegError("ffprobe вернул не JSON") from exc


def media_duration(info: dict) -> float:
    """Длительность в секундах: сначала формат, потом самый длинный поток."""
    raw = (info.get("format") or {}).get("duration")
    try:
        value = float(raw)
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass

    longest = 0.0
    for stream in info.get("streams") or []:
        try:
            longest = max(longest, float(stream.get("duration") or 0))
        except (TypeError, ValueError):
            continue
    return longest


def has_audio(info: dict) -> bool:
    return any(
        stream.get("codec_type") == "audio"
        for stream in (info.get("streams") or [])
    )


def list_tracks(folder: Path) -> list[Path]:
    """Все аудиофайлы из папки, по алфавиту. Нет папки — пустой список."""
    if not folder.is_dir():
        return []
    try:
        files = [
            path
            for path in folder.iterdir()
            if path.is_file()
            and path.suffix.lower() in AUDIO_EXTENSIONS
            and not path.name.startswith(".")
        ]
    except OSError:
        return []
    return sorted(files, key=lambda path: path.name.casefold())


def pick_track(tracks: list[Path], selected: str | None) -> Path | None:
    """Выбранный трек по имени; `random`/пусто/не найдено — случайный."""
    if not tracks:
        return None
    if selected and selected not in {"random", ""}:
        for track in tracks:
            if track.name.casefold() == selected.casefold():
                return track
    return random.choice(tracks)


# --- Сборка команды ---------------------------------------------------------


def build_args(
    source: Path,
    music: Path,
    target: Path,
    *,
    duration: float,
    ffmpeg: str,
    volume: float = DEFAULT_VOLUME,
    keep_voice: bool = True,
    source_has_audio: bool = True,
    music_offset: float = 0.0,
    copy_video: bool = True,
    normalize: bool = True,
    gain: float = 1.0,
) -> list[str]:
    """Собирает argv для ffmpeg. Вынесено отдельно, чтобы можно было тестировать."""
    duration = max(0.5, min(float(duration), MAX_VIDEO_NOTE_SECONDS))
    volume = max(0.0, min(float(volume), 1.0)) * max(gain, 1e-6)
    voice_kept = keep_voice and source_has_audio

    music_chain = (
        f"aresample={SAMPLE_RATE},"
        "aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"volume={volume:.4f}"
    )
    if duration > FADE_IN + 0.2:
        music_chain += f",afade=t=in:st=0:d={FADE_IN}"

    fade_out = ""
    if duration > FADE_OUT + 0.3:
        fade_out = f",afade=t=out:st={duration - FADE_OUT:.3f}:d={FADE_OUT}"

    chains: list[str] = []
    if voice_kept:
        voice_chain = (
            f"aresample={SAMPLE_RATE},"
            "aformat=sample_fmts=fltp:channel_layouts=stereo"
        )
        if gain != 1.0:
            voice_chain += f",volume={gain:.4f}"
        chains.append(f"[0:a]{voice_chain}[voice]")
        chains.append(f"[1:a]{music_chain}[music]")
        amix = "[voice][music]amix=inputs=2:duration=longest:dropout_transition=0"
        if normalize:
            amix += ":normalize=0"
        chains.append(f"{amix}{fade_out}[aout]")
    else:
        chains.append(f"[1:a]{music_chain}{fade_out}[aout]")

    args = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-stream_loop",
        "-1",
    ]
    if music_offset > 0.05:
        args += ["-ss", f"{music_offset:.3f}"]
    args += ["-i", str(music)]
    args += [
        "-filter_complex",
        ";".join(chains),
        "-map",
        "0:v:0",
        "-map",
        "[aout]",
    ]

    if copy_video:
        args += ["-c:v", "copy"]
    else:
        args += [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
        ]

    args += [
        "-c:a",
        "aac",
        "-b:a",
        AUDIO_BITRATE,
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        "2",
        "-t",
        f"{duration:.3f}",
        "-movflags",
        "+faststart",
        str(target),
    ]
    return args


def _run(args: list[str], timeout: int = 300) -> None:
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError("ffmpeg не успел обработать видео за 5 минут") from exc
    except OSError as exc:
        raise FFmpegError(f"Не удалось запустить ffmpeg: {exc}") from exc

    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-4:]
        raise FFmpegError("ffmpeg: " + (" | ".join(tail) or "неизвестная ошибка"))


def _random_offset(music: Path, duration: float, ffprobe_path: str | None) -> float:
    """Чтобы один и тот же трек не начинался одинаково, берём случайный кусок."""
    try:
        info = probe(music, ffprobe_path)
        music_duration = media_duration(info)
    except (FFmpegError, FFmpegMissing):
        return 0.0
    if music_duration <= duration + 2:
        return 0.0
    return round(random.uniform(0.0, min(30.0, music_duration - duration - 0.5)), 2)


def mix_video(
    source: Path,
    target: Path,
    music: Path,
    *,
    volume: float = DEFAULT_VOLUME,
    keep_voice: bool = True,
    ffmpeg_path: str | None = None,
    ffprobe_path: str | None = None,
    music_offset: float | None = None,
) -> MixResult:
    """Добавляет музыку к видео и пишет результат в target.

    Сначала пробуем без перекодирования видео. Если исходник попался
    «неудобный» — перекодируем. Если в ffmpeg нет опции amix normalize —
    повторяем без неё, компенсируя громкость.
    """
    ffmpeg = find_tool("ffmpeg", ffmpeg_path)
    info = probe(source, ffprobe_path)
    duration = min(media_duration(info), MAX_VIDEO_NOTE_SECONDS)
    if duration <= 0:
        raise FFmpegError("Не удалось определить длительность видео")
    source_audio = has_audio(info)

    target.parent.mkdir(parents=True, exist_ok=True)

    if music_offset is None:
        music_offset = _random_offset(music, duration, ffprobe_path)

    attempts = [
        dict(copy_video=True, normalize=True, gain=1.0),
        dict(copy_video=False, normalize=True, gain=1.0),
        dict(copy_video=False, normalize=False, gain=2.0),
    ]

    last_error: Exception | None = None
    for attempt in attempts:
        args = build_args(
            source,
            music,
            target,
            duration=duration,
            ffmpeg=ffmpeg,
            volume=volume,
            keep_voice=keep_voice,
            source_has_audio=source_audio,
            music_offset=music_offset,
            **attempt,
        )
        try:
            _run(args)
        except FFmpegError as exc:
            last_error = exc
            continue

        if target.is_file() and target.stat().st_size > 0:
            return MixResult(target, duration, keep_voice and source_audio)

        last_error = FFmpegError("ffmpeg не создал выходной файл")

    raise FFmpegError(str(last_error) if last_error else "ffmpeg не справился")
