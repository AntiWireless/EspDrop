# EspDrop - the look of the desktop app: Apple-like colour, type and controls.
#
# Fusion with a palette and a style sheet made from two sets of colours, light
# and dark, following the system (ESPDROP_THEME=light|dark forces one), plus
# the controls Qt has no Apple-shaped version of: the switch, the segmented
# control, round device avatars with a progress ring, the radar shown while
# searching, a thin progress line, rounded "cards" for grouped settings and
# line icons. All of it is drawn in code: no image or font files to ship. Text
# uses the system's UI face (SF on a Mac, Segoe UI Variable on Windows 11,
# Inter or the desktop's font on Linux).
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import os
import re

from PySide6.QtCore import (QEasingCurve, QPoint, QPointF, QRect, QRectF, QSize, Qt,
                            QTimer, QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QGuiApplication, QIcon, QImageReader,
                           QLinearGradient, QPainter, QPainterPath, QPalette, QPen, QPixmap)
from PySide6.QtWidgets import (QAbstractButton, QFrame, QHBoxLayout, QLabel, QLayout,
                               QSizePolicy, QToolButton, QVBoxLayout, QWidget)

# ---- colours ---------------------------------------------------------------------

LIGHT = {
    "window": "#F5F5F7", "card": "#FFFFFF", "card_border": "#E3E3E8",
    "separator": "#E5E5EA", "text": "#1D1D1F", "secondary": "#6E6E73",
    "tertiary": "#AEAEB2", "accent": "#007AFF", "accent_hover": "#1A86FF",
    "accent_pressed": "#0066D6", "focus": "#80B9FF",
    "fill": "#E8E8ED", "fill_hover": "#DEDEE3", "fill_pressed": "#D1D1D6",
    "field": "#FFFFFF", "field_border": "#D2D2D7",
    "segment": "#E8E8ED", "control": "#FFFFFF", "switch_off": "#E2E2E7",
    "track": "#E5E5EA", "scroll": "#C7C7CC", "hover": "#EDEDF2",
    "green": "#34C759", "orange": "#FF9500", "red": "#FF3B30",
    "avatar_top": "#A9AEBA", "avatar_bottom": "#858A96", "shadow_alpha": 40,
}
DARK = {
    "window": "#1C1C1E", "card": "#2C2C2E", "card_border": "#3A3A3C",
    "separator": "#3A3A3C", "text": "#F5F5F7", "secondary": "#98989D",
    "tertiary": "#636366", "accent": "#0A84FF", "accent_hover": "#3395FF",
    "accent_pressed": "#0070E0", "focus": "#4A9EFF",
    "fill": "#3A3A3C", "fill_hover": "#444446", "fill_pressed": "#505053",
    "field": "#1C1C1E", "field_border": "#48484A",
    "segment": "#1C1C1E", "control": "#636366", "switch_off": "#48484A",
    "track": "#3A3A3C", "scroll": "#5A5A5E", "hover": "#38383A",
    "green": "#30D158", "orange": "#FF9F0A", "red": "#FF453A",
    "avatar_top": "#8E8E93", "avatar_bottom": "#636366", "shadow_alpha": 90,
}
T = dict(LIGHT)                                # the colours in use; widgets read them when painting


def color(key, alpha=None):
    c = QColor(T[key])
    if alpha is not None:
        c.setAlpha(alpha)
    return c


def is_dark():
    forced = os.environ.get("ESPDROP_THEME", "").lower()
    if forced in ("dark", "light"):
        return forced == "dark"
    try:
        return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:                     # Qt before 6.5
        return QGuiApplication.palette().color(QPalette.ColorRole.Window).lightness() < 128


# ---- type ------------------------------------------------------------------------

TEXT_FACES = ["SF Pro Text", ".AppleSystemUIFont", "Segoe UI Variable Text", "Segoe UI",
              "Inter", "Helvetica Neue", "Cantarell", "Noto Sans", "DejaVu Sans"]
DISPLAY_FACES = ["SF Pro Display", ".AppleSystemUIFont", "Segoe UI Variable Display",
                 "Segoe UI", "Inter Display", "Inter", "Helvetica Neue", "Cantarell",
                 "Noto Sans", "DejaVu Sans"]


def font(px=13, weight=QFont.Weight.Normal):
    f = QFont()
    f.setFamilies(DISPLAY_FACES if px >= 20 else TEXT_FACES)
    f.setPixelSize(px)
    f.setWeight(weight)
    return f


# ---- applying it ---------------------------------------------------------------

def apply(app):
    """Style the whole app once; it follows the system's light/dark switch."""
    if app.property("espdrop_theme"):
        return
    app.setProperty("espdrop_theme", True)
    app.setStyle("Fusion")
    refresh(app)
    try:
        app.styleHints().colorSchemeChanged.connect(lambda _scheme: refresh(app))
    except AttributeError:
        pass


def refresh(app):
    T.clear()
    T.update(DARK if is_dark() else LIGHT)
    app.setFont(font(13))
    app.setPalette(palette())
    app.setStyleSheet(qss())
    for w in app.topLevelWidgets():
        w.update()


