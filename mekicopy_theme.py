from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

PINK = "#ff7da4"
ROSE = "#f86e83"
OUTLINE = "#feb3c7"
BORDER = "#ffd3de"
SOFT = "#ffe6ec"
BG = "#fff4f7"
SURFACE = "#ffffff"
INK = "#2d292a"
MUTED = "#b09ca3"
LILAC = "#f7ecff"
SUCCESS = "#31b66b"
DISABLED_BG = "#f6edf1"
DISABLED_FG = "#b9a5ad"

DEFAULT_FONT = ("Malgun Gothic", 9)
TITLE_FONT = ("Malgun Gothic", 13, "bold")
BUTTON_FONT = ("Malgun Gothic", 10, "bold")
SMALL_FONT = ("Malgun Gothic", 8)


def configure_window_theme(window: tk.Misc) -> None:
    window.option_add("*Font", DEFAULT_FONT)
    window.option_add("*Dialog.msg.font", DEFAULT_FONT)
    window.option_add("*Menu.background", SURFACE)
    window.option_add("*Menu.foreground", INK)
    window.option_add("*Menu.activeBackground", SOFT)
    window.option_add("*Menu.activeForeground", ROSE)
    try:
        window.configure(bg=BG)
    except tk.TclError:
        pass
    configure_ttk_styles(window)


def configure_ttk_styles(root: tk.Misc) -> None:
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(4, 4, 4, 0))
    style.configure(
        "TNotebook.Tab",
        background=SOFT,
        foreground=INK,
        bordercolor=BORDER,
        lightcolor=SURFACE,
        darkcolor=BORDER,
        padding=(14, 7),
        font=BUTTON_FONT,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", SURFACE), ("active", LILAC)],
        foreground=[("selected", ROSE), ("active", ROSE)],
    )
    style.configure(
        "Vertical.TScrollbar",
        background=SOFT,
        bordercolor=BORDER,
        arrowcolor=ROSE,
        troughcolor=BG,
        relief="flat",
    )


def style_standard_button(button: tk.Button, variant: str = "normal") -> None:
    palette = _button_palette(variant)
    button.configure(
        bg=palette["bg"],
        fg=palette["fg"],
        activebackground=palette["active_bg"],
        activeforeground=palette["active_fg"],
        disabledforeground=DISABLED_FG,
        relief=tk.FLAT,
        bd=1,
        highlightthickness=1,
        highlightbackground=palette["outline"],
        highlightcolor=palette["outline"],
        cursor="hand2",
        font=BUTTON_FONT,
        padx=10,
        pady=6,
    )


def style_color_button(button: tk.Button, color: str) -> None:
    fg = INK if _is_light_color(color) else SURFACE
    button.configure(
        bg=color,
        fg=fg,
        activebackground=color,
        activeforeground=fg,
        relief=tk.FLAT,
        bd=1,
        highlightthickness=1,
        highlightbackground=BORDER,
        highlightcolor=ROSE,
        cursor="hand2",
        font=BUTTON_FONT,
    )


def style_tree(widget: tk.Misc) -> None:
    if isinstance(widget, RoundedButton):
        return
    if isinstance(widget, (tk.Tk, tk.Toplevel, tk.Frame)):
        current_bg = _current_bg(widget)
        if current_bg in {"SystemButtonFace", "#f0f0f0"}:
            _safe_config(widget, bg=BG)
    elif isinstance(widget, tk.LabelFrame):
        _safe_config(
            widget,
            bg=_parent_bg(widget),
            fg=ROSE,
            font=BUTTON_FONT,
            bd=1,
            relief=tk.GROOVE,
        )
    elif isinstance(widget, tk.Label):
        _safe_config(widget, bg=_parent_bg(widget), fg=INK)
    elif isinstance(widget, tk.Checkbutton):
        _safe_config(
            widget,
            bg=SOFT,
            fg=INK,
            activebackground=SOFT,
            activeforeground=ROSE,
            selectcolor=SURFACE,
            highlightthickness=0,
            font=DEFAULT_FONT,
        )
    elif isinstance(widget, tk.Button):
        style_standard_button(widget)
    elif isinstance(widget, tk.Scale):
        _safe_config(
            widget,
            bg=BG,
            fg=INK,
            activebackground=SOFT,
            troughcolor=SOFT,
            highlightthickness=0,
        )
    elif isinstance(widget, tk.Spinbox):
        _safe_config(
            widget,
            bg=SURFACE,
            fg=INK,
            buttonbackground=SOFT,
            highlightbackground=BORDER,
            highlightcolor=ROSE,
            insertbackground=ROSE,
            relief=tk.FLAT,
        )
    elif isinstance(widget, tk.Listbox):
        _safe_config(
            widget,
            bg=SURFACE,
            fg=INK,
            selectbackground=SOFT,
            selectforeground=ROSE,
            highlightbackground=BORDER,
            highlightcolor=ROSE,
            relief=tk.FLAT,
        )
    elif widget.winfo_class() == "Menubutton":
        _safe_config(
            widget,
            bg=SURFACE,
            fg=INK,
            activebackground=SOFT,
            activeforeground=ROSE,
            highlightbackground=BORDER,
            highlightcolor=ROSE,
            relief=tk.FLAT,
        )
        try:
            menu = widget["menu"]
            widget.nametowidget(menu).configure(
                bg=SURFACE,
                fg=INK,
                activebackground=SOFT,
                activeforeground=ROSE,
                relief=tk.FLAT,
            )
        except (KeyError, tk.TclError):
            pass

    for child in widget.winfo_children():
        style_tree(child)


