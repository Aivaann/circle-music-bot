"""Чтение настроек из .env без внешних зависимостей.

Формат файла .env — по строке KEY=VALUE, строки с # игнорируются.
Реальные значения в репозиторий не попадают (см. .gitignore).
"""

import os
from pathlib import Path

ENV_PATH = Path(__file__).with_name(".env")
_loaded = False


def load_env(path: Path = ENV_PATH) -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True

    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        # уже заданные переменные окружения имеют приоритет
        os.environ.setdefault(key, value)


def env(name: str, default: str | None = None) -> str:
    """Значение из .env/окружения. Без default и без значения — ошибка."""
    load_env()
    value = os.environ.get(name, default)
    if value is None:
        raise SystemExit(
            f"В .env не задана переменная {name}. "
            "Скопируй .env.example в .env и заполни."
        )
    return value


def env_int(name: str, default: int | None = None) -> int:
    raw = env(name, None if default is None else str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise SystemExit(f"Переменная {name} должна быть числом, а не {raw!r}")