def palette():
    p = QPalette()
    roles = {
        QPalette.ColorRole.Window: "window", QPalette.ColorRole.WindowText: "text",
        QPalette.ColorRole.Base: "field", QPalette.ColorRole.AlternateBase: "window",
        QPalette.ColorRole.Text: "text", QPalette.ColorRole.Button: "fill",
        QPalette.ColorRole.ButtonText: "text", QPalette.ColorRole.Highlight: "accent",
        QPalette.ColorRole.ToolTipBase: "card", QPalette.ColorRole.ToolTipText: "text",
        QPalette.ColorRole.PlaceholderText: "tertiary", QPalette.ColorRole.Link: "accent",
        QPalette.ColorRole.Mid: "separator", QPalette.ColorRole.Midlight: "card",
        QPalette.ColorRole.Dark: "fill_pressed", QPalette.ColorRole.Light: "card",
    }
    for role, key in roles.items():
        p.setColor(role, QColor(T[key]))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText,
                 QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(T["tertiary"]))
    return p


def qss():
    t = T
    return f"""
QWidget {{ color: {t['text']}; }}
QMainWindow, QDialog {{ background: {t['window']}; }}
QWidget#page {{ background: {t['window']}; }}
QScrollArea {{ background: transparent; border: none; }}
QFrame#card {{ background: {t['card']}; border: 1px solid {t['card_border']};
              border-radius: 12px; }}
QFrame#alert {{ background: {t['card']}; border: 1px solid {t['card_border']};
               border-radius: 16px; }}
QLabel {{ background: transparent; }}
QLabel[role="title"] {{ font-size: 24px; font-weight: 700; }}
QLabel[role="headline"] {{ font-size: 15px; font-weight: 600; }}
QLabel[role="section"] {{ color: {t['secondary']}; font-size: 12px; font-weight: 600; }}
QLabel[role="secondary"] {{ color: {t['secondary']}; }}
QLabel[role="caption"] {{ color: {t['secondary']}; font-size: 11px; }}
QPushButton {{ background: {t['fill']}; color: {t['text']}; border: none; border-radius: 7px;
              padding: 5px 14px; min-height: 20px; }}
QPushButton:hover {{ background: {t['fill_hover']}; }}
QPushButton:pressed {{ background: {t['fill_pressed']}; }}
QPushButton:disabled {{ color: {t['tertiary']}; }}
QPushButton:focus {{ outline: none; }}
QPushButton[kind="primary"] {{ background: {t['accent']}; color: white; font-weight: 600; }}
QPushButton[kind="primary"]:hover {{ background: {t['accent_hover']}; }}
QPushButton[kind="primary"]:pressed {{ background: {t['accent_pressed']}; }}
QPushButton[kind="link"] {{ background: transparent; color: {t['accent']}; padding: 2px 4px;
                           min-height: 0; }}
QPushButton[kind="link"]:hover {{ color: {t['accent_hover']}; }}
QPushButton[kind="link"]:disabled {{ color: {t['tertiary']}; }}
QToolButton {{ background: transparent; border: none; border-radius: 7px; padding: 4px; }}
QToolButton:hover {{ background: {t['fill']}; }}
QToolButton:pressed {{ background: {t['fill_pressed']}; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}
QLineEdit {{ background: {t['field']}; border: 1px solid {t['field_border']};
            border-radius: 7px; padding: 4px 8px; selection-background-color: {t['accent']};
            selection-color: white; }}
QLineEdit:focus {{ border: 2px solid {t['focus']}; padding: 3px 7px; }}
QPlainTextEdit {{ background: {t['card']}; border: 1px solid {t['card_border']};
                 border-radius: 10px; padding: 6px; }}
QMenu {{ background: {t['card']}; border: 1px solid {t['separator']}; border-radius: 9px;
        padding: 5px; }}
QMenu::item {{ padding: 5px 22px 5px 14px; border-radius: 5px; }}
QMenu::item:selected {{ background: {t['accent']}; color: white; }}
QMenu::item:disabled {{ color: {t['tertiary']}; }}
QMenu::separator {{ height: 1px; background: {t['separator']}; margin: 4px 8px; }}
QMenu::indicator {{ width: 0; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {t['scroll']}; border-radius: 3px; min-height: 30px;
                              margin: 0 2px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QToolTip {{ background: {t['card']}; color: {t['text']}; border: 1px solid {t['separator']};
           border-radius: 6px; padding: 4px 6px; }}
QMessageBox {{ background: {t['window']}; }}
"""


def ensure(app):
    """apply() unless it ran already (windows made without main(), as in tests)."""
    apply(app)


# ---- drawn icons -------------------------------------------------------------------

def _pen(c, width):
    return QPen(QColor(c), width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin)