def _safe_config(widget: tk.Misc, **kwargs: Any) -> None:
    try:
        widget.configure(**kwargs)
    except tk.TclError:
        pass


def _button_palette(variant: str) -> dict[str, str]:
    if variant == "primary":
        return {
            "bg": PINK,
            "fg": SURFACE,
            "active_bg": ROSE,
            "active_fg": SURFACE,
            "outline": ROSE,
        }
    if variant == "tab-active":
        return {
            "bg": SURFACE,
            "fg": ROSE,
            "active_bg": SOFT,
            "active_fg": ROSE,
            "outline": OUTLINE,
        }
    if variant == "success":
        return {
            "bg": SUCCESS,
            "fg": SURFACE,
            "active_bg": SUCCESS,
            "active_fg": SURFACE,
            "outline": SUCCESS,
        }
    return {
        "bg": SOFT,
        "fg": INK,
        "active_bg": LILAC,
        "active_fg": ROSE,
        "outline": BORDER,
    }


def _is_light_color(color: str) -> bool:
    if not isinstance(color, str) or not color.startswith("#") or len(color) != 7:
        return True
    try:
        red = int(color[1:3], 16)
        green = int(color[3:5], 16)
        blue = int(color[5:7], 16)
    except ValueError:
        return True
    return (red * 299 + green * 587 + blue * 114) > 150000


