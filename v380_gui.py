from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable

# The module file is v380_event.py: importing "V380_Event" only works on a
# case-insensitive filesystem and breaks as soon as the project is moved.
from v380_event import Config, V380Service

TRUE_VALUES = {"1", "true", "yes", "on"}

# Defaults must match Config in v380_event.py, otherwise the first save writes
# an empty username / disabled modules over the working defaults.
DEFAULTS: dict[str, str] = {
    "V380_CAMERA_ID": "",
    "V380_USERNAME": "admin",
    "V380_PASSWORD": "",
    "MOTION_RTSP_URL": "rtsp://127.0.0.1:8554/live",
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
    "TELEGRAM_HTTPS_PROXY": "",
    "TERABOX_NDUS": "",
    "MOTION_ENABLED": "true",
    "TELEGRAM_ENABLED": "true",
    "TERABOX_EVENTS_ENABLED": "false",
    "CONTINUOUS_ENABLED": "true",
    "TERABOX_CONTINUOUS_ENABLED": "false",
    "PREBUFFER_SECONDS": "5",
    "POSTBUFFER_SECONDS": "20",
    "COOLDOWN_SECONDS": "30",
    "MIN_CHANGED_RATIO": "0.035",
    "CONSECUTIVE_FRAMES": "3",
    "DETECT_FPS": "4",
    "DETECT_WIDTH": "640",
    "DETECT_HEIGHT": "360",
    "CONTINUOUS_SEGMENT_SECONDS": "1800",
    "TERABOX_MAX_RETRIES": "3",
}

# key -> (kind, minimum, maximum)
NUMERIC_FIELDS: dict[str, tuple[type, float, float]] = {
    "PREBUFFER_SECONDS": (int, 0, 120),
    "POSTBUFFER_SECONDS": (int, 1, 600),
    "COOLDOWN_SECONDS": (int, 0, 3600),
    "MIN_CHANGED_RATIO": (float, 0.001, 1.0),
    "CONSECUTIVE_FRAMES": (int, 1, 100),
    "DETECT_FPS": (float, 1.0, 10.0),
    "DETECT_WIDTH": (int, 160, 1920),
    "DETECT_HEIGHT": (int, 90, 1080),
    "CONTINUOUS_SEGMENT_SECONDS": (int, 60, 86400),
    "TERABOX_MAX_RETRIES": (int, 1, 10),
}


def as_bool(value: str) -> bool:
    return value.strip().lower() in TRUE_VALUES