def paint_glyph(p, name, r, c, width=1.8):
    """A line icon in the rect r (QRectF): up, down, x, check, folder, search,
    more, phone, tablet, mac, person, doc, alert."""
    p.save()
    p.setPen(_pen(c, width))
    p.setBrush(Qt.BrushStyle.NoBrush)
    x, y, w, h = r.x(), r.y(), r.width(), r.height()
    cx, cy = r.center().x(), r.center().y()
    if name in ("up", "down"):
        s = 1 if name == "up" else -1
        tip, tail = cy - s * h * 0.30, cy + s * h * 0.30
        p.drawLine(QPointF(cx, tail), QPointF(cx, tip))
        p.drawLine(QPointF(cx, tip), QPointF(cx - w * 0.24, tip + s * h * 0.24))
        p.drawLine(QPointF(cx, tip), QPointF(cx + w * 0.24, tip + s * h * 0.24))
    elif name == "x":
        d = w * 0.24
        p.drawLine(QPointF(cx - d, cy - d), QPointF(cx + d, cy + d))
        p.drawLine(QPointF(cx + d, cy - d), QPointF(cx - d, cy + d))
    elif name == "check":
        path = QPainterPath(QPointF(x + w * 0.24, y + h * 0.52))
        path.lineTo(x + w * 0.43, y + h * 0.70)
        path.lineTo(x + w * 0.77, y + h * 0.32)
        p.drawPath(path)
    elif name == "folder":
        path = QPainterPath(QPointF(x + w * 0.12, y + h * 0.28))
        path.lineTo(x + w * 0.40, y + h * 0.28)
        path.lineTo(x + w * 0.48, y + h * 0.36)
        path.lineTo(x + w * 0.88, y + h * 0.36)
        path.lineTo(x + w * 0.88, y + h * 0.76)
        path.lineTo(x + w * 0.12, y + h * 0.76)
        path.closeSubpath()
        p.drawPath(path)
    elif name == "search":
        rr = w * 0.24
        p.drawEllipse(QPointF(cx - w * 0.06, cy - h * 0.06), rr, rr)
        p.drawLine(QPointF(cx + w * 0.12, cy + h * 0.12), QPointF(cx + w * 0.30, cy + h * 0.30))
    elif name == "more":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(c))
        for k in (-1, 0, 1):
            p.drawEllipse(QPointF(cx + k * w * 0.26, cy), width * 0.9, width * 0.9)
    elif name == "phone":
        body = QRectF(cx - w * 0.21, cy - h * 0.37, w * 0.42, h * 0.74)
        p.drawRoundedRect(body, w * 0.09, w * 0.09)
        p.drawLine(QPointF(cx - w * 0.06, body.top() + h * 0.08),
                   QPointF(cx + w * 0.06, body.top() + h * 0.08))
    elif name == "tablet":
        body = QRectF(cx - w * 0.30, cy - h * 0.38, w * 0.60, h * 0.76)
        p.drawRoundedRect(body, w * 0.08, w * 0.08)
        p.drawLine(QPointF(cx - w * 0.05, body.bottom() - h * 0.08),
                   QPointF(cx + w * 0.05, body.bottom() - h * 0.08))
    elif name == "mac":
        screen = QRectF(cx - w * 0.32, cy - h * 0.28, w * 0.64, h * 0.42)
        p.drawRoundedRect(screen, w * 0.04, w * 0.04)
        p.drawLine(QPointF(cx - w * 0.42, screen.bottom() + h * 0.10),
                   QPointF(cx + w * 0.42, screen.bottom() + h * 0.10))
    elif name == "person":
        p.drawEllipse(QPointF(cx, cy - h * 0.12), w * 0.15, w * 0.15)
        arc = QRectF(cx - w * 0.30, cy + h * 0.10, w * 0.60, h * 0.50)
        p.drawArc(arc, 0, 180 * 16)
    elif name == "doc":
        path = QPainterPath(QPointF(x + w * 0.24, y + h * 0.12))
        path.lineTo(x + w * 0.60, y + h * 0.12)
        path.lineTo(x + w * 0.78, y + h * 0.30)
        path.lineTo(x + w * 0.78, y + h * 0.88)
        path.lineTo(x + w * 0.24, y + h * 0.88)
        path.closeSubpath()
        p.drawPath(path)
        p.drawLine(QPointF(x + w * 0.60, y + h * 0.12), QPointF(x + w * 0.60, y + h * 0.30))
        p.drawLine(QPointF(x + w * 0.60, y + h * 0.30), QPointF(x + w * 0.78, y + h * 0.30))
    elif name == "alert":
        p.drawLine(QPointF(cx, cy - h * 0.24), QPointF(cx, cy + h * 0.06))
        p.drawPoint(QPointF(cx, cy + h * 0.24))
    p.restore()


