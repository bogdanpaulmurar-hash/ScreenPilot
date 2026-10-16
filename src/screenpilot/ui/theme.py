"""Design tokens and the application-wide Fluent-style theme.

Visual language: Windows 11 / Fluent 2 — neutral layered surfaces, 8 px
corners, hairline borders, one accent colour taken from the user's Windows
accent, light/dark following the system (or forced in Settings). Everything
visual reads from :class:`Theme`, so a new theme is a new token set.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

log = logging.getLogger(__name__)

FONT_FAMILIES = ("Segoe UI Variable Text", "Segoe UI", "Inter", "Arial")
MODES = ("system", "light", "dark")


@dataclass(frozen=True)
class Theme:
    dark: bool
    window: str  # page background
    surface: str  # cards
    surface_alt: str  # inputs, hover
    surface_strong: str  # pressed, selected rows
    border: str
    border_strong: str
    text: str
    text_secondary: str
    text_muted: str
    accent: str
    accent_hover: str
    on_accent: str
    accent_subtle: str  # tinted backgrounds (selected tile)
    danger: str
    success: str
    overlay_dim: str  # region selector dimming (rgba)
    radius: int = 8

    def qcolor(self, token: str) -> QColor:
        return QColor(getattr(self, token))


LIGHT = Theme(
    dark=False,
    window="#f3f3f3",
    surface="#ffffff",
    surface_alt="#f9f9f9",
    surface_strong="#ebebeb",
    border="#e5e5e5",
    border_strong="#d1d1d1",
    text="#1b1b1b",
    text_secondary="#5c5c5c",
    text_muted="#8a8a8a",
    accent="#0067c0",
    accent_hover="#1975c5",
    on_accent="#ffffff",
    accent_subtle="#e5f0fa",
    danger="#c42b1c",
    success="#0f7b0f",
    overlay_dim="rgba(0, 0, 0, 0.45)",
)

DARK = Theme(
    dark=True,
    window="#202020",
    surface="#2b2b2b",
    surface_alt="#323232",
    surface_strong="#3a3a3a",
    border="#3a3a3a",
    border_strong="#4a4a4a",
    text="#ffffff",
    text_secondary="#c5c5c5",
    text_muted="#9a9a9a",
    accent="#4cc2ff",
    accent_hover="#47b1e8",
    on_accent="#000000",
    accent_subtle="#1f3a4d",
    danger="#ff99a4",
    success="#6ccb5f",
    overlay_dim="rgba(0, 0, 0, 0.55)",
)


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(
        round(a.red() + (b.red() - a.red()) * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue() + (b.blue() - a.blue()) * t),
    )


def system_is_dark() -> bool:
    hints = QGuiApplication.styleHints()
    scheme = getattr(hints, "colorScheme", None)
    if scheme is not None:
        return scheme() == Qt.ColorScheme.Dark
    return QGuiApplication.palette().color(QPalette.ColorRole.Window).lightness() < 128


def system_accent() -> QColor | None:
    role = getattr(QPalette.ColorRole, "Accent", None)  # Qt 6.6+
    if role is None:
        return None
    color = QGuiApplication.palette().color(role)
    return color if color.isValid() and color.saturation() > 40 else None


def build_theme(mode: str = "system") -> Theme:
    dark = system_is_dark() if mode == "system" else mode == "dark"
    base = DARK if dark else LIGHT
    accent = system_accent()
    if accent is None:
        return base
    # Fluent uses a lighter accent tone on dark surfaces and a darker one on light.
    tuned = accent.lighter(150) if dark and accent.lightness() < 140 else accent
    if not dark and tuned.lightness() > 150:
        tuned = tuned.darker(140)
    surface = QColor(base.surface)
    on_accent = "#000000" if tuned.lightness() > 150 else "#ffffff"
    return replace(
        base,
        accent=tuned.name(),
        accent_hover=_mix(tuned, surface, 0.12).name(),
        accent_subtle=_mix(surface, tuned, 0.18 if dark else 0.10).name(),
        on_accent=on_accent,
    )


def ui_font(point_size: float = 10.0, *, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    font = QFont()
    font.setFamilies(list(FONT_FAMILIES))
    font.setPointSizeF(point_size)
    font.setWeight(weight)
    return font


def stylesheet(t: Theme) -> str:
    r = t.radius
    return f"""
