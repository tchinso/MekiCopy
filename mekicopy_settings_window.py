from __future__ import annotations

import tkinter as tk
from tkinter import colorchooser, messagebox, ttk

from mekicopy_runtime import _set_window_icon
from mekicopy_settings import (
    AppSettings,
    _geometry_size,
    _japanese_font_families,
    _korean_font_families,
    _normalize_hex_color,
    _normalize_font_name,
    _normalize_port,
    load_detached_geometry,
)
from service_ports import validate_unique_ports
from mekicopy_theme import (
    BG,
    BORDER,
    INK,
    ROSE,
    SOFT,
    SURFACE,
    configure_window_theme,
    style_color_button,
    style_standard_button,
    style_tree,
)


class _ScrollableTab(tk.Frame):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, bg=BG)
        self.canvas = tk.Canvas(
            self,
            bg=BG,
            highlightthickness=0,
            bd=0,
        )
        self.scrollbar = ttk.Scrollbar(
            self,
            orient=tk.VERTICAL,
            command=self.canvas.yview,
        )
        self.interior = tk.Frame(self.canvas, bg=BG, padx=12, pady=12)
        self._window_id = self.canvas.create_window(
            (0, 0),
            window=self.interior,
            anchor="nw",
        )
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.interior.bind("<Configure>", self._on_interior_configure, add="+")
        self.canvas.bind("<Configure>", self._on_canvas_configure, add="+")
        self.canvas.bind("<Enter>", self._bind_mousewheel, add="+")
        self.canvas.bind("<Leave>", self._unbind_mousewheel, add="+")

    def _on_interior_configure(self, _event: tk.Event) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self._window_id, width=event.width)

    def _bind_mousewheel(self, _event: tk.Event) -> None:
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel, add="+")

    def _unbind_mousewheel(self, _event: tk.Event) -> None:
        self.canvas.unbind_all("<MouseWheel>")

    def _on_mousewheel(self, event: tk.Event) -> None:
        if self.canvas.bbox("all") is None:
            return
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