def _canvas(w, h=None):
    """A transparent pixmap for crisp drawing on high-DPI screens."""
    h = w if h is None else h
    dpr = max(2.0, QGuiApplication.primaryScreen().devicePixelRatio()
              if QGuiApplication.primaryScreen() else 2.0)
    pm = QPixmap(int(w * dpr), int(h * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    return pm


def glyph_icon(name, c=None, px=18, width=1.7):
    pm = _canvas(px)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    paint_glyph(p, name, QRectF(0, 0, px, px), c or T["text"], width)
    p.end()
    return QIcon(pm)


def paint_app_icon(p, r):
    """Our own mark: a blue rounded square with a white drop in it."""
    s = r.width()
    grad = QLinearGradient(r.topLeft(), r.bottomLeft())
    grad.setColorAt(0, QColor("#53A8FF"))
    grad.setColorAt(1, QColor("#0A64F0"))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(grad)
    p.drawRoundedRect(r, s * 0.225, s * 0.225)
    cx, rad = r.center().x(), s * 0.21
    cy = r.top() + s * 0.60
    drop = QPainterPath(QPointF(cx, r.top() + s * 0.17))
    drop.cubicTo(QPointF(cx + rad * 0.25, r.top() + s * 0.31),
                 QPointF(cx + rad, cy - rad * 0.75), QPointF(cx + rad, cy))
    drop.arcTo(QRectF(cx - rad, cy - rad, 2 * rad, 2 * rad), 0, -180)
    drop.cubicTo(QPointF(cx - rad, cy - rad * 0.75),
                 QPointF(cx - rad * 0.25, r.top() + s * 0.31), QPointF(cx, r.top() + s * 0.17))
    white = QLinearGradient(QPointF(cx, r.top()), QPointF(cx, cy + rad))
    white.setColorAt(0, QColor(255, 255, 255, 255))
    white.setColorAt(1, QColor(255, 255, 255, 225))
    p.setBrush(white)
    p.drawPath(drop)


def app_pixmap(px=64):
    pm = _canvas(px)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    paint_app_icon(p, QRectF(px * 0.04, px * 0.04, px * 0.92, px * 0.92))
    p.end()
    return pm


def app_icon():
    icon = QIcon()
    for px in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(app_pixmap(px))
    return icon


# ---- devices ---------------------------------------------------------------------

def device_kind(model, name):
    m, n = (model or "").lower(), (name or "").lower()
    if m.startswith("iphone") or "iphone" in n:
        return "phone"
    if m.startswith("ipad") or "ipad" in n:
        return "tablet"
    if m.startswith(("mac", "imac")) or "macbook" in n or "imac" in n or n.startswith("mac"):
        return "mac"
    return None


def pretty_model(model):
    """'iPad13,1' -> 'iPad' (AirDrop's /Discover gives the hardware id)."""
    if not model:
        return ""
    m = re.match(r"[A-Za-z]+", model)
    word = m.group(0) if m else model
    return {"iphone": "iPhone", "ipad": "iPad", "macbookpro": "MacBook Pro",
            "macbookair": "MacBook Air", "imac": "iMac", "macmini": "Mac mini",
            "mac": "Mac"}.get(word.lower(), word)


def initials(name):
    words = re.findall(r"[^\W\d_]+", name or "")
    if not words:
        return ""
    return (words[0][0] + (words[-1][0] if len(words) > 1 else "")).upper()


def paint_avatar(p, r, name=None, model=None):
    """A round gray avatar: the device's outline (iPhone, iPad, Mac) or the
    name's initials, as the share sheet draws people and devices."""
    grad = QLinearGradient(r.topLeft(), r.bottomLeft())
    grad.setColorAt(0, color("avatar_top"))
    grad.setColorAt(1, color("avatar_bottom"))
    p.save()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(grad)
    p.drawEllipse(r)
    kind = device_kind(model, name)
    text = initials(name) if not kind else ""
    if kind or not text:
        inner = r.adjusted(r.width() * 0.22, r.height() * 0.22,
                           -r.width() * 0.22, -r.height() * 0.22)
        paint_glyph(p, kind or "person", inner, "#FFFFFF", max(1.6, r.width() / 26))
    else:
        p.setPen(QColor("white"))
        p.setFont(font(int(r.height() * 0.38), QFont.Weight.DemiBold))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
    p.restore()


IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"}


def _rounded(pm, px, radius):
    """pm cropped to a px square (or fitted inside it), with rounded corners."""
    out = _canvas(px)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(0, 0, px, px), radius, radius)
    p.setClipPath(clip)
    scaled = pm.scaled(int(px * out.devicePixelRatio()), int(px * out.devicePixelRatio()),
                       Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                       Qt.TransformationMode.SmoothTransformation)
    scaled.setDevicePixelRatio(out.devicePixelRatio())
    sw = scaled.width() / scaled.devicePixelRatio()
    sh = scaled.height() / scaled.devicePixelRatio()
    p.drawPixmap(QPointF((px - sw) / 2, (px - sh) / 2), scaled)
    p.end()
    return out


def rounded_fit(pm, max_w, max_h, radius=12):
    """pm scaled to fit max_w x max_h (keeping its shape), corners rounded."""
    if pm.isNull():
        return pm
    f = min(max_w / pm.width(), max_h / pm.height(), 1.0 if pm.width() >= 64 else 4.0)
    w, h = max(1, int(pm.width() * f)), max(1, int(pm.height() * f))
    out = _canvas(w, h)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(0, 0, w, h), radius, radius)
    p.setClipPath(clip)
    p.drawPixmap(QRectF(0, 0, w, h), pm, QRectF(pm.rect()))
    p.end()
    return out


def image_thumb(path, px=64, radius=10):
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid() and size.width() > 0:
        f = (px * 2.0) / min(size.width(), size.height())
        if f < 1:
            reader.setScaledSize(QSize(int(size.width() * f), int(size.height() * f)))
    img = reader.read()
    if img.isNull():
        return None
    return _rounded(QPixmap.fromImage(img), px, radius)


def file_badge(ext, px=64):
    """A document icon with the file's extension, for anything not a picture."""
    pm = _canvas(px)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    page = QRectF(px * 0.18, px * 0.06, px * 0.64, px * 0.86)
    path = QPainterPath(page.topLeft())
    fold = px * 0.18
    path.lineTo(page.right() - fold, page.top())
    path.lineTo(page.right(), page.top() + fold)
    path.lineTo(page.bottomRight())
    path.lineTo(page.bottomLeft())
    path.closeSubpath()
    p.setPen(_pen(T["separator"] if not is_dark() else T["tertiary"], 1.2))
    p.setBrush(QColor("#FFFFFF") if not is_dark() else QColor("#3A3A3C"))
    p.drawPath(path)
    p.setBrush(color("fill"))
    corner = QPainterPath(QPointF(page.right() - fold, page.top()))
    corner.lineTo(page.right() - fold, page.top() + fold)
    corner.lineTo(page.right(), page.top() + fold)
    corner.closeSubpath()
    p.drawPath(corner)
    label = (ext or "").lstrip(".").upper()[:4]
    if label:
        p.setPen(color("accent"))
        p.setFont(font(max(8, int(px * 0.17)), QFont.Weight.Bold))
        p.drawText(QRectF(page.left(), page.top() + page.height() * 0.45, page.width(),
                          page.height() * 0.3), Qt.AlignmentFlag.AlignCenter, label)
    p.end()
    return pm


