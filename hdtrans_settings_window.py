"""hdTrans installation, context, and user dictionary controls."""
from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
import threading
import urllib.error
import uuid

from PySide6.QtCore import QSignalBlocker, QTimer, Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QProgressBar,
    QWidget,
)

from hytrans.hdtrans_download import DICTIONARY_VERSION, VERSION
from hytrans.hdtrans_settings import HdTransSettings, load_hdtrans_settings, save_hdtrans_settings
from mekicopy_companions import _json_request
from mekicopy_qt_tasks import QtTaskRunner
from mekicopy_settings_window import _action, _checkbox, _configure_dialog, _description, _footer, _scroll_content, _section


_INSTALL_STATES = {"STARTING", "LOADING", "DOWNLOADING", "VERIFYING", "EXTRACTING", "INSTALLING"}


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


class HdTransSettingsWindow(QDialog):
    def __init__(self, owner: QWidget, *, main_window=None) -> None:
        super().__init__(owner)
        self.main_window = main_window or owner
        self.setWindowFlag(
            Qt.WindowType.WindowStaysOnTopHint,
            bool(getattr(self.main_window.settings, "main_always_on_top", False)),
        )
        body = _configure_dialog(self, "hdTrans 설정", 660, 680)
        self.tasks = QtTaskRunner(self)
        self.saved = load_hdtrans_settings()
        self.paths = list(self.saved.user_dictionaries)
        self._drafts: dict[str, str] = {}
        self._selected_path: str | None = None
        self._closed_event = threading.Event()
        self._install_updates: deque[dict] = deque(maxlen=1)
        self._install_busy = False
        scroll, content = _scroll_content()
        body.addWidget(scroll, 1)

        installation = _section(content, "엔진 및 기본 사전")
        self.status_text = _description("설치 상태 확인 중…")
        self.status_text.setTextFormat(Qt.TextFormat.PlainText)
        installation.addWidget(self.status_text)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.hide()
        installation.addWidget(self.progress_bar)

        install_row = QHBoxLayout()
        self.install_button = _action("설치 / 재시도", self._install, variant="primary")
        self.cancel_button = _action("설치 취소", self._cancel_install)
        self.cancel_button.setEnabled(False)
        self.repair_button = _action("지원 버전 재설치", self._repair)
        for action in (self.install_button, self.cancel_button, self.repair_button):
            install_row.addWidget(action)
        installation.addLayout(install_row)
        maintenance_row = QHBoxLayout()
        maintenance_row.addWidget(_action("업데이트 확인", self._check_updates))
        maintenance_row.addWidget(_action("HYTrans 재시작", self._restart))
        installation.addLayout(maintenance_row)
        self.notice_text = self._feedback_label(installation)
        self.error_text = self._feedback_label(installation, error=True)
        self.engine_error_text = self._feedback_label(installation, error=True)

        context = _section(content, "번역 문맥")
        self.preserve_context = _checkbox(context, "이전 대화 문맥 보존", self.saved.preserve_context)
        context.addWidget(_description(
            "최근 입력을 번역에 참고합니다. OCR·음성·자막 작업이 바뀌면 문맥이 초기화됩니다. "
            "다른 게임으로 바꿀 때는 문맥 초기화를 누르세요. 문맥은 디스크에 저장되지 않습니다."
        ))
        context.addWidget(_action("문맥 초기화", self._reset))

        dictionaries = _section(content, "사용자 사전")
        dictionaries.addWidget(_description(
            "UTF-8 파일에 일본어, 탭, 한국어 순으로 입력하세요. 공식 품사 필드를 추가할 수 있습니다. "
            "사전을 재적용하면 번역 문맥이 초기화됩니다."
        ))
        self.listbox = QListWidget()
        self.listbox.setAccessibleName("사용자 사전 파일 목록")
        self.listbox.setFixedHeight(74)
        self.listbox.currentRowChanged.connect(self._select)
        dictionaries.addWidget(self.listbox)
        actions = QHBoxLayout()
        add_button = _action("파일 추가", self._add)
        new_button = _action("새 사전", self._new)
        self.remove_button = _action("목록에서 제거", self._remove)
        self.save_editor_button = _action("편집 저장", self._save_editor)
        for action in (add_button, new_button, self.remove_button, self.save_editor_button):
            actions.addWidget(action)
        dictionaries.addLayout(actions)
        self.editor = QPlainTextEdit()
        self.editor.setAccessibleName("사용자 사전 편집기")
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.editor.setMinimumHeight(112)
        self.editor.setPlaceholderText("편집할 사전 파일을 선택하세요.")
        dictionaries.addWidget(self.editor)
        self.dictionary_error_text = self._feedback_label(dictionaries, error=True)
        content.addStretch()

        self.apply_button = _footer(body, self.reject, self._apply, confirm_text="설정 저장 및 사전 재적용")
        self._refresh_paths()

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1500)
        self._status_timer.timeout.connect(self._request_status)
        self._status_timer.start()
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(250)
        self._progress_timer.timeout.connect(self._update_install_progress)
        self._request_status()

    @staticmethod
    def _feedback_label(layout, *, error=False) -> QLabel:
        label = _description("")
        label.setTextFormat(Qt.TextFormat.PlainText)
        if error:
            label.setStyleSheet("color: #9d294a; background: transparent;")
        label.hide()
        layout.addWidget(label)
        return label

    @staticmethod
    def _set_feedback(label: QLabel, message: str) -> None:
        label.setText(message)
        label.setVisible(bool(message))

    def _url(self) -> str:
        return self.main_window._hytrans_base_url()

    def _run(self, key, operation, callback=None, error_callback=None) -> bool:
        if self.tasks.is_running(key):
            return False
        if key != "status":
            self._set_feedback(self.error_text, "")
        if error_callback is None:
            error_callback = lambda exc: self._set_feedback(self.error_text, str(exc))
        return self.tasks.submit(
            key, operation, on_success=callback or self._show_result, on_error=error_callback
        )

    def _show_result(self, result) -> None:
        if isinstance(result, str):
            self._set_feedback(self.notice_text, result)
            return
        if result.get("latestVersion"):
            self._set_feedback(
                self.notice_text,
                f"설치 버전 {result.get('installedVersion') or '없음'} · "
                f"지원 버전 {result.get('supportedVersion')} · 공식 최신 {result['latestVersion']}\n"
                f"{result.get('message') or ''}",
            )
            return
        if "userDictionaryError" in result:
            error = result["userDictionaryError"]
            self._set_feedback(self.dictionary_error_text, f"사용자 사전 진단\n{error}" if error else "")
        if "error" in result:
            error = result["error"]
            self._set_feedback(self.engine_error_text, f"엔진 오류\n{error}" if error else "")
        state = str(result.get("state", "")).upper()
        self.cancel_button.setEnabled(self._install_busy or state in _INSTALL_STATES)
        phase = result.get("phase") or result.get("message") or state
        if state == "INACTIVE":
            phase = "현재 HYTrans 엔진이 hdTrans가 아닙니다. 메인 설정을 저장하여 hdTrans를 시작하세요."
        if not phase:
            phase = ("설치된 번들을 확인했습니다. 엔진 준비 상태는 HYTrans 시작 후 확인됩니다."
                     if result.get("installed") else "hdTrans 설치가 필요합니다. 설치 / 재시도를 누르세요.")
        installed_version = result.get("version") or result.get("engineVersion")
        self.status_text.setText(
            f"엔진 {installed_version or f'미준비 (지원 v{VERSION})'} · "
            f"사전 {result.get('dictionaryVersion') or f'미준비 (지원 {DICTIONARY_VERSION})'}\n{phase}"
        )
        progress = result.get("progress")
        show_progress = isinstance(progress, (int, float)) and 0 <= progress < 1
        self.progress_bar.setVisible(show_progress)
        if show_progress:
            self.progress_bar.setValue(round(progress * 1000))

    def _request_status(self) -> None:
        if self.tasks.is_running("install"):
            return
        url = self._url()

        def status():
            from hytrans.hdtrans_download import installation_status
            try:
                result = _native_request(f"{url}/hdtrans/status", timeout=2)
            except OSError:
                return installation_status()
            if result.get("version") and result.get("dictionaryVersion"):
                return result
            return {**installation_status(), **result}

        self._run("status", status, self._show_status)

    def _show_status(self, result) -> None:
        if not self.tasks.is_running("install"):
            self._show_result(result)

    def _set_install_busy(self, busy: bool) -> None:
        self._install_busy = busy
        self.install_button.setEnabled(not busy)
        self.repair_button.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        if busy:
            self._progress_timer.start()
        else:
            self._progress_timer.stop()
            self._install_updates.clear()
            self.progress_bar.hide()

    def _update_install_progress(self) -> None:
        try:
            result = self._install_updates.pop()
        except IndexError:
            return
        self._show_result(result)

    def _start_install(self, *, repair: bool) -> None:
        if self.tasks.is_running("install"):
            return
        url, progress, closed = self._url(), self._install_updates, self._closed_event

        def install():
            from hytrans.hdtrans_download import get_download_manager

            if not repair:
                try:
                    initial = _native_request(f"{url}/hdtrans/install", {}, method="POST", timeout=3)
                except OSError:
                    pass
                else:
                    if initial.get("ready"):
                        return initial, False
                    while not closed.is_set():
                        status = _native_request(f"{url}/hdtrans/status", timeout=3)
                        progress.append(status)
                        state = str(status.get("state", "")).upper()
                        if status.get("ready") or status.get("error") or state in {"CANCELLED", "STOPPED", "INACTIVE"}:
                            return status, False
                        closed.wait(0.3)
                    return initial, False

            manager = get_download_manager()
            if repair:
                manager.start(force=True)
            else:
                manager.start()
            status = manager.status()
            while not closed.is_set() and status.get("busy"):
                progress.append(status)
                closed.wait(0.3)
                status = manager.status()
            return status, str(status.get("state", "")).lower() == "ready" and not status.get("error")

        def installed(value):
            self._set_install_busy(False)
            status, restart = value
            self._show_result(status)
            if restart:
                self._restart()

        def failed(exc):
            self._set_install_busy(False)
            self._set_feedback(self.error_text, str(exc))

        self._set_feedback(self.notice_text, "")
        self._set_install_busy(True)
        self._run("install", install, installed, failed)

    def _install(self) -> None:
        self._start_install(repair=False)

    def _repair(self) -> None:
        self._start_install(repair=True)

    def _cancel_install(self) -> None:
        url = self._url()

        def cancel():
            from hytrans.hdtrans_download import get_download_manager
            local = get_download_manager()
            if local.status().get("busy"):
                local.cancel()
                return "다운로드 취소를 요청했습니다."
            try:
                result = _native_request(f"{url}/hdtrans/download/cancel", {}, method="POST", timeout=3)
                return result.get("phase", "다운로드 취소를 요청했습니다.")
            except OSError:
                return "다운로드 취소를 요청했습니다."

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

    def _reset(self) -> None:
        url = self._url()

        def reset():
            result = _native_request(f"{url}/hdtrans/context/reset", {}, method="POST", timeout=650)
            return {**result, "phase": "번역 문맥을 초기화했습니다."}

        self._run("reset", reset)

    def _refresh_paths(self, selected: str | None = None) -> None:
        if selected is None:
            selected = self._selected_path
        self._store_draft()
        with QSignalBlocker(self.listbox):
            self.listbox.clear()
            self.listbox.addItems(self.paths)
            self.listbox.setCurrentRow(self.paths.index(selected) if selected in self.paths else -1)
        self._select()

    def _store_draft(self) -> None:
        if self._selected_path is not None and self.editor.document().isModified():
            self._drafts[self._selected_path] = self.editor.toPlainText()

    def _select(self) -> None:
        self._store_draft()
        index = self.listbox.currentRow()
        self._selected_path = None
        self.editor.clear()
        self.editor.setReadOnly(True)
        self.remove_button.setEnabled(index >= 0)
        self.save_editor_button.setEnabled(False)
        if index < 0:
            return
        path = self.paths[index]
        text = self._drafts.get(path)
        if text is None:
            try:
                text = Path(path).read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError) as exc:
                self._set_feedback(self.dictionary_error_text, f"사용자 사전 읽기 오류\n{exc}")
                return
        self.editor.setPlainText(text)
        self.editor.setReadOnly(False)
        self.save_editor_button.setEnabled(True)
        self._selected_path = path

    def _add(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "사용자 사전 파일 추가", "", "사용자 사전 (*.txt *.tsv);;모든 파일 (*)"
        )
        if paths:
            normalized = [str(Path(path).resolve()) for path in paths]
            self.paths = list(dict.fromkeys([*self.paths, *normalized]))
            self._refresh_paths(normalized[0])

    def _new(self) -> None:
        from hytrans.hdtrans_download import user_root
        path = user_root() / f"userdict-{uuid.uuid4().hex[:8]}.txt"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# 일본어\t한국어\n", encoding="utf-8")
        except OSError as exc:
            self._set_feedback(self.dictionary_error_text, f"사용자 사전 생성 오류\n{exc}")
            return
        self.paths.append(str(path))
        self._refresh_paths(str(path))

    def _remove(self) -> None:
        index = self.listbox.currentRow()
        if index < 0:
            return
        path = self.paths.pop(index)
        self._selected_path = None
        self._drafts.pop(path, None)
        self._refresh_paths(self.paths[min(index, len(self.paths) - 1)] if self.paths else None)

    def _save_dictionary(self, filename: str) -> bool:
        path = Path(filename)
        temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(self._drafts[filename], encoding="utf-8")
            os.replace(temporary, path)
        except (OSError, UnicodeError) as exc:
            self._set_feedback(self.dictionary_error_text, f"사용자 사전 저장 오류\n{exc}")
            return False
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        del self._drafts[filename]
        if self._selected_path == filename:
            self.editor.document().setModified(False)
        return True

    def _save_editor(self) -> bool:
        self._store_draft()
        if self._selected_path not in self._drafts:
            return True
        if not self._save_dictionary(self._selected_path):
            return False
        self._set_feedback(self.notice_text, "사용자 사전을 저장했습니다. 재적용하면 엔진 진단을 확인할 수 있습니다.")
        return True

    def _apply(self) -> None:
        if self.tasks.is_running("apply"):
            return
        if len(self.paths) > 100:
            self._set_feedback(self.dictionary_error_text, "사용자 사전은 최대 100개까지 등록할 수 있습니다.")
            return
        self._store_draft()
        if not all(self._save_dictionary(path) for path in tuple(self._drafts)):
            return
        settings = HdTransSettings(self.preserve_context.isChecked(), list(self.paths))
        changed_paths = settings.user_dictionaries != self.saved.user_dictionaries
        url = self._url()

        def apply():
            try:
                applied = _native_request(f"{url}/hdtrans/settings", settings.as_payload(), method="PUT", timeout=650)
                if applied.get("ready") and not changed_paths:
                    return _native_request(f"{url}/hdtrans/reload", {}, method="POST", timeout=650)
                return applied
            except OSError:
                if not save_hdtrans_settings(settings):
                    raise OSError("hdTrans 설정을 저장하지 못했습니다.") from None
                return "설정을 저장했습니다. HYTrans 시작 시 적용됩니다."

        def applied(result):
            self.apply_button.setEnabled(True)
            self.saved = settings
            self._set_feedback(self.dictionary_error_text, "")
            self._show_result(result)
            if isinstance(result, dict):
                self._set_feedback(self.notice_text, "설정을 저장하고 사용자 사전을 재적용했습니다.")

        def failed(exc):
            self.apply_button.setEnabled(True)
            self._set_feedback(self.dictionary_error_text, str(exc))

        self.apply_button.setEnabled(False)
        self._run("apply", apply, applied, failed)

    def done(self, result: int) -> None:
        self._closed_event.set()
        self._status_timer.stop()
        self._progress_timer.stop()
        self.tasks.close()
        if getattr(self.main_window, "hdtrans_settings_window", None) is self:
            self.main_window.hdtrans_settings_window = None
        super().done(result)