class RoundedButton(tk.Canvas):
    def __init__(
        self,
        master: tk.Misc,
        text: str = "",
        command: Callable[[], None] | None = None,
        *,
        height: int = 38,
        radius: int = 16,
        anchor: str = "center",
        variant: str = "normal",
        font: tuple[str, int] | tuple[str, int, str] = BUTTON_FONT,
        **kwargs: Any,
    ) -> None:
        palette = _button_palette(variant)
        canvas_bg = _parent_bg(master)
        super().__init__(
            master,
            height=height,
            highlightthickness=0,
            bd=0,
            bg=canvas_bg,
            takefocus=1,
            cursor="hand2",
        )
        self._command = command
        self._height = height
        self._radius = radius
        self._anchor = anchor
        self._hover = False
        self._pressed = False
        self._button_options: dict[str, Any] = {
            "text": text,
            "state": kwargs.pop("state", tk.NORMAL),
            "bg": kwargs.pop("bg", kwargs.pop("background", palette["bg"])),
            "fg": kwargs.pop("fg", kwargs.pop("foreground", palette["fg"])),
            "activebackground": kwargs.pop("activebackground", palette["active_bg"]),
            "activeforeground": kwargs.pop("activeforeground", palette["active_fg"]),
            "disabledforeground": kwargs.pop("disabledforeground", DISABLED_FG),
            "outline": kwargs.pop("outline", palette["outline"]),
            "font": kwargs.pop("font", font),
        }
        self.configure(**kwargs)
        self.bind("<Configure>", lambda _event: self._redraw(), add="+")
        self.bind("<Enter>", self._on_enter, add="+")
        self.bind("<Leave>", self._on_leave, add="+")
        self.bind("<ButtonPress-1>", self._on_press, add="+")
        self.bind("<ButtonRelease-1>", self._on_release, add="+")
        self.bind("<Key-space>", lambda _event: self.invoke(), add="+")
        self.bind("<Key-Return>", lambda _event: self.invoke(), add="+")
        self.bind("<FocusIn>", lambda _event: self._redraw(), add="+")
        self.bind("<FocusOut>", lambda _event: self._redraw(), add="+")
        self._redraw()

    def configure(self, cnf: dict[str, Any] | None = None, **kwargs: Any) -> None:  # type: ignore[override]
        if cnf:
            kwargs.update(cnf)
        redraw = False
        canvas_options: dict[str, Any] = {}
        for key, value in kwargs.items():
            normalized = "bg" if key == "background" else key
            if normalized == "command":
                self._command = value
            elif normalized in self._button_options:
                self._button_options[normalized] = value
                redraw = True
            elif normalized == "text":
                self._button_options["text"] = value
                redraw = True
            elif normalized == "anchor":
                self._anchor = value
                redraw = True
            elif normalized in {"padx", "pady", "relief", "bd", "borderwidth"}:
                continue
            else:
                canvas_options[key] = value
        if canvas_options:
            super().configure(**canvas_options)
        if redraw:
            self._redraw()

    config = configure

    def cget(self, key: str) -> Any:  # type: ignore[override]
        normalized = "bg" if key == "background" else key
        if normalized in self._button_options:
            return self._button_options[normalized]
        if normalized == "command":
            return self._command
        return super().cget(key)

    def invoke(self) -> None:
        if self._button_options.get("state") == tk.DISABLED:
            return
        if self._command is not None:
            self._command()

    def _on_enter(self, _event: tk.Event) -> None:
        self._hover = True
        self._redraw()

    def _on_leave(self, _event: tk.Event) -> None:
        self._hover = False
        self._pressed = False
        self._redraw()

    def _on_press(self, _event: tk.Event) -> None:
        if self._button_options.get("state") == tk.DISABLED:
            return
        self.focus_set()
        self._pressed = True
        self._redraw()

    def _on_release(self, event: tk.Event) -> None:
        if self._button_options.get("state") == tk.DISABLED:
            return
        was_pressed = self._pressed
        self._pressed = False
        self._redraw()
        if was_pressed and 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height():
            self.invoke()

    def _redraw(self) -> None:
        try:
            self.delete("all")
        except tk.TclError:
            return
        width = max(2, self.winfo_width())
        height = max(2, self.winfo_height() or self._height)
        state = self._button_options.get("state")
        disabled = state == tk.DISABLED
        fill = DISABLED_BG if disabled else self._button_options["bg"]
        text_fill = (
            self._button_options["disabledforeground"]
            if disabled
            else self._button_options["fg"]
        )
        if not disabled and (self._hover or self._pressed):
            fill = self._button_options["activebackground"]
            text_fill = self._button_options["activeforeground"]
        outline = OUTLINE if self.focus_get() is self else self._button_options["outline"]
        if disabled:
            outline = BORDER
        y_offset = 1 if self._pressed else 0
        _rounded_rect(
            self,
            1,
            1 + y_offset,
            width - 1,
            height - 2 + y_offset,
            min(self._radius, height // 2),
            fill=fill,
            outline=outline,
            width=1,
        )
        if self._anchor == "w":
            x = 14
            anchor = "w"
            wrap = max(40, width - 28)
        else:
            x = width // 2
            anchor = "center"
            wrap = max(40, width - 24)
        self.create_text(
            x,
            height // 2 + y_offset,
            text=str(self._button_options["text"]),
            fill=text_fill,
            font=self._button_options["font"],
            anchor=anchor,
            width=wrap,
            justify="center",
        )


def _rounded_rect(
    canvas: tk.Canvas,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    radius: int,
    **kwargs: Any,
) -> None:
    radius = max(1, min(radius, (x2 - x1) // 2, (y2 - y1) // 2))
    points = [
        x1 + radius,
        y1,
        x2 - radius,
        y1,
        x2,
        y1,
        x2,
        y1 + radius,
        x2,
        y2 - radius,
        x2,
        y2,
        x2 - radius,
        y2,
        x1 + radius,
        y2,
        x1,
        y2,
        x1,
        y2 - radius,
        x1,
        y1 + radius,
        x1,
        y1,
    ]
    canvas.create_polygon(points, smooth=True, splinesteps=18, **kwargs)


def _parent_bg(master: tk.Misc) -> str:
    try:
        return str(master.cget("bg"))
    except tk.TclError:
        return BG


def _current_bg(widget: tk.Misc) -> str:
    try:
        return str(widget.cget("bg"))
    except tk.TclError:
        return BG