def files_preview(paths, px=64):
    """The first file as a picture or a document icon; more files stack behind."""
    if not paths:
        return file_badge("", px)
    first = paths[0]
    ext = os.path.splitext(first)[1].lower()
    front = image_thumb(first, px - 8, 10) if ext in IMAGE_EXT else None
    front = front or file_badge(ext, px - 8)
    out = _canvas(px)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    if len(paths) > 1:                         # more files: cards peeking out behind
        p.setPen(Qt.PenStyle.NoPen)
        for k, a in ((2, 70), (1, 130)):
            p.setBrush(color("tertiary", a))
            p.drawRoundedRect(QRectF(4 + k * 2, 8 - k * 4, px - 8, px - 8), 10, 10)
    p.drawPixmap(QPointF(0, 8), front)
    p.end()
    return out


# ---- controls ----------------------------------------------------------------------

class Switch(QAbstractButton):
    """The Apple switch: a rounded track, a white knob that slides."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._pos = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._set_pos)
        self.toggled.connect(self._slide)

    def sizeHint(self):
        return QSize(42, 26)

    def set_quiet(self, on):
        """Set it without the toggled signal (initial state, undoing a change)."""
        self.blockSignals(True)
        self.setChecked(on)
        self.blockSignals(False)
        self._anim.stop()
        self._set_pos(1.0 if on else 0.0)

    def _set_pos(self, v):
        self._pos = float(v)
        self.update()

    def _slide(self, on):
        end = 1.0 if on else 0.0
        if not self.isVisible():
            self._set_pos(end)
            return
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(end)
        self._anim.start()

    def hitButton(self, pos):
        return self.rect().contains(pos)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = 38.0, 22.0
        r = QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)
        off, on = color("switch_off"), color("accent")
        t = self._pos
        track = QColor(int(off.red() + (on.red() - off.red()) * t),
                       int(off.green() + (on.green() - off.green()) * t),
                       int(off.blue() + (on.blue() - off.blue()) * t))
        if not self.isEnabled():
            p.setOpacity(0.5)
        if self.hasFocus():
            p.setPen(QPen(color("focus"), 3))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r.adjusted(-2, -2, 2, 2), h / 2 + 2, h / 2 + 2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(r, h / 2, h / 2)
        d = h - 4
        knob = QRectF(r.left() + 2 + t * (w - h), r.top() + 2, d, d)
        p.setBrush(QColor(0, 0, 0, 35))
        p.drawEllipse(knob.translated(0, 0.8))
        p.setBrush(QColor("white"))
        p.drawEllipse(knob)


class Segmented(QWidget):
    """The segmented control: a recessed track with a raised, sliding choice."""
    changed = Signal(int)                      # picked by the user (not by set_index)

    def __init__(self, labels, parent=None):
        super().__init__(parent)
        self.labels = list(labels)
        self._index = 0
        self._pos = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._set_pos)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(" / ".join(self.labels))

    def sizeHint(self):
        fm = QFontMetrics(font(13, QFont.Weight.DemiBold))
        seg = max(fm.horizontalAdvance(t) for t in self.labels) + 26
        return QSize(seg * len(self.labels) + 4, 30)

    def minimumSizeHint(self):
        return self.sizeHint()

    def index(self):
        return self._index

    def set_index(self, i, animate=True):
        i = max(0, min(len(self.labels) - 1, i))
        self._index = i
        if animate and self.isVisible():
            self._anim.stop()
            self._anim.setStartValue(self._pos)
            self._anim.setEndValue(float(i))
            self._anim.start()
        else:
            self._set_pos(float(i))

    def select(self, i):
        """As a click on segment i."""
        if i != self._index:
            self.set_index(i)
            self.changed.emit(self._index)

    def _set_pos(self, v):
        self._pos = float(v)
        self.update()

    def mousePressEvent(self, e):
        seg = self.width() / len(self.labels)
        self.select(int(e.position().x() // seg))

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Left, Qt.Key.Key_Up):
            self.select(self._index - 1)
        elif e.key() in (Qt.Key.Key_Right, Qt.Key.Key_Down):
            self.select(self._index + 1)
        else:
            super().keyPressEvent(e)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        n = len(self.labels)
        seg = r.width() / n
        if not self.isEnabled():
            p.setOpacity(0.5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color("segment"))
        p.drawRoundedRect(r, 8, 8)
        sel = QRectF(r.left() + 2 + self._pos * seg, r.top() + 2, seg - 4, r.height() - 4)
        p.setBrush(QColor(0, 0, 0, T["shadow_alpha"] // 2))
        p.drawRoundedRect(sel.translated(0, 0.8), 6, 6)
        p.setBrush(color("control"))
        p.drawRoundedRect(sel, 6, 6)
        p.setPen(QPen(color("tertiary", 120), 1))
        for k in range(1, n):                  # dividers away from the choice
            if abs(self._pos - k) > 0.55 and abs(self._pos - (k - 1)) > 0.55:
                x = r.left() + k * seg
                p.drawLine(QPointF(x, r.top() + 8), QPointF(x, r.bottom() - 8))
        for k, label in enumerate(self.labels):
            chosen = k == self._index
            p.setFont(font(13, QFont.Weight.DemiBold if chosen else QFont.Weight.Normal))
            p.setPen(color("text"))
            p.drawText(QRectF(r.left() + k * seg, r.top(), seg, r.height()),
                       Qt.AlignmentFlag.AlignCenter, label)
        if self.hasFocus():
            p.setPen(QPen(color("focus"), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(r.adjusted(-1, -1, 1, 1), 9, 9)


class ThinProgress(QWidget):
    """A 4 px progress line; value None runs a sliding segment (no total yet)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._value = None
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._step)
        self.setFixedHeight(4)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def value(self):
        return self._value

    def set_value(self, v):
        self._value = None if v is None else max(0.0, min(1.0, float(v)))
        if self._value is None and self.isVisible():
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def showEvent(self, e):
        if self._value is None:
            self._timer.start()

    def hideEvent(self, e):
        self._timer.stop()

    def _step(self):
        self._phase = (self._phase + 0.012) % 1.0
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color("track"))
        p.drawRoundedRect(r, 2, 2)
        p.setBrush(color("accent"))
        if self._value is None:
            seg = r.width() * 0.3
            x = -seg + self._phase * (r.width() + seg)
            p.setClipRect(r)
            p.drawRoundedRect(QRectF(x, r.top(), seg, r.height()), 2, 2)
        elif self._value > 0:
            p.drawRoundedRect(QRectF(r.left(), r.top(), r.width() * self._value, r.height()), 2, 2)


