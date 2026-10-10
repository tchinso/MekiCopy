from __future__ import annotations

from copy import deepcopy
import tkinter as tk
from tkinter import colorchooser, messagebox, ttk

from mekicopy_hotkey import (
    DEFAULT_GLOBAL_HOTKEY,
    hotkey_from_tk_event,
    parse_hotkey,
)
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
from hytrans.model_files import DEFAULT_MODEL_ID
from hytrans.api_settings import (
    BACKEND_LABELS,
    PROVIDERS,
    ApiSettings,
    ProviderSettings,
    load_api_settings,
    normalize_backend,
    validate_provider_settings,
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

HYTRANS_MODEL_LABELS = {
    "mt1.5": "MT1.5 (기본) · Hy-MT1.5 1.8B q4",
}
BACKEND_IDS_BY_LABEL = {label: backend for backend, label in BACKEND_LABELS.items()}
STT_MODEL_LABELS = {
    "parakeet": "Parakeet TDT-CTC 0.6B 일본어 (기본, INT8)",
    "reazonspeech": "ReazonSpeech 일본어 (INT8 / FP32)",
}
STT_MODEL_IDS_BY_LABEL = {
    label: model_id for model_id, label in STT_MODEL_LABELS.items()
}


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


class ApiSettingsDialog(tk.Toplevel):
    """Edit provider drafts; the main settings Save commits them together."""

    def __init__(self, owner: "SettingsWindow") -> None:
        super().__init__(owner)
        self.owner = owner
        self.settings = deepcopy(owner.api_settings)
        backend = BACKEND_IDS_BY_LABEL.get(owner.hytrans_backend_var.get(), "local")
        self.provider = backend if backend in PROVIDERS else PROVIDERS[0]
        self.title("번역 API 설정")
        configure_window_theme(self)
        self.geometry(
            f"{min(660, max(560, self.winfo_screenwidth() - 80))}x"
            f"{min(840, max(480, self.winfo_screenheight() - 100))}"
        )
        self.minsize(560, 480)
        self.transient(owner)
        _set_window_icon(self)
        self.provider_var = tk.StringVar(value=BACKEND_LABELS[self.provider])
        self.api_key_var = tk.StringVar()
        self.account_id_var = tk.StringVar()
        self.model_var = tk.StringVar()
        self.show_key_var = tk.BooleanVar(value=False)

        body = tk.Frame(self, bg=BG, padx=14, pady=14)
        body.pack(fill=tk.BOTH, expand=True)
        provider_row = tk.Frame(body, bg=BG)
        provider_row.pack(fill=tk.X, pady=(0, 8))
        tk.Label(provider_row, text="API 서비스").pack(side=tk.LEFT)
        tk.OptionMenu(
            provider_row,
            self.provider_var,
            *(BACKEND_LABELS[provider] for provider in PROVIDERS),
            command=self._on_provider_changed,
        ).pack(side=tk.RIGHT)

        wrapper = _ScrollableTab(body)
        wrapper.pack(fill=tk.BOTH, expand=True)
        fields = wrapper.interior
        key_frame = tk.LabelFrame(fields, text="연결 정보", padx=10, pady=8)
        key_frame.pack(fill=tk.X)
        tk.Label(key_frame, text="API 키", anchor="w").pack(fill=tk.X)
        self.key_entry = tk.Entry(key_frame, textvariable=self.api_key_var, show="•")
        self.key_entry.pack(fill=tk.X, pady=(4, 2))
        tk.Checkbutton(
            key_frame,
            text="API 키 표시",
            variable=self.show_key_var,
            command=self._update_key_visibility,
            anchor="w",
        ).pack(fill=tk.X)
        self.account_row = tk.Frame(key_frame, bg=BG)
        tk.Label(self.account_row, text="Cloudflare Account ID", anchor="w").pack(fill=tk.X)
        tk.Entry(self.account_row, textvariable=self.account_id_var).pack(fill=tk.X, pady=(4, 0))

        model_frame = tk.LabelFrame(fields, text="모델", padx=10, pady=8)
        model_frame.pack(fill=tk.X, pady=(10, 0))
        tk.Label(model_frame, text="사용할 모델 ID (직접 입력 가능)", anchor="w").pack(fill=tk.X)
        self.model_combo = ttk.Combobox(model_frame, textvariable=self.model_var, state="normal")
        self.model_combo.pack(fill=tk.X, pady=(4, 8))
        tk.Label(model_frame, text="모델 목록 (한 줄에 모델 ID 하나)", anchor="w").pack(fill=tk.X)
        self.models_text = self._text_field(model_frame, height=4)
        self.models_text.bind("<<Modified>>", self._on_model_list_changed)

        prompt_frame = tk.LabelFrame(fields, text="번역 프롬프트", padx=10, pady=8)
        prompt_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        tk.Label(
            prompt_frame,
            text="{source}: 원문 언어 · {target}: 번역 언어 · {text}: 원문\n"
            "프롬프트와 모델 목록은 서비스별로 따로 보관됩니다.",
            justify=tk.LEFT,
            anchor="w",
            wraplength=520,
        ).pack(fill=tk.X, pady=(0, 6))
        self.prompt_text = self._text_field(prompt_frame, height=8)

        tk.Label(
            body,
            text="적용한 뒤 MekiCopy 설정의 '저장'을 누르면 반영됩니다.",
            anchor="w",
        ).pack(fill=tk.X, pady=(10, 4))
        buttons = tk.Frame(body, bg=BG)
        buttons.pack(fill=tk.X)
        apply_button = tk.Button(buttons, text="적용", command=self._on_apply)
        apply_button.pack(side=tk.RIGHT, padx=(8, 0))
        tk.Button(buttons, text="취소", command=self._on_close).pack(side=tk.RIGHT)
        style_tree(self)
        style_standard_button(apply_button, "primary")
        self._load_provider()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    @staticmethod
    def _text_field(master: tk.Misc, *, height: int) -> tk.Text:
        row = tk.Frame(master, bg=BG)
        row.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        field = tk.Text(
            row,
            height=height,
            width=1,
            wrap=tk.WORD,
            undo=True,
            bg=SURFACE,
            fg=INK,
            insertbackground=ROSE,
            selectbackground=SOFT,
            highlightbackground=BORDER,
            highlightcolor=ROSE,
            relief=tk.FLAT,
        )
        scrollbar = ttk.Scrollbar(row, command=field.yview)
        field.configure(yscrollcommand=scrollbar.set)
        field.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        return field

    def _models(self) -> list[str]:
        return list(dict.fromkeys(
            line.strip()
            for line in self.models_text.get("1.0", "end-1c").splitlines()
            if line.strip()
        ))

    def _store_provider(self) -> None:
        self.settings.profiles[self.provider] = ProviderSettings(
            api_key=self.api_key_var.get().strip(),
            account_id=self.account_id_var.get().strip(),
            model=self.model_var.get().strip(),
            models=self._models(),
            prompt=self.prompt_text.get("1.0", "end-1c"),
        )

    def _load_provider(self) -> None:
        profile = self.settings.profiles[self.provider]
        self.api_key_var.set(profile.api_key)
        self.account_id_var.set(profile.account_id)
        self.model_var.set(profile.model)
        self.models_text.delete("1.0", tk.END)
        self.models_text.insert("1.0", "\n".join(profile.models))
        self.prompt_text.delete("1.0", tk.END)
        self.prompt_text.insert("1.0", profile.prompt)
        self.models_text.edit_reset()
        self.prompt_text.edit_reset()
        self.model_combo.configure(values=profile.models)
        self.show_key_var.set(False)
        self._update_key_visibility()
        if self.provider == "cloudflare":
            self.account_row.pack(fill=tk.X, pady=(8, 0))
        else:
            self.account_row.pack_forget()

    def _on_provider_changed(self, label: str) -> None:
        self._store_provider()
        self.provider = BACKEND_IDS_BY_LABEL[label]
        self._load_provider()

    def _on_model_list_changed(self, _event: tk.Event) -> None:
        if self.models_text.edit_modified():
            self.model_combo.configure(values=self._models())
            self.models_text.edit_modified(False)

    def _update_key_visibility(self) -> None:
        self.key_entry.configure(show="" if self.show_key_var.get() else "•")

    def _on_apply(self) -> None:
        self._store_provider()
        self.owner.api_settings = deepcopy(self.settings)
        self.owner._update_translation_controls()
        self._on_close()

    def _on_close(self) -> None:
        self.owner.api_settings_dialog = None
        self.destroy()


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
        self.global_hotkey_enabled_var = tk.BooleanVar(
            value=settings.global_hotkey_enabled
        )
        self.global_hotkey_var = tk.StringVar(value=settings.global_hotkey)
        self.overlay_mode_var = tk.BooleanVar(value=settings.overlay_translation_mode)
        self.api_settings: ApiSettings = load_api_settings()
        self.api_settings_dialog: ApiSettingsDialog | None = None
        self.hytrans_backend_var = tk.StringVar(
            value=BACKEND_LABELS[normalize_backend(settings.hytrans_backend)]
        )
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
        selected_stt_model = (
            settings.audio_stt_model
            if settings.audio_stt_model in STT_MODEL_LABELS
            else "parakeet"
        )
        self.audio_stt_model_var = tk.StringVar(
            value=STT_MODEL_LABELS[selected_stt_model]
        )
        self.audio_stt_precision_var = tk.StringVar(
            value=("int8" if selected_stt_model == "parakeet" else settings.audio_stt_precision)
        )
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
        self._global_hotkey_controls: list[tk.Widget] = []

        self._build_ui()
        self.audio_stt_model_var.trace_add("write", self._on_audio_stt_model_changed)
        self._on_audio_stt_model_changed()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.transient(owner)
        self.attributes("-topmost", settings.main_always_on_top)
        self.overlay_mode_var.trace_add("write", lambda *_: self._on_overlay_mode_changed())
        self.global_hotkey_enabled_var.trace_add(
            "write", lambda *_: self._update_global_hotkey_controls()
        )
        self._update_mode_labels()
        self._update_overlay_controls()
        self._update_global_hotkey_controls()
        self.hytrans_backend_var.trace_add("write", lambda *_: self._update_translation_controls())
        self._update_translation_controls()

    def _on_audio_stt_model_changed(self, *_args) -> None:
        is_parakeet = (
            STT_MODEL_IDS_BY_LABEL.get(self.audio_stt_model_var.get()) == "parakeet"
        )
        if is_parakeet:
            # The packaged Parakeet checkpoint only has model.int8.onnx. Keep
            # the persisted selection truthful rather than silently accepting
            # a stale FP32 value left over from the ReazonSpeech-only UI.
            self.audio_stt_precision_var.set("int8")
        precision_menu = getattr(self, "audio_stt_precision_menu", None)
        if precision_menu is not None:
            precision_menu.config(state=tk.DISABLED if is_parakeet else tk.NORMAL)

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
        translation_tab = self._add_scrollable_tab(notebook, "번역")
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

        global_hotkey_frame = tk.LabelFrame(
            general_tab,
            text="전역 단축키",
            padx=10,
            pady=8,
        )
        global_hotkey_frame.pack(fill=tk.X, pady=(8, 4))
        tk.Checkbutton(
            global_hotkey_frame,
            text="전역 단축키 사용",
            variable=self.global_hotkey_enabled_var,
            anchor="w",
        ).pack(fill=tk.X, pady=(0, 4))
        self.global_hotkey_action_label = tk.Label(
            global_hotkey_frame,
            anchor="w",
        )
        self.global_hotkey_action_label.pack(fill=tk.X, pady=(0, 4))
        global_hotkey_row = tk.Frame(global_hotkey_frame)
        global_hotkey_row.pack(fill=tk.X)
        tk.Label(global_hotkey_row, text="실행 키").pack(side=tk.LEFT)
        self.global_hotkey_entry = tk.Entry(
            global_hotkey_row,
            width=18,
            textvariable=self.global_hotkey_var,
        )
        self.global_hotkey_entry.pack(side=tk.RIGHT, padx=(8, 0))
        self.global_hotkey_entry.bind(
            "<KeyPress>",
            self._capture_global_hotkey,
            add="+",
        )
        self.global_hotkey_entry.bind(
            "<FocusIn>",
            self._on_global_hotkey_focus_in,
            add="+",
        )
        self.global_hotkey_entry.bind(
            "<FocusOut>",
            self._on_global_hotkey_focus_out,
            add="+",
        )
        self.global_hotkey_default_button = tk.Button(
            global_hotkey_frame,
            text="기본값 B",
            command=self._restore_default_global_hotkey,
        )
        self.global_hotkey_default_button.pack(anchor="e", pady=(4, 2))
        tk.Label(
            global_hotkey_frame,
            text="입력칸을 클릭한 뒤 키를 누르세요. B 같은 일반 키도 단독으로 사용할 수 있습니다.",
            anchor="w",
            justify=tk.LEFT,
        ).pack(fill=tk.X)
        self._global_hotkey_controls.extend(
            [self.global_hotkey_entry, self.global_hotkey_default_button]
        )

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

        translation_frame = tk.LabelFrame(translation_tab, text="HYTrans 번역", padx=10, pady=8)
        translation_frame.pack(fill=tk.X)
        tk.Label(
            translation_frame,
            text="번역 오버레이와 음성인식 등 HYTrans를 사용하는 모든 번역에 적용됩니다.",
            justify=tk.LEFT,
            anchor="w",
            wraplength=460,
        ).pack(fill=tk.X, pady=(0, 8))
        provider_row = tk.Frame(translation_frame)
        provider_row.pack(fill=tk.X, pady=3)
        tk.Label(provider_row, text="번역 서비스").pack(side=tk.LEFT)
        provider_menu = tk.OptionMenu(
            provider_row, self.hytrans_backend_var, *BACKEND_LABELS.values()
        )
        provider_menu.configure(width=28, anchor="e")
        provider_menu.pack(side=tk.RIGHT)
        self.translation_model_label = tk.Label(
            translation_frame, anchor="w", justify=tk.LEFT, wraplength=460
        )
        self.translation_model_label.pack(fill=tk.X, pady=(4, 8))
        tk.Button(
            translation_frame,
            text="API 키 · 모델 목록 · 번역 프롬프트 설정",
            command=self._open_api_settings,
        ).pack(fill=tk.X, pady=3)
        port_row = tk.Frame(translation_frame)
        port_row.pack(fill=tk.X, pady=3)
        tk.Label(port_row, text="HYTrans 포트").pack(side=tk.LEFT)
        tk.Spinbox(
            port_row,
            from_=1,
            to=65535,
            width=8,
            textvariable=self.hytrans_port_var,
        ).pack(side=tk.RIGHT)

        overlay_frame = tk.LabelFrame(overlay_tab, text="번역 오버레이 모드", padx=10, pady=8)
        overlay_frame.pack(fill=tk.BOTH, expand=True)

        overlay_checkbox = tk.Checkbutton(
            overlay_frame,
            text="오버레이어 번역 모드 사용",
            variable=self.overlay_mode_var,
            anchor="w",
        )
        overlay_checkbox.pack(fill=tk.X, pady=3)

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
        model_row = tk.Frame(audio_model_frame)
        model_row.pack(fill=tk.X, pady=3)
        tk.Label(model_row, text="STT 모델").pack(side=tk.LEFT)
        tk.OptionMenu(
            model_row,
            self.audio_stt_model_var,
            *STT_MODEL_IDS_BY_LABEL,
        ).pack(side=tk.RIGHT)
        precision_row = tk.Frame(audio_model_frame)
        precision_row.pack(fill=tk.X, pady=3)
        tk.Label(precision_row, text="ReazonSpeech 정밀도").pack(side=tk.LEFT)
        self.audio_stt_precision_menu = tk.OptionMenu(
            precision_row,
            self.audio_stt_precision_var,
            "fp32",
            "int8",
        )
        self.audio_stt_precision_menu.pack(side=tk.RIGHT)
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

    def _open_api_settings(self) -> None:
        if self.api_settings_dialog is not None and self.api_settings_dialog.winfo_exists():
            self.api_settings_dialog.lift()
            self.api_settings_dialog.focus_set()
            return
        self.api_settings_dialog = ApiSettingsDialog(self)

    def _update_translation_controls(self) -> None:
        backend = BACKEND_IDS_BY_LABEL.get(self.hytrans_backend_var.get(), "local")
        if backend == "local":
            text = "로컬 모델: " + HYTRANS_MODEL_LABELS[DEFAULT_MODEL_ID]
        elif backend == "translator_api":
            text = "브라우저 내장 Translator API · 일본어 → 한국어 (첫 사용 시 모델 다운로드)"
        else:
            profile = self.api_settings.profiles[backend]
            text = f"API 모델: {profile.model or '(모델 ID를 입력하세요)'}"
        self.translation_model_label.configure(text=text)

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
        global_hotkey_label = getattr(self, "global_hotkey_action_label", None)
        if global_hotkey_label is not None:
            global_hotkey_label.configure(
                text=f"전역 단축키를 누르면 현재 모드의 '{action_label}' 실행"
            )

    def _update_global_hotkey_controls(self) -> None:
        state = tk.NORMAL if self.global_hotkey_enabled_var.get() else tk.DISABLED
        for widget in self._global_hotkey_controls:
            widget.configure(state=state)

    def _capture_global_hotkey(self, event: tk.Event) -> str:
        hotkey = hotkey_from_tk_event(event.keysym, event.state)
        if hotkey is not None:
            self.global_hotkey_var.set(hotkey)
        # This is a dedicated capture field. Do not let a keypress append raw
        # text after it has been converted to the canonical shortcut.
        return "break"

    def _restore_default_global_hotkey(self) -> None:
        self.global_hotkey_var.set(DEFAULT_GLOBAL_HOTKEY)

    def _on_global_hotkey_focus_in(self, _event: tk.Event) -> None:
        pause = getattr(self.owner, "pause_global_hotkey", None)
        if callable(pause):
            pause()

    def _on_global_hotkey_focus_out(self, _event: tk.Event) -> None:
        resume = getattr(self.owner, "resume_global_hotkey", None)
        if callable(resume):
            resume()

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
        backend = BACKEND_IDS_BY_LABEL.get(self.hytrans_backend_var.get(), "local")
        if backend in PROVIDERS:
            error = validate_provider_settings(self.api_settings.profiles[backend], backend)
            if error:
                raise ValueError(f"{BACKEND_LABELS[backend]} API 설정을 확인하세요.\n{error}")
        detached_geometry = load_detached_geometry(current.detached_geometry)
        global_hotkey = parse_hotkey(self.global_hotkey_var.get())
        if global_hotkey is None:
            raise ValueError(
                "전역 단축키는 B 또는 Ctrl+Alt+B처럼 키 하나를 포함해 입력하세요."
            )
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
            global_hotkey_enabled=self.global_hotkey_enabled_var.get(),
            global_hotkey=global_hotkey.text,
            detached_geometry=detached_geometry,
            detached_fixed_width=current.detached_fixed_width,
            detached_fixed_height=current.detached_fixed_height,
            overlay_translation_mode=self.overlay_mode_var.get(),
            hytrans_backend=backend,
            hytrans_model_id=DEFAULT_MODEL_ID,
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
            audio_stt_model=STT_MODEL_IDS_BY_LABEL.get(
                self.audio_stt_model_var.get(),
                "parakeet",
            ),
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
        if self.api_settings_dialog is not None and self.api_settings_dialog.winfo_exists():
            self.api_settings_dialog._on_apply()
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
        result = self.owner.apply_settings(settings, persist=True, api_settings=self.api_settings)
        if result is None:
            return
        self._on_close()

    def _on_test_connection(self) -> None:
        if self.api_settings_dialog is not None and self.api_settings_dialog.winfo_exists():
            self.api_settings_dialog._on_apply()
        try:
            settings = self._collect_settings()
        except ValueError as exc:
            messagebox.showerror("MekiCopy", str(exc), parent=self)
            return
        result = self.owner.apply_settings(settings, persist=True, api_settings=self.api_settings)
        if result is None:
            return
        if result:
            messagebox.showinfo(
                "MekiCopy",
                "HYTrans를 재시작하고 있습니다. 번역 서비스 준비가 끝난 뒤 연결 상태를 확인해 주세요.",
                parent=self,
            )
            return
        self.owner._on_test_overlay_connection(parent=self)

    def _on_close(self) -> None:
        self._on_global_hotkey_focus_out(None)
        self.owner.settings_window = None
        self.destroy()