class SettingsWindow(tk.Toplevel):
    def __init__(self, owner) -> None:
        super().__init__(owner)
        self.owner = owner
        self.title("MekiCopy 설정")
        configure_window_theme(self)
        self.resizable(True, True)
        self._set_safe_geometry(580, 720)
        self.minsize(560, 520)
        _set_window_icon(self)

        settings = owner.settings
        self.minimize_to_tray_var = tk.BooleanVar(value=settings.minimize_to_tray)
        self.main_topmost_var = tk.BooleanVar(value=settings.main_always_on_top)
        self.detached_topmost_var = tk.BooleanVar(value=settings.detached_always_on_top)
        self.detached_hide_titlebar_var = tk.BooleanVar(
            value=settings.detached_hide_titlebar
        )
        self.detached_fixed_size_var = tk.BooleanVar(value=settings.detached_fixed_size)
        self.simple_copy_complete_var = tk.BooleanVar(
            value=settings.simple_copy_complete
        )
        self.overlay_mode_var = tk.BooleanVar(value=settings.overlay_translation_mode)
        self.hytrans_port_var = tk.IntVar(value=settings.hytrans_port)
        self.overlayer_port_var = tk.IntVar(value=settings.overlayer_port)
        self.audio_capture_port_var = tk.IntVar(value=settings.audio_capture_port)
        self.script_port_var = tk.IntVar(value=settings.script_port)
        self.overlayer_topmost_var = tk.BooleanVar(value=settings.overlayer_always_on_top)
        self.overlayer_hide_titlebar_var = tk.BooleanVar(
            value=settings.overlayer_hide_titlebar
        )
        self.overlayer_fixed_size_var = tk.BooleanVar(value=settings.overlayer_fixed_size)
        self.overlayer_exclude_capture_var = tk.BooleanVar(
            value=settings.overlayer_exclude_from_capture
        )
        self.overlayer_bg_color_var = tk.StringVar(value=settings.overlayer_bg_color)
        self.overlayer_opacity_var = tk.IntVar(
            value=max(10, min(100, int(settings.overlayer_bg_opacity * 100)))
        )
        self.overlayer_text_color_var = tk.StringVar(value=settings.overlayer_text_color)
        self.overlayer_text_size_var = tk.IntVar(value=settings.overlayer_text_size)
        self.overlayer_text_font_var = tk.StringVar(value=settings.overlayer_text_font)
        self.audio_stt_precision_var = tk.StringVar(value=settings.audio_stt_precision)
        self.audio_chunk_preset_var = tk.StringVar(value=settings.audio_chunk_preset)
        self.script_topmost_var = tk.BooleanVar(value=settings.script_always_on_top)
        self.script_bg_color_var = tk.StringVar(value=settings.script_bg_color)
        self.script_opacity_var = tk.IntVar(
            value=max(10, min(100, int(settings.script_bg_opacity * 100)))
        )
        self.script_original_color_var = tk.StringVar(value=settings.script_original_text_color)
        self.script_original_size_var = tk.IntVar(value=settings.script_original_text_size)
        self.script_original_font_var = tk.StringVar(value=settings.script_original_text_font)
        self.script_translated_color_var = tk.StringVar(value=settings.script_translated_text_color)
        self.script_translated_size_var = tk.IntVar(value=settings.script_translated_text_size)
        self.script_translated_font_var = tk.StringVar(value=settings.script_translated_text_font)
        self.suppress_magpie_notice_var = tk.BooleanVar(
            value=settings.suppress_magpie_launch_notice
        )
        self.debug_logging_var = tk.BooleanVar(value=settings.debug_logging)
        self.overlay_only_widgets: list[tk.Widget] = []
        self.detached_label_controls: list[tuple[tk.Widget, str]] = []
        self._color_buttons: list[tuple[tk.Button, tk.StringVar]] = []

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.transient(owner)
        self.attributes("-topmost", settings.main_always_on_top)
        self.overlay_mode_var.trace_add("write", lambda *_: self._on_overlay_mode_changed())
        self._update_mode_labels()
        self._update_overlay_controls()

    def _set_safe_geometry(self, width: int, height: int) -> None:
        safe_height = max(520, min(height, self.winfo_screenheight() - 90))
        safe_width = max(560, min(width, self.winfo_screenwidth() - 80))
        self.geometry(f"{safe_width}x{safe_height}")

    def _add_scrollable_tab(self, notebook: ttk.Notebook, title: str) -> tk.Frame:
        wrapper = _ScrollableTab(notebook)
        notebook.add(wrapper, text=title)
        return wrapper.interior

    def _build_ui(self) -> None:
        body = tk.Frame(self, padx=14, pady=14, bg=BG)
        body.pack(fill=tk.BOTH, expand=True)

        notebook = ttk.Notebook(body)
        notebook.pack(fill=tk.BOTH, expand=True)
        general_tab = self._add_scrollable_tab(notebook, "일반")
        overlay_tab = self._add_scrollable_tab(notebook, "번역 오버레이")
        audio_tab = self._add_scrollable_tab(notebook, "음성인식")

        options = [
            (
                "MekiCopy가 최소화되면 시스템 트레이로 이동",
                self.minimize_to_tray_var,
            ),
            ("MekiCopy를 항상 위로", self.main_topmost_var),
            ("복사 완료를 간단하게 표시하기", self.simple_copy_complete_var),
        ]
        for text, variable in options:
            checkbox = tk.Checkbutton(general_tab, text=text, variable=variable, anchor="w")
            checkbox.pack(fill=tk.X, pady=4)

        detached_options = [
            ("버튼을 항상 위로", self.detached_topmost_var),
            ("버튼의 제목표시줄 숨김", self.detached_hide_titlebar_var),
            ("버튼의 크기를 고정", self.detached_fixed_size_var),
        ]
        for suffix, variable in detached_options:
            checkbox = tk.Checkbutton(general_tab, variable=variable, anchor="w")
            checkbox.pack(fill=tk.X, pady=4)
            self.detached_label_controls.append((checkbox, suffix))

        suppress_magpie_notice_checkbox = tk.Checkbutton(
            general_tab,
            text="MagPie 실행 시 안내 띄우지 않기",
            variable=self.suppress_magpie_notice_var,
            anchor="w",
        )
        suppress_magpie_notice_checkbox.pack(fill=tk.X, pady=4)

        debug_checkbox = tk.Checkbutton(
            general_tab,
            text="오류 분석을 위한 디버그 로그 켜기",
            variable=self.debug_logging_var,
            anchor="w",
        )
        debug_checkbox.pack(fill=tk.X, pady=(4, 8))

        overlay_frame = tk.LabelFrame(overlay_tab, text="번역 오버레이 모드", padx=10, pady=8)
        overlay_frame.pack(fill=tk.BOTH, expand=True)

        overlay_checkbox = tk.Checkbutton(
            overlay_frame,
            text="오버레이어 번역 모드 사용",
            variable=self.overlay_mode_var,
            anchor="w",
        )
        overlay_checkbox.pack(fill=tk.X, pady=3)

        port_row = tk.Frame(overlay_frame)
        port_row.pack(fill=tk.X, pady=3)
        port_label = tk.Label(port_row, text="HYTrans 포트")
        port_label.pack(side=tk.LEFT)
        port_spin = tk.Spinbox(
            port_row,
            from_=1,
            to=65535,
            width=8,
            textvariable=self.hytrans_port_var,
        )
        port_spin.pack(side=tk.RIGHT)
        self.overlay_only_widgets.extend([port_label, port_spin])

        overlayer_port_row = tk.Frame(overlay_frame)
        overlayer_port_row.pack(fill=tk.X, pady=3)
        overlayer_port_label = tk.Label(overlayer_port_row, text="MekiOverlayer 포트")
        overlayer_port_label.pack(side=tk.LEFT)
        overlayer_port_spin = tk.Spinbox(
            overlayer_port_row,
            from_=1,
            to=65535,
            width=8,
            textvariable=self.overlayer_port_var,
        )
        overlayer_port_spin.pack(side=tk.RIGHT)
        self.overlay_only_widgets.extend([overlayer_port_label, overlayer_port_spin])

        overlayer_options = [
            ("MekiOverlayer를 항상 위로", self.overlayer_topmost_var),
            ("MekiOverlayer의 제목표시줄 숨김", self.overlayer_hide_titlebar_var),
            ("MekiOverlayer 크기 고정", self.overlayer_fixed_size_var),
            (
                "MekiOverlayer가 캡쳐되지 않도록 방지",
                self.overlayer_exclude_capture_var,
            ),
        ]
        for text, variable in overlayer_options:
            checkbox = tk.Checkbutton(
                overlay_frame,
                text=text,
                variable=variable,
                anchor="w",
            )
            checkbox.pack(fill=tk.X, pady=3)
            self.overlay_only_widgets.append(checkbox)

        overlayer_style = tk.LabelFrame(
            overlay_frame,
            text="MekiOverlayer 설정",
            padx=8,
            pady=8,
        )
        overlayer_style.pack(fill=tk.X, pady=(8, 2))
        self.overlay_only_widgets.append(overlayer_style)

        bg_button = tk.Button(
            overlayer_style,
            text="배경색깔",
            command=lambda: self._choose_color(self.overlayer_bg_color_var, bg_button),
        )
        bg_button.pack(fill=tk.X, pady=2)
        self._color_buttons.append((bg_button, self.overlayer_bg_color_var))
        self.overlay_only_widgets.append(bg_button)

        opacity_row = tk.Frame(overlayer_style)
        opacity_row.pack(fill=tk.X, pady=2)
        tk.Label(opacity_row, text="배경 투명도").pack(side=tk.LEFT)
        opacity_scale = tk.Scale(
            opacity_row,
            from_=10,
            to=100,
            orient=tk.HORIZONTAL,
            showvalue=True,
            variable=self.overlayer_opacity_var,
            length=180,
        )
        opacity_scale.pack(side=tk.RIGHT)
        self.overlay_only_widgets.extend([opacity_row, opacity_scale])

        text_color_button = tk.Button(
            overlayer_style,
            text="글씨 색깔",
            command=lambda: self._choose_color(
                self.overlayer_text_color_var, text_color_button
            ),
        )
        text_color_button.pack(fill=tk.X, pady=2)
        self._color_buttons.append((text_color_button, self.overlayer_text_color_var))
        self.overlay_only_widgets.append(text_color_button)

        size_row = tk.Frame(overlayer_style)
        size_row.pack(fill=tk.X, pady=2)
        tk.Label(size_row, text="글씨 크기").pack(side=tk.LEFT)
        size_spin = tk.Spinbox(
            size_row,
            from_=8,
            to=96,
            width=6,
            textvariable=self.overlayer_text_size_var,
        )
        size_spin.pack(side=tk.RIGHT)
        self.overlay_only_widgets.extend([size_row, size_spin])

        font_row = tk.Frame(overlayer_style)
        font_row.pack(fill=tk.X, pady=2)
        tk.Label(font_row, text="글씨 폰트").pack(side=tk.LEFT)
        font_names = _korean_font_families(self)
        current_font = _normalize_font_name(self.overlayer_text_font_var.get())
        if font_names:
            selected_font = current_font if current_font in font_names else (
                "Malgun Gothic" if "Malgun Gothic" in font_names else (
                    "맑은 고딕" if "맑은 고딕" in font_names else font_names[0]
                )
            )
            self.overlayer_text_font_var.set(selected_font)
            menu_values = font_names
        else:
            self.overlayer_text_font_var.set("")
            menu_values = [""]
        self.korean_font_names = font_names
        font_menu = tk.OptionMenu(
            font_row,
            self.overlayer_text_font_var,
            *menu_values,
        )
        font_menu.config(width=20)
        if not font_names:
            font_menu.config(state=tk.DISABLED)
        font_menu.pack(side=tk.RIGHT)
        self.overlay_only_widgets.extend([font_row, font_menu])

        audio_model_frame = tk.LabelFrame(audio_tab, text="음성인식", padx=10, pady=8)
        audio_model_frame.pack(fill=tk.X)
        precision_row = tk.Frame(audio_model_frame)
        precision_row.pack(fill=tk.X, pady=3)
        tk.Label(precision_row, text="음성인식 모델").pack(side=tk.LEFT)
        tk.OptionMenu(precision_row, self.audio_stt_precision_var, "fp32", "int8").pack(side=tk.RIGHT)
        audio_port_row = tk.Frame(audio_model_frame)
        audio_port_row.pack(fill=tk.X, pady=3)
        tk.Label(audio_port_row, text="MekiAudioCapture 포트").pack(side=tk.LEFT)
        tk.Spinbox(
            audio_port_row,
            from_=1,
            to=65535,
            width=8,
            textvariable=self.audio_capture_port_var,
        ).pack(side=tk.RIGHT)
        preset_row = tk.Frame(audio_model_frame)
        preset_row.pack(fill=tk.X, pady=3)
        tk.Label(preset_row, text="음성 CHUNK 기준").pack(side=tk.LEFT)
        tk.OptionMenu(preset_row, self.audio_chunk_preset_var, "FAST", "BALANCED", "LONG").pack(side=tk.RIGHT)

        script_frame = tk.LabelFrame(audio_tab, text="MekiScript", padx=10, pady=8)
        script_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        script_port_row = tk.Frame(script_frame)
        script_port_row.pack(fill=tk.X, pady=2)
        tk.Label(script_port_row, text="MekiScript 포트").pack(side=tk.LEFT)
        tk.Spinbox(
            script_port_row,
            from_=1,
            to=65535,
            width=8,
            textvariable=self.script_port_var,
        ).pack(side=tk.RIGHT)
        tk.Checkbutton(
            script_frame,
            text="MekiScript를 항상 위로",
            variable=self.script_topmost_var,
            anchor="w",
        ).pack(fill=tk.X, pady=2)

        def color_button(text: str, variable: tk.StringVar) -> None:
            button = tk.Button(script_frame, text=text)
            button.configure(command=lambda: self._choose_color(variable, button))
            button.pack(fill=tk.X, pady=2)
            self._color_buttons.append((button, variable))

        color_button("배경색", self.script_bg_color_var)
        script_opacity_row = tk.Frame(script_frame)
        script_opacity_row.pack(fill=tk.X, pady=2)
        tk.Label(script_opacity_row, text="배경 투명도").pack(side=tk.LEFT)
        tk.Scale(
            script_opacity_row, from_=10, to=100, orient=tk.HORIZONTAL,
            variable=self.script_opacity_var, length=180,
        ).pack(side=tk.RIGHT)
        color_button("미번역 글씨 색깔", self.script_original_color_var)

        original_size_row = tk.Frame(script_frame)
        original_size_row.pack(fill=tk.X, pady=2)
        tk.Label(original_size_row, text="미번역 글씨 크기").pack(side=tk.LEFT)
        tk.Spinbox(original_size_row, from_=8, to=96, width=6, textvariable=self.script_original_size_var).pack(side=tk.RIGHT)

        japanese_fonts = _japanese_font_families(self)
        current_japanese = _normalize_font_name(self.script_original_font_var.get())
        if japanese_fonts:
            if current_japanese not in japanese_fonts:
                self.script_original_font_var.set("Yu Gothic UI" if "Yu Gothic UI" in japanese_fonts else japanese_fonts[0])
        else:
            japanese_fonts = [current_japanese]
        original_font_row = tk.Frame(script_frame)
        original_font_row.pack(fill=tk.X, pady=2)
        tk.Label(original_font_row, text="미번역 글씨 폰트").pack(side=tk.LEFT)
        original_font_menu = tk.OptionMenu(original_font_row, self.script_original_font_var, *japanese_fonts)
        original_font_menu.config(width=20)
        original_font_menu.pack(side=tk.RIGHT)

        color_button("번역 글씨 색깔", self.script_translated_color_var)
        translated_size_row = tk.Frame(script_frame)
        translated_size_row.pack(fill=tk.X, pady=2)
        tk.Label(translated_size_row, text="번역 글씨 크기").pack(side=tk.LEFT)
        tk.Spinbox(translated_size_row, from_=8, to=96, width=6, textvariable=self.script_translated_size_var).pack(side=tk.RIGHT)

        korean_fonts = _korean_font_families(self) or [_normalize_font_name(self.script_translated_font_var.get())]
        current_korean = _normalize_font_name(self.script_translated_font_var.get())
        if current_korean not in korean_fonts:
            self.script_translated_font_var.set(
                "Malgun Gothic" if "Malgun Gothic" in korean_fonts else (
                    "맑은 고딕" if "맑은 고딕" in korean_fonts else korean_fonts[0]
                )
            )
        translated_font_row = tk.Frame(script_frame)
        translated_font_row.pack(fill=tk.X, pady=2)
        tk.Label(translated_font_row, text="번역 글씨 폰트").pack(side=tk.LEFT)
        translated_font_menu = tk.OptionMenu(translated_font_row, self.script_translated_font_var, *korean_fonts)
        translated_font_menu.config(width=20)
        translated_font_menu.pack(side=tk.RIGHT)

        button_row = tk.Frame(body, bg=BG)
        button_row.pack(fill=tk.X, pady=(14, 0))
        save_button = tk.Button(button_row, text="저장", command=self._on_save)
        save_button.pack(side=tk.RIGHT, padx=(8, 0))
        close_button = tk.Button(button_row, text="닫기", command=self._on_close)
        close_button.pack(side=tk.RIGHT)
        style_tree(self)
        style_standard_button(save_button, "primary")
        style_standard_button(close_button)
        self._refresh_color_buttons()

    def _choose_color(self, variable: tk.StringVar, button: tk.Button) -> None:
        color = colorchooser.askcolor(color=variable.get(), parent=self)[1]
        if not color:
            return
        variable.set(color)
        self._refresh_color_buttons()

    def _refresh_color_buttons(self) -> None:
        for button, variable in self._color_buttons:
            color = variable.get()
            style_color_button(button, color)

    def _mode_action_label(self) -> str:
        return "번역 후 표시" if self.overlay_mode_var.get() else "인식 후 복사"

    def _update_mode_labels(self) -> None:
        action_label = self._mode_action_label()
        for widget, suffix in self.detached_label_controls:
            widget.configure(text=f"분리된 '{action_label}' {suffix}")

    def _on_overlay_mode_changed(self) -> None:
        self._update_mode_labels()
        self._update_overlay_controls()

    def _update_overlay_controls(self) -> None:
        state = tk.NORMAL if self.overlay_mode_var.get() else tk.DISABLED
        for widget in self.overlay_only_widgets:
            try:
                widget.configure(state=state)
            except tk.TclError:
                for child in widget.winfo_children():
                    try:
                        child.configure(state=state)
                    except tk.TclError:
                        pass

    def _read_port(self, variable: tk.IntVar, label: str) -> int:
        try:
            return _normalize_port(variable.get(), 0)
        except (tk.TclError, ValueError):
            raise ValueError(f"{label} 포트 번호는 1부터 65535 사이의 숫자여야 합니다.")

    def _read_int_range(
        self,
        variable: tk.IntVar,
        label: str,
        minimum: int,
        maximum: int,
    ) -> int:
        try:
            value = int(variable.get())
        except (tk.TclError, ValueError):
            raise ValueError(f"{label} 값은 {minimum}부터 {maximum} 사이의 숫자여야 합니다.")
        return max(minimum, min(maximum, value))

    def _read_color(self, variable: tk.StringVar, label: str, fallback: str) -> str:
        color = _normalize_hex_color(variable.get(), "")
        if not color:
            raise ValueError(f"{label} 색상은 #RRGGBB 형식이어야 합니다.")
        return color

    def _collect_settings(self) -> AppSettings:
        current = self.owner.settings
        detached_geometry = load_detached_geometry(current.detached_geometry)
        hytrans_port = self._read_port(self.hytrans_port_var, "HYTrans")
        overlayer_port = self._read_port(self.overlayer_port_var, "MekiOverlayer")
        audio_capture_port = self._read_port(
            self.audio_capture_port_var, "MekiAudioCapture"
        )
        script_port = self._read_port(self.script_port_var, "MekiScript")
        validate_unique_ports(
            {
                "HYTrans": hytrans_port,
                "MekiOverlayer": overlayer_port,
                "MekiAudioCapture": audio_capture_port,
                "MekiScript": script_port,
            }
        )
        opacity = self._read_int_range(self.overlayer_opacity_var, "MekiOverlayer 배경 투명도", 10, 100) / 100.0
        text_size = self._read_int_range(self.overlayer_text_size_var, "MekiOverlayer 글씨 크기", 8, 96)
        script_opacity = self._read_int_range(self.script_opacity_var, "MekiScript 배경 투명도", 10, 100) / 100.0
        script_original_size = self._read_int_range(self.script_original_size_var, "미번역 글씨 크기", 8, 96)
        script_translated_size = self._read_int_range(self.script_translated_size_var, "번역 글씨 크기", 8, 96)
        return AppSettings(
            minimize_to_tray=self.minimize_to_tray_var.get(),
            main_always_on_top=self.main_topmost_var.get(),
            detached_always_on_top=self.detached_topmost_var.get(),
            detached_hide_titlebar=self.detached_hide_titlebar_var.get(),
            detached_fixed_size=self.detached_fixed_size_var.get(),
            simple_copy_complete=self.simple_copy_complete_var.get(),
            detached_geometry=detached_geometry,
            detached_fixed_width=current.detached_fixed_width,
            detached_fixed_height=current.detached_fixed_height,
            overlay_translation_mode=self.overlay_mode_var.get(),
            hytrans_port=hytrans_port,
            overlayer_port=overlayer_port,
            audio_capture_port=audio_capture_port,
            script_port=script_port,
            overlayer_always_on_top=self.overlayer_topmost_var.get(),
            overlayer_hide_titlebar=self.overlayer_hide_titlebar_var.get(),
            overlayer_fixed_size=self.overlayer_fixed_size_var.get(),
            overlayer_exclude_from_capture=self.overlayer_exclude_capture_var.get(),
            overlayer_bg_color=self._read_color(
                self.overlayer_bg_color_var,
                "MekiOverlayer 배경",
                current.overlayer_bg_color,
            ),
            overlayer_bg_opacity=opacity,
            overlayer_text_color=self._read_color(
                self.overlayer_text_color_var,
                "MekiOverlayer 글씨",
                current.overlayer_text_color,
            ),
            overlayer_text_size=text_size,
            overlayer_text_font=_normalize_font_name(self.overlayer_text_font_var.get()),
            audio_stt_precision=(
                self.audio_stt_precision_var.get()
                if self.audio_stt_precision_var.get() in {"fp32", "int8"}
                else "fp32"
            ),
            audio_chunk_preset=(
                self.audio_chunk_preset_var.get()
                if self.audio_chunk_preset_var.get() in {"FAST", "BALANCED", "LONG"}
                else "BALANCED"
            ),
            script_always_on_top=self.script_topmost_var.get(),
            script_bg_color=self._read_color(
                self.script_bg_color_var,
                "MekiScript 배경",
                current.script_bg_color,
            ),
            script_bg_opacity=script_opacity,
            script_original_text_color=self._read_color(
                self.script_original_color_var,
                "미번역 글씨",
                current.script_original_text_color,
            ),
            script_original_text_size=script_original_size,
            script_original_text_font=_normalize_font_name(self.script_original_font_var.get()),
            script_translated_text_color=self._read_color(
                self.script_translated_color_var,
                "번역 글씨",
                current.script_translated_text_color,
            ),
            script_translated_text_size=script_translated_size,
            script_translated_text_font=_normalize_font_name(self.script_translated_font_var.get()),
            suppress_magpie_launch_notice=self.suppress_magpie_notice_var.get(),
            debug_logging=self.debug_logging_var.get(),
        )

    def _on_save(self) -> None:
        try:
            settings = self._collect_settings()
        except ValueError as exc:
            messagebox.showerror("MekiCopy", str(exc), parent=self)
            return
        if settings.detached_fixed_size:
            detached_size = _geometry_size(settings.detached_geometry)
            if detached_size:
                width, height = detached_size
                settings.detached_fixed_width = width
                settings.detached_fixed_height = height
        self.owner.apply_settings(settings, persist=True)
        self._on_close()

    def _on_test_connection(self) -> None:
        try:
            settings = self._collect_settings()
        except ValueError as exc:
            messagebox.showerror("MekiCopy", str(exc), parent=self)
            return
        self.owner.apply_settings(settings, persist=True)
        self.owner._on_test_overlay_connection(parent=self)

    def _on_close(self) -> None:
        self.owner.settings_window = None
        self.destroy()