class Radar(QWidget):
    """'Looking for devices': rings that grow out of our mark and fade."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._phase = 0.0
        self._running = False
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._step)
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_running(self, on):
        self._running = on
        if on and self.isVisible():
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def showEvent(self, e):
        if self._running:
            self._timer.start()

    def hideEvent(self, e):
        self._timer.stop()

    def _step(self):
        self._phase = (self._phase + 0.008) % 1.0
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QPointF(self.width() / 2, self.height() / 2)
        maxr = min(self.width(), self.height()) / 2 - 2
        if self._running:
            for k in range(3):
                t = (self._phase + k / 3) % 1.0
                rad = 22 + t * (maxr - 22)
                p.setPen(QPen(color("accent", int(170 * (1 - t))), 1.6))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(c, rad, rad)
        else:
            p.setPen(QPen(color("separator"), 1.2))
            for rad in (maxr * 0.45, maxr * 0.75):
                p.drawEllipse(c, rad, rad)
        paint_app_icon(p, QRectF(c.x() - 20, c.y() - 20, 40, 40))


class StatusDot(QWidget):
    """Green connected, orange looking, red nothing."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._key = "orange"
        self.setFixedSize(10, 10)

    def set_state(self, key):
        self._key = key
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color(self._key))
        p.drawEllipse(QRectF(1, 1, 8, 8))


class ElidedLabel(QLabel):
    """One line, shortened in the middle to fit (paths)."""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def setText(self, text):
        self._full = text
        self.setToolTip(text)
        self._elide()

    def full_text(self):
        return self._full

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def _elide(self):
        fm = QFontMetrics(self.font())
        super().setText(fm.elidedText(self._full, Qt.TextElideMode.ElideMiddle,
                                      max(40, self.width())))


# ---- cards ------------------------------------------------------------------------

class Separator(QWidget):
    def __init__(self, inset=16, parent=None):
        super().__init__(parent)
        self.inset = inset
        self.setFixedHeight(1)

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(QRectF(self.inset, 0, self.width() - self.inset, 1), color("separator"))


