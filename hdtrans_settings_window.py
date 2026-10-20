"""Tk controls for the native hdTrans bundle and its official TSV dictionaries."""
from __future__ import annotations

import os
import json
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog
import uuid
import urllib.error

from mekicopy_companions import _json_request
from mekicopy_tasks import TkTaskRunner
from mekicopy_theme import configure_window_theme, style_tree


def _native_request(*args, **kwargs) -> dict:
    """Keep HTTP rejections distinct from an absent/offline server."""
    try:
        return _json_request(*args, **kwargs)
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            detail = payload.get("detail", payload.get("error", str(exc)))
        except (OSError, ValueError, UnicodeError, AttributeError):
            detail = str(exc)
        raise RuntimeError(f"HYTrans HTTP {exc.code}: {detail}") from exc


class HdTransSettingsWindow(tk.Toplevel):
    def __init__(self, owner: tk.Misc, *, main_window=None) -> None:
        super().__init__(owner)
        from hytrans.hdtrans_settings import load_hdtrans_settings

        self.main_window = main_window or owner
        self.title("hdTrans 설정")
        self.geometry("720x720")
        configure_window_theme(self)
        self.tasks = TkTaskRunner(self)
        self.messages = queue.Queue()
        self.saved = load_hdtrans_settings()
        self.paths = list(self.saved.user_dictionaries)
        self.preserve_var = tk.BooleanVar(value=self.saved.preserve_context)
        self.status_var = tk.StringVar(value="설치 상태 확인 중…")
        self.error_var = tk.StringVar(value="")
        self._selected_path: str | None = None
        self._closed = False
        self._closed_event = threading.Event()
        self._poll_ticks = 0
        self.protocol("WM_DELETE_WINDOW", self._close)
        body = tk.Frame(self, padx=12, pady=12)
        body.pack(fill=tk.BOTH, expand=True)
        tk.Label(body, textvariable=self.status_var, justify=tk.LEFT,
                 anchor="w", wraplength=680).pack(fill=tk.X)
        tk.Label(body, textvariable=self.error_var, justify=tk.LEFT,
                 anchor="w", wraplength=680).pack(fill=tk.X)
        controls = tk.Frame(body)
        controls.pack(fill=tk.X, pady=6)
        for text, command in (
            ("설치 / 재시도", self._install), ("취소", self._cancel_install),
            ("지원 버전 재설치", self._repair),
            ("업데이트 확인", self._check_updates), ("HYTrans 재시작", self._restart),
        ):
            tk.Button(controls, text=text, command=command).pack(side=tk.LEFT, padx=2)
        context = tk.LabelFrame(body, text="문맥", padx=8, pady=8)
        context.pack(fill=tk.X, pady=6)
        tk.Checkbutton(context, text="이전 대화 문맥 보존", variable=self.preserve_var).pack(side=tk.LEFT)
        tk.Button(context, text="문맥 초기화", command=self._reset).pack(side=tk.RIGHT)
        tk.Label(body, justify=tk.LEFT, anchor="w", wraplength=680, text=(
            "hdTrans는 프로세스당 문맥 하나를 사용합니다. OCR·음성·자막 작업이 바뀌면 문맥을 초기화합니다. "
            "다른 게임으로 바꿀 때는 문맥 초기화를 누르세요. 문맥은 디스크에 저장하지 않습니다.\n"
            "사용자 사전 재적용은 문맥을 초기화합니다. 외부 편집 감지에 따른 재로드도 문맥을 초기화할 수 있습니다."
        )).pack(fill=tk.X, pady=4)
        dictionaries = tk.LabelFrame(body, text="사용자 사전 (UTF-8 · 일본어<TAB>한국어 · 선택적 공식 품사 필드)", padx=8, pady=8)
        dictionaries.pack(fill=tk.BOTH, expand=True, pady=6)
        self.listbox = tk.Listbox(dictionaries, height=4, exportselection=False)
        self.listbox.pack(fill=tk.X)
        self.listbox.bind("<<ListboxSelect>>", self._select)
        actions = tk.Frame(dictionaries)
        actions.pack(fill=tk.X, pady=4)
        for text, command in (("파일 추가", self._add), ("새 사전", self._new),
                              ("목록에서 제거", self._remove), ("편집 저장", self._save_editor)):
            tk.Button(actions, text=text, command=command).pack(side=tk.LEFT, padx=2)
        self.editor = tk.Text(dictionaries, wrap="none", height=12, undo=True)
        self.editor.pack(fill=tk.BOTH, expand=True)
        tk.Button(body, text="설정 저장 및 사전 재적용", command=self._apply).pack(fill=tk.X, pady=6)
        self._refresh_paths()
        style_tree(self)
        self._poll_id = self.after(250, self._poll)
        self._run("status", self._status_operation())

    def _url(self) -> str:
        return self.main_window._hytrans_base_url()

    def _run(self, key, operation, callback=None) -> None:
        if key != "status":
            self.error_var.set("")
        self.tasks.submit(key, operation, on_success=callback or self._show_result,
                          on_error=lambda exc: self.error_var.set(str(exc)))

    def _show_result(self, result) -> None:
        if isinstance(result, dict):
            errors = []
            if result.get("error"):
                errors.append("엔진 오류: " + str(result["error"]))
            if result.get("userDictionaryError"):
                errors.append("사용자 사전 진단: " + str(result["userDictionaryError"]))
            if errors:
                self.error_var.set("\n".join(errors))
            if result.get("latestVersion"):
                self.status_var.set(
                    f"설치 버전 {result.get('installedVersion') or '없음'} · "
                    f"지원 버전 {result.get('supportedVersion')} · 공식 최신 {result['latestVersion']}\n"
                    f"{result.get('message') or ''}"
                )
                return
            installed_version = result.get("version") or result.get("engineVersion")
            phase = result.get("phase") or result.get("state") or result.get("message")
            if not phase:
                phase = "설치된 번들을 확인했습니다. 엔진 준비 상태는 HYTrans 시작 후 확인됩니다." if result.get("installed") else "hdTrans 설치가 필요합니다. 설치 / 재시도를 누르세요."
            if str(result.get("state", "")).upper() == "INACTIVE":
                phase = "HYTrans의 현재 엔진이 hdTrans가 아닙니다. 메인 설정을 저장하여 hdTrans를 시작하세요."
            self.status_var.set(
                f"엔진 {installed_version or '미준비 (지원 v0.2.1)'} · "
                f"사전 {result.get('dictionaryVersion') or '미준비 (지원 0.2.1+c9ab89c732ad)'}\n{phase}"
            )
        else:
            self.status_var.set(str(result))

    def _status_operation(self):
        from hytrans.hdtrans_download import installation_status
        url = self._url()
        def status():
            try:
                return {**installation_status(), **_native_request(f"{url}/hdtrans/status", timeout=2)}
            except OSError:
                return installation_status()
        return status

    def _poll(self) -> None:
        if self._closed:
            return
        try:
            while True:
                self._show_result(self.messages.get_nowait())
        except queue.Empty:
            pass
        self._poll_ticks += 1
        if self._poll_ticks % 4 == 0 and not self.tasks.is_running("install"):
            self._run("status", self._status_operation())
        self._poll_id = self.after(250, self._poll)

    def _install(self) -> None:
        url, messages, closed = self._url(), self.messages, self._closed_event
        def install():
            from hytrans.hdtrans_download import get_download_manager
            manager = get_download_manager()
            # HYTrans owns downloads during initialization. If it is running,
            # use its cancellation and status paths, otherwise use the same
            # install manager locally before starting the server.
            try:
                _native_request(f"{url}/hdtrans/install", {}, method="POST", timeout=3)
                import time
                while not closed.is_set():
                    status = _native_request(f"{url}/hdtrans/status", timeout=3)
                    messages.put(status)
                    if status.get("ready") or status.get("error") or str(status.get("state", "")).upper() in {"CANCELLED", "STOPPED", "INACTIVE"}:
                        return status
                    time.sleep(0.3)
                return "창을 닫았습니다. 설치 상태는 다음 열기에서 확인할 수 있습니다."
            except OSError:
                manager.start()
                import time
                while not closed.is_set():
                    status = manager.status()
                    messages.put(status)
                    if not status.get("busy", False):
                        return status
                    time.sleep(0.3)
                return manager.status()
        def installed(status):
            self._show_result(status)
            if isinstance(status, dict) and status.get("state") == "ready" and not status.get("error"):
                self._restart()
        self._run("install", install, installed)

    def _cancel_install(self) -> None:
        url = self._url()
        def cancel():
            from hytrans.hdtrans_download import get_download_manager
            get_download_manager().cancel()
            try:
                return _native_request(f"{url}/hdtrans/download/cancel", {}, method="POST", timeout=3)
            except OSError:
                return "다운로드 취소를 요청했습니다. 설치된 정상 버전은 유지됩니다."
        self._run("cancel", cancel)

    def _check_updates(self) -> None:
        from hytrans.hdtrans_download import check_for_updates
        self._run("updates", check_for_updates)

    def _restart(self) -> None:
        restarted = self.main_window._begin_hytrans_restart(
            self.main_window.settings.hytrans_port, "hdtrans-settings"
        )
        if not restarted:
            self.main_window._launch_hytrans(notify=False)

    def _repair(self) -> None:
        messages, closed = self.messages, self._closed_event
        def repair():
            from hytrans.hdtrans_download import get_download_manager
            import time
            manager = get_download_manager()
            manager.start(force=True)
            while not closed.is_set():
                status = manager.status()
                messages.put(status)
                if not status.get("busy"):
                    return status
                time.sleep(0.3)
            return manager.status()
        def repaired(status):
            self._show_result(status)
            if status.get("state") == "ready" and not status.get("error"):
                self._restart()
        self._run("install", repair, repaired)

    def _reset(self) -> None:
        url = self._url()
        self._run("reset", lambda: _native_request(
            f"{url}/hdtrans/context/reset", {}, method="POST", timeout=650))

    def _refresh_paths(self) -> None:
        self.listbox.delete(0, tk.END)
        for path in self.paths:
            self.listbox.insert(tk.END, path)

    def _select(self, _event=None) -> None:
        indexes = self.listbox.curselection()
        if not indexes:
            return
        path = self.paths[indexes[0]]
        try:
            text = Path(path).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            self.error_var.set(f"사용자 사전 읽기 오류: {exc}")
            return
        self._selected_path = path
        self.editor.delete("1.0", tk.END)
        self.editor.insert("1.0", text)

    def _add(self) -> None:
        for path in filedialog.askopenfilenames(parent=self, filetypes=[("사용자 사전", "*.txt *.tsv"), ("모든 파일", "*.*")]):
            if path not in self.paths:
                self.paths.append(path)
        self._refresh_paths()

    def _new(self) -> None:
        from hytrans.hdtrans_download import user_root
        path = user_root() / f"userdict-{uuid.uuid4().hex[:8]}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# 일본어\t한국어\n", encoding="utf-8")
        self.paths.append(str(path))
        self._refresh_paths()
        self.listbox.selection_set(len(self.paths) - 1)
        self._select()

    def _remove(self) -> None:
        for index in reversed(self.listbox.curselection()):
            self.paths.pop(index)
        self._refresh_paths()
        self._selected_path = None
        self.editor.delete("1.0", tk.END)

    def _save_editor(self) -> bool:
        if not self._selected_path:
            return True
        path = Path(self._selected_path)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(self.editor.get("1.0", "end-1c"), encoding="utf-8")
            os.replace(temporary, path)
            self.status_var.set("사용자 사전 저장 완료. 재적용하면 엔진의 파일·줄 번호 진단을 확인할 수 있습니다.")
            return True
        except (OSError, UnicodeError) as exc:
            self.error_var.set(f"사용자 사전 저장 오류: {exc}")
            return False
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _apply(self) -> None:
        if not self._save_editor():
            return
        preserve, paths = self.preserve_var.get(), list(self.paths)
        changed_paths = paths != self.saved.user_dictionaries
        url = self._url()
        def apply():
            from hytrans.hdtrans_settings import HdTransSettings, save_hdtrans_settings
            # The server serializes changes with translation when it is live.
            try:
                applied = _native_request(f"{url}/hdtrans/settings", {
                    "preserveContext": preserve, "userDictionaries": paths,
                }, method="PUT", timeout=650)
                if applied.get("ready") and not changed_paths:
                    return _native_request(f"{url}/hdtrans/reload", {}, method="POST", timeout=650)
                return applied
            except OSError as exc:
                # HTTP failures must stay visible; only an absent server uses
                # the offline settings file for the next initialization.
                saved = save_hdtrans_settings(HdTransSettings(
                    preserve_context=preserve, user_dictionaries=paths))
                if saved is False:
                    raise OSError("hdTrans 설정을 저장하지 못했습니다.")
                return "설정을 저장했습니다. HYTrans 시작 시 적용됩니다."
        def applied(result):
            from hytrans.hdtrans_settings import HdTransSettings
            self.saved = HdTransSettings(preserve_context=preserve, user_dictionaries=paths)
            # Applying a valid dictionary clears previous, stale diagnostics.
            self.error_var.set("")
            self._show_result(result)
        self._run("apply", apply, applied)

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._closed_event.set()
        self.after_cancel(self._poll_id)
        self.tasks.close()
        self.destroy()
        # Explicitly release Tcl variables on their owning thread. A pending
        # network worker may outlive this window but owns only plain snapshots.
        self.preserve_var = self.status_var = self.error_var = None
