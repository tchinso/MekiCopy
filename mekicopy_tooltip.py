from html import escape

from PySide6.QtWidgets import QWidget


def Tooltip(widget: QWidget, text: str, *, wraplength: int = 430) -> None:
    widget.setToolTip(f'<p style="white-space: normal; width: {wraplength}px">{escape(text)}</p>')