* {{ outline: none; }}
QWidget {{ color: {t.text}; font-size: 10pt; }}
QDialog, QMainWindow, QWizard, QWidget#Page, QWidget[page="true"] {{ background: {t.window}; }}
QToolTip {{ background: {t.surface}; color: {t.text}; border: 1px solid {t.border_strong}; border-radius: 6px; padding: 6px 8px; }}

QLabel[role="display"] {{ font-size: 20pt; font-weight: 600; }}
QLabel[role="title"] {{ font-size: 14pt; font-weight: 600; }}
QLabel[role="subtitle"] {{ font-size: 11pt; font-weight: 600; }}
QLabel[role="body"] {{ color: {t.text}; }}
QLabel[role="secondary"] {{ color: {t.text_secondary}; }}
QLabel[role="caption"] {{ color: {t.text_muted}; font-size: 9pt; }}
QLabel[role="danger"] {{ color: {t.danger}; }}
QLabel[role="kbd"] {{ background: {t.surface_alt}; border: 1px solid {t.border_strong}; border-bottom-width: 2px;
    border-radius: 4px; padding: 1px 6px; font-size: 9pt; color: {t.text_secondary}; }}

QFrame[card="true"] {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: {r}px; }}
QFrame[card="true"] QLabel {{ background: transparent; border: none; }}
QFrame[divider="true"] {{ background: {t.border}; max-height: 1px; min-height: 1px; border: none; }}

QPushButton {{ background: {t.surface}; border: 1px solid {t.border_strong}; border-radius: 6px;
    padding: 6px 14px; min-height: 20px; }}
QPushButton:hover {{ background: {t.surface_alt}; }}
QPushButton:pressed {{ background: {t.surface_strong}; color: {t.text_secondary}; }}
QPushButton:disabled {{ color: {t.text_muted}; background: {t.surface_alt}; border-color: {t.border}; }}
QPushButton[accent="true"] {{ background: {t.accent}; color: {t.on_accent}; border: 1px solid {t.accent}; font-weight: 600; }}
QPushButton[accent="true"]:hover {{ background: {t.accent_hover}; border-color: {t.accent_hover}; }}
QPushButton[flat="true"], QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 6px; padding: 6px; }}
QPushButton[flat="true"]:hover, QToolButton:hover {{ background: {t.surface_strong}; }}
QToolButton:checked {{ background: {t.accent_subtle}; }}

