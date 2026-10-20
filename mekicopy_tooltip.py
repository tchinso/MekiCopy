from __future__ import annotations

import tkinter as tk

from mekicopy_theme import BORDER, INK, SURFACE


class Tooltip:
    """Show help while the pointer is over a widget."""

    def __init__(self, widget: tk.Widget, text: str, *, wraplength: int = 430) -> None:
        self.widget = widget
        self.text = text
        self.wraplength = wraplength
        self.window: tk.Toplevel | None = None
        self._bindings: list[tuple[tk.Misc, str, str]] = []
        self._bind(widget, "<Enter>", self._show)
        self._bind(widget, "<Leave>", self._hide)
        self._bind(widget, "<Unmap>", self._hide)
        self._bind(widget, "<Destroy>", self._dispose)
        self._bind(widget.winfo_toplevel(), "<Unmap>", self._hide)

    def _bind(self, widget: tk.Misc, sequence: str, callback) -> None:
        binding = widget.bind(sequence, callback, add="+")
        self._bindings.append((widget, sequence, binding))

    def _show(self, _event: tk.Event | None = None) -> None:
        if self.window is not None or not self.widget.winfo_ismapped():
            return
        window = tk.Toplevel(self.widget)
        self.window = window
        window.withdraw()
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        tk.Label(
            window,
            text=self.text,
            justify=tk.LEFT,
            anchor="w",
            wraplength=self.wraplength,
            padx=10,
            pady=8,
            bg=SURFACE,
            fg=INK,
            highlightbackground=BORDER,
            highlightthickness=1,
        ).pack()
        window.update_idletasks()
        width, height = window.winfo_reqwidth(), window.winfo_reqheight()
        x = self.widget.winfo_rootx() + self.widget.winfo_width() + 8
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        x = max(0, min(x, window.winfo_screenwidth() - width))
        y = max(0, min(y, window.winfo_screenheight() - height))
        window.geometry(f"+{x}+{y}")
        window.deiconify()

    def _hide(self, _event: tk.Event | None = None) -> None:
        window, self.window = self.window, None
        if window is not None:
            try:
                window.destroy()
            except tk.TclError:
                pass

    def _dispose(self, _event: tk.Event | None = None) -> None:
        self._hide()
        for widget, sequence, binding in self._bindings:
            try:
                widget.unbind(sequence, binding)
            except tk.TclError:
                pass
        self._bindings.clear()
