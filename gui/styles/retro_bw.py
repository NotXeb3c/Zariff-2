"""Retro black & white Zariff theme."""

RETRO_STYLESHEET = """
QMainWindow, QWidget {
    background-color: #0a0a0a;
    color: #e8e8e8;
    font-family: "Consolas", "Courier New", monospace;
    font-size: 13px;
}

QScrollArea {
    border: none;
    background: transparent;
}

QScrollBar:vertical {
    background: #111111;
    width: 8px;
    border: 1px solid #333333;
}
QScrollBar::handle:vertical {
    background: #666666;
    min-height: 24px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}

QLineEdit, QTextEdit, QPlainTextEdit {
    background-color: #0f0f0f;
    color: #ffffff;
    border: 1px solid #555555;
    border-radius: 0px;
    padding: 8px;
    selection-background-color: #333333;
}

QLineEdit:focus, QTextEdit:focus {
    border: 1px solid #ffffff;
}

QPushButton {
    background-color: #1a1a1a;
    color: #ffffff;
    border: 1px solid #888888;
    border-radius: 0px;
    padding: 8px 16px;
    font-weight: bold;
    text-transform: uppercase;
    letter-spacing: 1px;
}
QPushButton:hover {
    background-color: #2a2a2a;
    border: 1px solid #ffffff;
}
QPushButton:pressed {
    background-color: #000000;
}
QPushButton:disabled {
    color: #555555;
    border-color: #333333;
}

QListWidget {
    background-color: #0d0d0d;
    border: 1px solid #444444;
    outline: none;
}
QListWidget::item {
    padding: 10px;
    border-bottom: 1px solid #222222;
}
QListWidget::item:selected {
    background-color: #222222;
    color: #ffffff;
    border-left: 3px solid #ffffff;
}

QLabel#titleLabel {
    font-size: 28px;
    font-weight: bold;
    letter-spacing: 12px;
    color: #ffffff;
}

QLabel#statusLabel {
    color: #aaaaaa;
    font-size: 11px;
    letter-spacing: 2px;
}

QFrame#panel {
    border: 1px solid #444444;
    background-color: #0c0c0c;
}

QFrame#sidebar {
    background-color: #050505;
    border-right: 1px solid #333333;
}

QSlider::groove:horizontal {
    height: 4px;
    background: #333333;
}
QSlider::handle:horizontal {
    width: 12px;
    margin: -4px 0;
    background: #ffffff;
}

QCheckBox {
    spacing: 8px;
}
QCheckBox::indicator {
    width: 14px;
    height: 14px;
    border: 1px solid #888888;
    background: #111111;
}
QCheckBox::indicator:checked {
    background: #ffffff;
}

QComboBox {
    background: #111111;
    border: 1px solid #555555;
    padding: 6px;
    color: #ffffff;
}
QComboBox::drop-down {
    border: none;
}
QComboBox QAbstractItemView {
    background: #111111;
    color: #ffffff;
    selection-background-color: #333333;
}

QTabWidget::pane {
    border: 1px solid #444444;
    background: #0a0a0a;
}
QTabBar::tab {
    background: #111111;
    color: #888888;
    padding: 8px 16px;
    border: 1px solid #333333;
}
QTabBar::tab:selected {
    color: #ffffff;
    border-bottom: 2px solid #ffffff;
}
"""
