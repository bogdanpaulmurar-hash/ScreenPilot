"""Runtime localization.

Design:
* Every user-facing string lives in ``locales/<code>.json`` as a flat mapping
  of dotted keys (``"capture.region"``) to text. A ``_meta`` object holds the
  language's native name.
* Missing keys fall back to the fallback language (English), then to the key
  itself, so a missing translation never crashes the UI.
* :meth:`Translator.set_language` notifies listeners immediately, so the UI
  re-translates in place: no restart is needed.
* Adding a language = dropping a new JSON file next to the others (or into an
  extra directory passed to the constructor). No code changes.
"""

from __future__ import annotations

import json
import logging
import string
import threading
from pathlib import Path
from typing import Callable, Iterable

log = logging.getLogger(__name__)

BUILTIN_LOCALES_DIR = Path(__file__).with_name("locales")
DEFAULT_LANGUAGE = "en"
META_KEY = "_meta"

Listener = Callable[[str], None]


class _SafeDict(dict):
    """Leaves unknown ``{placeholders}`` untouched instead of raising."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def load_catalog(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Locale file must contain a JSON object: {path}")
    return data


class Translator:
    def __init__(
        self,
        language: str = DEFAULT_LANGUAGE,
        *,
        fallback: str = DEFAULT_LANGUAGE,
        locale_dirs: Iterable[Path] = (),
    ) -> None:
        self._lock = threading.RLock()
        self._dirs = [BUILTIN_LOCALES_DIR, *locale_dirs]
        self._catalogs: dict[str, dict[str, str]] = {}
        self._listeners: list[Listener] = []
        self.fallback = fallback
        self._language = fallback
        self._load_all()
        self.set_language(language, notify=False)

    # -- catalogue management -------------------------------------------------
    def _load_all(self) -> None:
        for directory in self._dirs:
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.json")):
                try:
                    catalog = load_catalog(path)
                except (OSError, ValueError) as exc:
                    log.error("Skipping invalid locale file %s: %s", path, exc)
                    continue
                # Later directories extend / override earlier ones.
                self._catalogs.setdefault(path.stem, {}).update(catalog)

    def available_languages(self) -> dict[str, str]:
        """Language code -> native display name."""
        result = {}
        for code, catalog in sorted(self._catalogs.items()):
            meta = catalog.get(META_KEY)
            name = meta.get("name", code) if isinstance(meta, dict) else code
            result[code] = name
        return result

    def keys(self, language: str | None = None) -> set[str]:
        catalog = self._catalogs.get(language or self.fallback, {})
        return {k for k in catalog if k != META_KEY}

    # -- language selection ---------------------------------------------------
    @property
    def language(self) -> str:
        return self._language

    def set_language(self, language: str, *, notify: bool = True) -> str:
        with self._lock:
            if language not in self._catalogs:
                log.warning("Unknown language %r, using %r", language, self.fallback)
                language = self.fallback
            changed = language != self._language
            self._language = language
            listeners = list(self._listeners)
        if notify and changed:
            for listener in listeners:
                try:
                    listener(language)
                except Exception:  # noqa: BLE001
                    log.exception("Language listener failed")
        return language

    def add_listener(self, listener: Listener) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def remove() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return remove

    # -- lookup ---------------------------------------------------------------
    def has(self, key: str) -> bool:
        return key in self._catalogs.get(self._language, {}) or key in self._catalogs.get(self.fallback, {})

    def t(self, key: str, **params: object) -> str:
        text = self._catalogs.get(self._language, {}).get(key)
        if not isinstance(text, str):
            text = self._catalogs.get(self.fallback, {}).get(key)
        if not isinstance(text, str):
            log.debug("Missing translation key %s", key)
            return key
        if params:
            try:
                return string.Formatter().vformat(text, (), _SafeDict(params))
            except (ValueError, IndexError):
                log.warning("Bad format string for key %s", key)
        return text

    __call__ = t