QLineEdit, QComboBox, QSpinBox, QKeySequenceEdit QLineEdit {{ background: {t.surface_alt}; border: 1px solid {t.border};
    border-bottom: 1px solid {t.border_strong}; border-radius: 6px; padding: 5px 10px; min-height: 20px;
    selection-background-color: {t.accent}; selection-color: {t.on_accent}; }}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover {{ background: {t.surface_strong}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ background: {t.surface}; border-bottom: 2px solid {t.accent}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox QAbstractItemView {{ background: {t.surface}; border: 1px solid {t.border_strong}; border-radius: {r}px;
    padding: 4px; selection-background-color: {t.surface_strong}; selection-color: {t.text}; }}
QSpinBox::up-button, QSpinBox::down-button {{ width: 18px; border: none; background: transparent; }}

QListWidget, QTableWidget, QTreeView {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: {r}px;
    alternate-background-color: {t.surface_alt}; gridline-color: transparent; }}
QListWidget::item {{ border-radius: 6px; padding: 6px 8px; margin: 1px 2px; }}
QListWidget::item:hover, QTableWidget::item:hover {{ background: {t.surface_alt}; }}
QListWidget::item:selected, QTableWidget::item:selected {{ background: {t.accent_subtle}; color: {t.text}; }}
QHeaderView::section {{ background: {t.surface}; color: {t.text_secondary}; border: none;
    border-bottom: 1px solid {t.border}; padding: 6px 8px; font-weight: 600; }}
QTableWidget {{ padding: 2px; }}
QTableWidget::item {{ padding: 4px 8px; border-bottom: 1px solid {t.border}; }}

QListWidget#Nav {{ background: transparent; border: none; }}
QListWidget#Nav::item {{ padding: 9px 12px; margin: 2px 0; border-radius: 6px; }}
QListWidget#Nav::item:selected {{ background: {t.surface_strong}; color: {t.text}; border-left: 3px solid {t.accent}; }}

QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 4px; border: 1px solid {t.border_strong}; background: {t.surface_alt}; }}
QCheckBox::indicator:checked {{ background: {t.accent}; border-color: {t.accent}; }}

QMenu {{ background: {t.surface}; border: 1px solid {t.border_strong}; border-radius: {r}px; padding: 4px; }}
QMenu::item {{ padding: 7px 28px 7px 12px; border-radius: 6px; margin: 1px 2px; }}
QMenu::item:selected {{ background: {t.surface_strong}; }}
QMenu::item:disabled {{ color: {t.text_muted}; }}
QMenu::separator {{ height: 1px; background: {t.border}; margin: 4px 8px; }}
QMenu::indicator {{ width: 14px; height: 14px; left: 6px; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {t.border_strong}; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {t.text_muted}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {t.border_strong}; border-radius: 3px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background: none; border: none; width: 0; height: 0; }}

QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
"""


class ThemeManager(QObject):
    """Owns the active theme, applies it app-wide and follows system changes."""

    changed = Signal(object)  # Theme

    def __init__(self, mode: str = "system", parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.mode = mode if mode in MODES else "system"
        self.theme = build_theme(self.mode)
        hints = QGuiApplication.styleHints()
        signal = getattr(hints, "colorSchemeChanged", None)
        if signal is not None:
            signal.connect(lambda *_: self.refresh())

    def set_mode(self, mode: str) -> None:
        self.mode = mode if mode in MODES else "system"
        self.refresh()

    def refresh(self) -> None:
        self.theme = build_theme(self.mode)
        self.apply()

    def apply(self) -> None:
        app = QApplication.instance()
        if app is None:
            return
        t = self.theme
        app.setStyle("Fusion")  # consistent base on every Windows version
        palette = QPalette()
        for role, value in (
            (QPalette.ColorRole.Window, t.window),
            (QPalette.ColorRole.Base, t.surface),
            (QPalette.ColorRole.AlternateBase, t.surface_alt),
            (QPalette.ColorRole.Button, t.surface),
            (QPalette.ColorRole.Text, t.text),
            (QPalette.ColorRole.WindowText, t.text),
            (QPalette.ColorRole.ButtonText, t.text),
            (QPalette.ColorRole.PlaceholderText, t.text_muted),
            (QPalette.ColorRole.Highlight, t.accent),
            (QPalette.ColorRole.HighlightedText, t.on_accent),
            (QPalette.ColorRole.ToolTipBase, t.surface),
            (QPalette.ColorRole.ToolTipText, t.text),
            (QPalette.ColorRole.Mid, t.text_muted),
            (QPalette.ColorRole.Link, t.accent),
        ):
            palette.setColor(role, QColor(value))
        for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
            palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(t.text_muted))
        app.setPalette(palette)
        app.setFont(ui_font(10))
        app.setStyleSheet(stylesheet(t))
        self.changed.emit(t)
        log.info("Theme applied (dark=%s, accent=%s)", t.dark, t.accent)