class Card(QFrame):
    """A rounded group of rows, divided by inset lines (as in System Settings)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(0, 2, 0, 2)
        self.box.setSpacing(0)
        self._n = 0

    def add(self, w, margins=(16, 10, 16, 10)):
        if self._n:
            self.box.addWidget(Separator(16))
        holder = QWidget()
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(*margins)
        lay.addWidget(w)
        self.box.addWidget(holder)
        self._n += 1
        return holder

    def add_row(self, title, control=None, subtitle=None):
        """Title (and a gray line under it) on the left, a control on the right.
        Returns the subtitle label (None without one)."""
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(12)
        v = QVBoxLayout()
        v.setSpacing(2)
        v.addWidget(QLabel(title))
        sub = None
        if subtitle is not None:
            sub = QLabel(subtitle)
            sub.setProperty("role", "caption")
            sub.setWordWrap(True)
            v.addWidget(sub)
        h.addLayout(v, 1)
        if control is not None:
            h.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
        self.add(row, margins=(16, 9, 14, 9))
        return sub


def section(title, right=None):
    """The gray heading above a card, with an optional button on the right."""
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(6, 14, 4, 4)
    label = QLabel(title)
    label.setProperty("role", "section")
    h.addWidget(label)
    h.addStretch(1)
    if right is not None:
        h.addWidget(right)
    return w


def label(text="", role=None, wrap=False, center=False):
    lbl = QLabel(text)
    if role:
        lbl.setProperty("role", role)
    lbl.setWordWrap(wrap)
    if center:
        lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
    return lbl


def button(text, kind=None):
    from PySide6.QtWidgets import QPushButton
    b = QPushButton(text)
    if kind:
        b.setProperty("kind", kind)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


def tool_button(glyph, tip):
    b = QToolButton()
    b.setIcon(glyph_icon(glyph, T["secondary"], 18))
    b.setIconSize(QSize(18, 18))
    b.setToolTip(tip)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


# ---- devices on screen -----------------------------------------------------------

class FlowLayout(QLayout):
    """Lays its widgets out in rows that wrap, each row centred."""

    def __init__(self, parent=None, spacing=4):
        super().__init__(parent)
        self._items = []
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._place(QRect(0, 0, width, 0), dry=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._place(rect, dry=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _place(self, rect, dry):
        m = self.contentsMargins()
        r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        sp = self.spacing()
        rows, row, x, row_h = [], [], 0, 0
        for it in self._items:
            if it.isEmpty():
                continue
            s = it.sizeHint()
            if row and x + s.width() > r.width():
                rows.append((row, x - sp, row_h))
                row, x, row_h = [], 0, 0
            row.append((it, s))
            x += s.width() + sp
            row_h = max(row_h, s.height())
        if row:
            rows.append((row, x - sp, row_h))
        y = r.y()
        for items, row_w, row_h in rows:
            x = r.x() + max(0, (r.width() - row_w) // 2)
            for it, s in items:
                if not dry:
                    it.setGeometry(QRect(QPoint(x, y), s))
                x += s.width() + sp
            y += row_h + sp
        used = (y - sp - r.y()) if rows else 0
        return used + m.top() + m.bottom()


def _two_lines(fm, text, width):
    """text in at most two lines of `width`, the second elided."""
    words, first = text.split(), ""
    while words and fm.horizontalAdvance((first + " " + words[0]).strip()) <= width:
        first = (first + " " + words.pop(0)).strip()
    if not first:                              # one long word
        return [fm.elidedText(text, Qt.TextElideMode.ElideRight, width)]
    rest = " ".join(words)
    return [first] + ([fm.elidedText(rest, Qt.TextElideMode.ElideRight, width)] if rest else [])


class DeviceTile(QWidget):
    """One device: avatar, name, and a line saying what is going on; a ring
    around the avatar while waiting for Accept and while sending. Files
    dropped on it are sent to it, a click picks it."""
    clicked = Signal(str)
    dropped = Signal(str, list)
    W, H, AV = 104, 124, 58
    NOTES = {"waiting": "Warten …", "sent": "Gesendet", "declined": "Abgelehnt",
             "failed": "Fehlgeschlagen", "cancelled": "Abgebrochen"}

    def __init__(self, rid, parent=None):
        super().__init__(parent)
        self.rid = rid
        self.name = rid
        self.model = None
        self.named = False
        self.present = True
        self.phase = "idle"
        self.progress = 0.0
        self._hover = self._drag = False
        self._spin = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._step)
        self._reset = QTimer(self)
        self._reset.setSingleShot(True)
        self._reset.timeout.connect(lambda: self.set_phase("idle"))
        self.setFixedSize(self.W, self.H)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_info(self, name, model, present, named):
        self.name, self.model, self.present, self.named = name, model, present, named
        self.setToolTip(name if named else f"{name} (Name noch unbekannt)")
        self.setAccessibleName(name)
        self.update()

    def set_phase(self, phase, progress=0.0):
        self.phase, self.progress = phase, progress
        if phase in ("waiting", "sending"):
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()
        if phase in ("sent", "declined", "failed", "cancelled"):
            self._reset.start(6000)
        else:
            self._reset.stop()
        self.update()

    def caption(self):
        if self.phase == "sending":
            return f"Senden … {int(self.progress * 100)} %"
        if self.phase in self.NOTES:
            return self.NOTES[self.phase]
        if not self.present:
            return "nicht in der Nähe"
        return pretty_model(self.model)

    def _step(self):
        self._spin = (self._spin + 7) % 360
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled() or (not self.present and self.phase == "idle"):
            p.setOpacity(0.45)
        av = QRectF((self.W - self.AV) / 2, 12, self.AV, self.AV)
        if self._hover or self._drag or self.hasFocus():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color("hover"))
            p.drawRoundedRect(QRectF(4, 2, self.W - 8, self.H - 4), 14, 14)
        paint_avatar(p, av, self.name if self.named else None, self.model)
        ring = av.adjusted(-5, -5, 5, 5)
        if self._drag:
            p.setPen(QPen(color("accent"), 3))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(ring)
        elif self.phase in ("waiting", "sending"):
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(color("track"), 3))
            p.drawEllipse(ring)
            pen = QPen(color("accent"), 3)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            if self.phase == "waiting":
                p.drawArc(ring, int((90 - self._spin) * 16), -75 * 16)
            else:
                p.drawArc(ring, 90 * 16, int(-max(0.02, self.progress) * 360 * 16))
        f = font(12, QFont.Weight.Medium)
        p.setFont(f)
        p.setPen(color("text"))
        fm = QFontMetrics(f)
        y = av.bottom() + 10
        for line in _two_lines(fm, self.name, self.W - 10):
            p.drawText(QRectF(5, y, self.W - 10, fm.height()), Qt.AlignmentFlag.AlignHCenter, line)
            y += fm.height()
        note = self.caption()
        if note:
            f2 = font(11)
            p.setFont(f2)
            key = {"sent": "accent", "declined": "red", "failed": "red"}.get(self.phase,
                                                                               "secondary")
            p.setPen(color(key))
            fm2 = QFontMetrics(f2)
            p.drawText(QRectF(5, y + 1, self.W - 10, fm2.height()), Qt.AlignmentFlag.AlignHCenter,
                       fm2.elidedText(note, Qt.TextElideMode.ElideRight, self.W - 10))

    # mouse, keyboard and drops

    def enterEvent(self, e):
        self._hover = True
        self.update()

    def leaveEvent(self, e):
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit(self.rid)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.clicked.emit(self.rid)
        else:
            super().keyPressEvent(e)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and self.isEnabled():
            self._drag = True
            self.update()
            e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self._drag = False
        self.update()

    def dropEvent(self, e):
        self._drag = False
        self.update()
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.dropped.emit(self.rid, paths)
            e.acceptProposedAction()


class DeviceGrid(QWidget):
    """The devices in reach, as tiles in wrapping, centred rows."""
    clicked = Signal(str)
    dropped = Signal(str, list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tiles = {}
        self.flow = FlowLayout(self, spacing=6)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

    def upsert(self, rid, name, model, present, named):
        tile = self.tiles.get(rid)
        if tile is None:
            tile = self.tiles[rid] = DeviceTile(rid, self)
            tile.clicked.connect(self.clicked)
            tile.dropped.connect(self.dropped)
            self.flow.addWidget(tile)
            tile.show()
        tile.set_info(name, model, present, named)
        self.updateGeometry()
        return tile

    def count(self):
        return len(self.tiles)

    def set_tiles_enabled(self, on, except_rid=None):
        for rid, tile in self.tiles.items():
            tile.setEnabled(on or rid == except_rid)


class RoundGlyph(QWidget):
    """A coloured circle with a white line icon (transfer rows)."""

    def __init__(self, glyph, key="accent", px=30, parent=None):
        super().__init__(parent)
        self.glyph, self.key = glyph, key
        self.setFixedSize(px, px)

    def set(self, glyph, key):
        self.glyph, self.key = glyph, key
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color(self.key))
        p.drawEllipse(r)
        paint_glyph(p, self.glyph, r.adjusted(5, 5, -5, -5), "#FFFFFF", 2.0)


class TransferRow(QWidget):
    """One transfer: icon, files, device and state, a progress line while it
    runs; buttons to cancel it or to show the files."""
    cancel = Signal()
    open = Signal()

    def __init__(self, direction, title, device, parent=None):
        super().__init__(parent)
        self.direction = direction             # "send" or "receive"
        self.files = []
        self._top_line = False
        h = QHBoxLayout(self)
        h.setContentsMargins(14, 10, 10, 10)
        h.setSpacing(12)
        self.icon = RoundGlyph("up" if direction == "send" else "down",
                               "accent" if direction == "send" else "green")
        h.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignVCenter)
        v = QVBoxLayout()
        v.setSpacing(3)
        self.title = ElidedLabel(title)
        self.title.setFont(font(13, QFont.Weight.Medium))
        self.sub = QLabel()
        self.sub.setProperty("role", "caption")
        self.bar = ThinProgress()
        self.bar.hide()
        v.addWidget(self.title)
        v.addWidget(self.sub)
        v.addWidget(self.bar)
        h.addLayout(v, 1)
        self.cancel_btn = tool_button("x", "Abbrechen")
        self.cancel_btn.clicked.connect(self.cancel)
        self.open_btn = tool_button("search", "Im Ordner zeigen")
        self.open_btn.clicked.connect(self.open)
        h.addWidget(self.cancel_btn)
        h.addWidget(self.open_btn)
        self.cancel_btn.hide()
        self.open_btn.hide()
        self.device = device
        self._status = ""
        self.set_state("")

    def set_title(self, title):
        self.title.setText(title)

    def set_state(self, status, running=False, progress=None, files=None, tone=None):
        """tone: None, "done" or "bad" (the icon turns into a check or a mark)."""
        self._status = status
        self.sub.setText(f"{self.device} · {status}" if status else self.device)
        self.bar.setVisible(running)
        if running:
            self.bar.set_value(progress)
        self.cancel_btn.setVisible(running and self.direction == "send")
        if files is not None:
            self.files = files
        self.open_btn.setVisible(bool(self.files) and not running)
        if tone == "done":
            self.icon.set("check", "accent" if self.direction == "send" else "green")
        elif tone == "bad":
            self.icon.set("alert", "red")

    def status_text(self):
        return self._status

    def title_text(self):
        return self.title.full_text()

    def set_top_line(self, on):
        self._top_line = on
        self.update()

    def paintEvent(self, e):
        if self._top_line:
            p = QPainter(self)
            p.fillRect(QRectF(56, 0, self.width() - 56, 1), color("separator"))


class TransferList(QWidget):
    """The transfers, newest on top; a gray line when there are none."""

    def __init__(self, keep=30, parent=None):
        super().__init__(parent)
        self.rows = []
        self.keep = keep
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(0)
        self.placeholder = label("Noch keine Übertragungen.", "secondary", center=True)
        self.placeholder.setContentsMargins(0, 16, 0, 16)
        self.box.addWidget(self.placeholder)

    def add(self, row):
        self.placeholder.hide()
        self.rows.insert(0, row)
        self.box.insertWidget(0, row)
        while len(self.rows) > self.keep:
            old = self.rows.pop()
            old.setParent(None)
            old.deleteLater()
        self._lines()
        return row

    def remove(self, row):
        if row in self.rows:
            self.rows.remove(row)
            row.setParent(None)
            row.deleteLater()
        self.placeholder.setVisible(not self.rows)
        self._lines()

    def count(self):
        return len(self.rows)

    def _lines(self):
        for k, row in enumerate(self.rows):
            row.set_top_line(k > 0)