class V380GUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.root_dir = Path(__file__).resolve().parent
        self.title("V380 Event — локальное видеонаблюдение")
        self.geometry("1080x800")
        self.minsize(920, 680)

        self.service: V380Service | None = None
        self.service_thread: threading.Thread | None = None
        self.ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self.vars: dict[str, tk.Variable] = {}
        self.env_values: dict[str, str] = {}
        self.defaults: dict[str, str] = dict(DEFAULTS)
        self.stopping = False
        self.closing = False
        self.status_var = tk.StringVar(value="Готово к работе")

        self._build_style()
        self._build_ui()
        self.load_env()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(100, self._process_ui_queue)
        self.after(0, self.start_service)

    # -------------------------------------------------------------------- ui

    def _build_style(self) -> None:
        style = ttk.Style(self)
        for theme in ("vista", "clam", "default"):
            try:
                style.theme_use(theme)
                break
            except tk.TclError:
                continue
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("Section.TLabelframe.Label", font=("Segoe UI", 10, "bold"))

    def _build_ui(self) -> None:
        header = ttk.Frame(self, padding=(18, 14))
        header.pack(fill="x")
        ttk.Label(header, text="V380 Event", style="Title.TLabel").pack(side="left")
        ttk.Label(header, textvariable=self.status_var).pack(side="right")

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=14, pady=(0, 10))
        settings = ttk.Frame(notebook, padding=14)
        diagnostics = ttk.Frame(notebook, padding=14)
        logs = ttk.Frame(notebook, padding=14)
        notebook.add(settings, text="Настройки")
        notebook.add(diagnostics, text="Проверка системы")
        notebook.add(logs, text="Логи")

        self._settings_tab(settings)
        self._diagnostics_tab(diagnostics)
        self._logs_tab(logs)

        footer = ttk.Frame(self, padding=(14, 0, 14, 14))
        footer.pack(fill="x")
        ttk.Button(
            footer,
            text="Сохранить настройки",
            command=self.save_env,
        ).pack(side="left")
        self.start_button = ttk.Button(
            footer,
            text="Запустить сервис",
            command=self.start_service,
        )
        self.start_button.pack(side="right")
        self.stop_button = ttk.Button(
            footer,
            text="Остановить сервис",
            command=self.stop_service,
            state="disabled",
        )
        self.stop_button.pack(side="right", padx=8)

    def _settings_tab(self, parent: ttk.Frame) -> None:
        top = ttk.Frame(parent)
        top.pack(fill="x")
        left = ttk.LabelFrame(top, text="Камера", padding=10)
        right = ttk.LabelFrame(top, text="Уведомления и архив", padding=10)
        left.pack(side="left", fill="both", expand=True, padx=(0, 7))
        right.pack(side="left", fill="both", expand=True, padx=(7, 0))

        self._field(left, "V380 Camera ID", "V380_CAMERA_ID", 0)
        self._field(left, "Имя пользователя", "V380_USERNAME", 1)
        self._field(left, "Пароль камеры", "V380_PASSWORD", 2, secret=True)
        self._field(left, "RTSP URL", "MOTION_RTSP_URL", 3)

        self._field(right, "Telegram Bot Token", "TELEGRAM_BOT_TOKEN", 0, secret=True)
        self._field(right, "Telegram Chat ID", "TELEGRAM_CHAT_ID", 1)
        self._field(right, "Telegram proxy", "TELEGRAM_HTTPS_PROXY", 2)
        self._field(right, "TeraBox NDUS", "TERABOX_NDUS", 3, secret=True)

        modules = ttk.LabelFrame(parent, text="Модули", padding=10)
        modules.pack(fill="x", pady=14)
        checks = [
            ("Детектор движения", "MOTION_ENABLED"),
            ("Telegram", "TELEGRAM_ENABLED"),
            ("TeraBox события", "TERABOX_EVENTS_ENABLED"),
            ("Постоянная запись", "CONTINUOUS_ENABLED"),
            ("TeraBox архив", "TERABOX_CONTINUOUS_ENABLED"),
        ]
        for column, (label, key) in enumerate(checks):
            self._check(modules, label, key, column)

        recording = ttk.LabelFrame(parent, text="Запись и обнаружение", padding=10)
        recording.pack(fill="x")
        fields = [
            ("До события, секунд", "PREBUFFER_SECONDS"),
            ("После события, секунд", "POSTBUFFER_SECONDS"),
            ("Пауза между событиями", "COOLDOWN_SECONDS"),
            ("Чувствительность", "MIN_CHANGED_RATIO"),
            ("Кадров для срабатывания", "CONSECUTIVE_FRAMES"),
            ("Частота анализа, FPS", "DETECT_FPS"),
            ("Ширина анализа", "DETECT_WIDTH"),
            ("Высота анализа", "DETECT_HEIGHT"),
            ("Сегмент архива, секунд", "CONTINUOUS_SEGMENT_SECONDS"),
            ("Попыток TeraBox", "TERABOX_MAX_RETRIES"),
        ]
        for row, (label, key) in enumerate(fields):
            self._field(recording, label, key, row, width=12)

    def _field(
        self,
        parent: tk.Misc,
        label: str,
        key: str,
        row: int,
        secret: bool = False,
        width: int = 38,
    ) -> None:
        ttk.Label(parent, text=label).grid(
            row=row,
            column=0,
            sticky="w",
            padx=(0, 8),
            pady=4,
        )
        var = tk.StringVar()
        self.vars[key] = var
        ttk.Entry(
            parent,
            textvariable=var,
            width=width,
            show="•" if secret else "",
        ).grid(row=row, column=1, sticky="ew", pady=4)
        parent.columnconfigure(1, weight=1)

    def _check(self, parent: tk.Misc, label: str, key: str, column: int) -> None:
        var = tk.BooleanVar(value=as_bool(DEFAULTS.get(key, "false")))
        self.vars[key] = var
        ttk.Checkbutton(parent, text=label, variable=var).grid(
            row=0,
            column=column,
            sticky="w",
            padx=7,
        )

    def _diagnostics_tab(self, parent: ttk.Frame) -> None:
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill="x")
        ttk.Button(
            toolbar,
            text="Запустить проверку",
            command=self.run_diagnostics,
        ).pack(side="left")
        ttk.Button(
            toolbar,
            text="Открыть папку приложения",
            command=self.open_app_directory,
        ).pack(side="left", padx=8)
        self.diag = tk.Text(parent, state="disabled", font=("Consolas", 10))
        self.diag.pack(fill="both", expand=True, pady=10)

    def _logs_tab(self, parent: ttk.Frame) -> None:
        self.log_text = tk.Text(parent, state="disabled", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)
        ttk.Button(parent, text="Обновить логи", command=self.refresh_logs).pack(
            anchor="e",
            pady=6,
        )

    # ---------------------------------------------------------------- helpers

    def open_app_directory(self) -> None:
        path = str(self.root_dir)
        try:
            if hasattr(os, "startfile"):
                os.startfile(path)  # noqa: S606 - Windows explorer
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except (OSError, AttributeError) as exc:
            messagebox.showerror("V380 Event", f"Не удалось открыть папку: {exc}")

    def load_env(self) -> None:
        path = self.root_dir / ".env.windows"
        if not path.exists():
            path = self.root_dir / ".env.windows.example"

        self.env_values = {}
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8-sig")
            except OSError as exc:
                messagebox.showerror("V380 Event", f"Не удалось прочитать {path}: {exc}")
                text = ""
            for raw in text.splitlines():
                line = raw.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    self.env_values[key.strip()] = value.strip()

        for key, var in self.vars.items():
            default = self.defaults.get(key, "")
            value = self.env_values.get(key, default)
            if not value.strip() and default:
                value = default
            if isinstance(var, tk.BooleanVar):
                var.set(as_bool(value))
            else:
                var.set(value)

    def normalize_numeric(self) -> None:
        """Replace unparsable numbers with the default and clamp the range."""
        for key, (kind, low, high) in NUMERIC_FIELDS.items():
            var = self.vars.get(key)
            if var is None:
                continue
            raw = str(var.get()).strip().replace(",", ".")
            fallback = self.defaults.get(key, "0")
            try:
                number = float(raw) if raw else float(fallback)
            except ValueError:
                number = float(fallback)
            number = min(max(number, low), high)
            var.set(str(int(number)) if kind is int else f"{number:g}")

    def collect_values(self) -> dict[str, str]:
        values = dict(self.env_values)
        for key, var in self.vars.items():
            if isinstance(var, tk.BooleanVar):
                values[key] = "true" if var.get() else "false"
            else:
                values[key] = str(var.get()).strip()
        return values

    def save_env_silent(self) -> None:
        self.normalize_numeric()
        values = self.collect_values()
        lines = ["# Generated by V380 Event GUI", ""]
        lines.extend(f"{key}={value}" for key, value in values.items())
        target = self.root_dir / ".env.windows"
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        temporary.replace(target)
        self.env_values = values

    def save_env(self) -> None:
        try:
            self.save_env_silent()
        except OSError as exc:
            messagebox.showerror("V380 Event", str(exc))
            return
        self.status_var.set("Настройки сохранены")

    def _set_text(self, widget: tk.Text, text: str) -> None:
        try:
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.insert("end", text)
            widget.configure(state="disabled")
        except tk.TclError:
            pass

    def _post_ui(self, callback: Callable[[], None]) -> None:
        self.ui_queue.put(callback)

    def _process_ui_queue(self) -> None:
        while True:
            try:
                callback = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception as exc:  # a broken callback must not kill the loop
                print(f"UI callback failed: {exc}", file=sys.stderr)
        if not self.closing:
            try:
                self.after(100, self._process_ui_queue)
            except tk.TclError:
                pass

    # ------------------------------------------------------------ diagnostics

    def run_diagnostics(self) -> None:
        self._set_text(self.diag, "Проверка выполняется...\n")
        try:
            self.save_env_silent()
        except OSError as exc:
            self._set_text(self.diag, f"ERROR  не удалось сохранить настройки: {exc}")
            return

        def worker() -> None:
            try:
                cfg = Config.load(self.root_dir)
                checks: list[tuple[str, Path, bool]] = [
                    ("V380Decoder", cfg.decoder, True),
                    ("MediaMTX", cfg.mediamtx, True),
                    ("FFmpeg", Path(cfg.ffmpeg), True),
                    ("native-mediamtx.yml", cfg.root / "native-mediamtx.yml", True),
                    ("TeraBox uploader", cfg.terabox_uploader, False),
                ]
                lines: list[str] = []
                for label, path, required in checks:
                    found = path.is_file() or shutil.which(str(path)) is not None
                    if found:
                        state = "OK   "
                    else:
                        state = "ERROR" if required else "WARN "
                    lines.append(f"{state}  {label}: {path}")

                camera_ok = bool(cfg.camera_id and cfg.password)
                lines.append(
                    ("OK   " if camera_ok else "ERROR") + "  Данные камеры заполнены"
                )
                telegram_set = bool(cfg.telegram_token and cfg.telegram_chat_id)
                if not cfg.telegram_enabled:
                    lines.append("OK     Telegram: отключён")
                else:
                    lines.append(
                        ("OK   " if telegram_set else "WARN ")
                        + "  Telegram: "
                        + ("настроен" if telegram_set else "нет токена или chat id")
                    )
                terabox_needed = (
                    cfg.terabox_events_enabled or cfg.terabox_continuous_enabled
                )
                if terabox_needed:
                    lines.append(
                        ("OK   " if cfg.terabox_ndus else "WARN ")
                        + "  TeraBox NDUS cookie"
                    )
                    lines.append(
                        ("OK   " if shutil.which("node") else "WARN ")
                        + "  Node.js в PATH"
                    )
                lines.append("")
                lines.append(f"RTSP: {cfg.rtsp_url}")
                lines.append(f"События: {cfg.events_dir}")
                lines.append(f"Архив: {cfg.continuous_dir}")
                result = "\n".join(lines)
            except Exception as exc:
                result = f"ERROR  {exc}"
            self._post_ui(lambda text=result: self._set_text(self.diag, text))

        threading.Thread(target=worker, name="diagnostics", daemon=True).start()

    # --------------------------------------------------------------- service

    def start_service(self) -> None:
        if self.service_thread and self.service_thread.is_alive():
            messagebox.showinfo("V380 Event", "Сервис уже запущен")
            return
        try:
            self.save_env_silent()
        except OSError as exc:
            messagebox.showerror("V380 Event", str(exc))
            return
        try:
            service = V380Service(Config.load(self.root_dir))
            service.validate()
        except Exception as exc:
            messagebox.showerror("Ошибка конфигурации", str(exc))
            return

        self.service = service
        self.stopping = False

        def worker() -> None:
            try:
                service.run()
            except Exception as exc:
                error = str(exc)
                self._post_ui(
                    lambda message=error: self.status_var.set(f"Ошибка: {message}")
                )
            finally:
                try:
                    service.stop()
                except Exception:
                    pass

        self.service_thread = threading.Thread(
            target=worker,
            name="v380-service",
            daemon=True,
        )
        self.service_thread.start()
        self.status_var.set("Сервис запускается...")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.after(1000, self._poll_service)

    def _poll_service(self) -> None:
        if self.closing:
            return
        if self.service_thread and self.service_thread.is_alive():
            if not self.stopping and not self.status_var.get().startswith("Ошибка"):
                self.status_var.set("Сервис работает")
            self.after(1000, self._poll_service)
            return

        self.service = None
        self.service_thread = None
        self.stopping = False
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        if not self.status_var.get().startswith("Ошибка"):
            self.status_var.set("Сервис остановлен")

    def stop_service(self) -> None:
        service = self.service
        if service is None or self.stopping:
            return
        self.stopping = True
        self.status_var.set("Остановка сервиса...")
        self.stop_button.configure(state="disabled")

        # service.stop() waits for child processes, so it must not run on the
        # Tk thread: that would freeze the window for up to half a minute.
        def worker() -> None:
            try:
                service.stop()
            except Exception as exc:
                error = str(exc)
                self._post_ui(
                    lambda message=error: self.status_var.set(f"Ошибка: {message}")
                )

        threading.Thread(target=worker, name="v380-stop", daemon=True).start()

    def refresh_logs(self) -> None:
        chunks: list[str] = []
        log_dir = self.root_dir / "data" / "logs"
        for path in sorted(log_dir.glob("*.log")):
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
                chunks.append(f"===== {path.name} =====\n{content[-16000:]}")
            except OSError as exc:
                chunks.append(f"===== {path.name} =====\n{exc}")
        self._set_text(
            self.log_text,
            "\n\n".join(chunks) or "Логи пока отсутствуют.",
        )

    def close(self) -> None:
        service = self.service
        self.closing = True
        if service is not None:
            self.status_var.set("Завершение работы...")
            self.update_idletasks()
            stopper = threading.Thread(target=self._safe_stop, args=(service,))
            stopper.start()
            # Bounded wait: child processes must be terminated before exit, but
            # the window must not hang forever either.
            stopper.join(timeout=20)
        self.destroy()

    @staticmethod
    def _safe_stop(service: V380Service) -> None:
        try:
            service.stop()
        except Exception:
            pass


def main() -> int:
    try:
        V380GUI().mainloop()
    except tk.TclError as exc:
        print(f"Не удалось запустить интерфейс: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
