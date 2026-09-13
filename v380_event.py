from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request

TELEGRAM_MAX_BYTES = 50 * 1024 * 1024
PROTECTED_ENV_KEYS = {
    "PATH",
    "PATHEXT",
    "COMSPEC",
    "SYSTEMROOT",
    "WINDIR",
    "TEMP",
    "TMP",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
}
_logging_ready = False


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def env_str(name: str, default: str = "") -> str:
    value = (os.getenv(name) or "").strip()
    return value or default


def env_bool(name: str, default: bool) -> bool:
    value = (os.getenv(name) or "").strip().lower()
    if not value:
        return default
    return value not in {"0", "false", "no", "off"}


def env_float(name: str, default: float) -> float:
    value = (os.getenv(name) or "").strip().replace(",", ".")
    try:
        return float(value) if value else default
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    value = (os.getenv(name) or "").strip()
    try:
        return int(float(value)) if value else default
    except ValueError:
        return default


def creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def clean_console_output(value: str) -> str:
    ansi = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
    return ansi.sub("", value).replace("\r", "\n")


def tail_text(path: Path, limit: int = 1600) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            data = handle.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace").strip()


def setup_logging(log_dir: Path) -> None:
    """Configure logging once: rotating file + console when a console exists.

    A file handler is required because the GUI reads data/logs/*.log, and under
    pythonw.exe there is no stderr at all.
    """
    global _logging_ready
    if _logging_ready:
        return

    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(threadName)s] %(message)s"
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "service.log",
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if sys.stderr is not None:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)

    _logging_ready = True


def load_env_file(env_file: Path) -> None:
    """Read KEY=VALUE lines into os.environ.

    An empty value means "not configured": the key is removed so that the
    dataclass defaults below apply instead of an empty string leaking into
    command lines (an empty --username or an empty RTSP URL breaks ffmpeg).
    """
    if not env_file.exists():
        return
    try:
        text = env_file.read_text(encoding="utf-8-sig")
    except OSError as exc:
        logging.warning("cannot read %s: %s", env_file, exc)
        return

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value:
            os.environ[key] = value
        elif key.upper() not in PROTECTED_ENV_KEYS:
            os.environ.pop(key, None)


@dataclass
class Config:
    root: Path
    camera_id: str
    username: str
    password: str
    ffmpeg: str
    decoder: Path
    mediamtx: Path
    rtsp_url: str
    events_dir: Path
    continuous_dir: Path
    buffer_dir: Path
    telegram_token: str
    telegram_chat_id: str
    telegram_proxy: str
    terabox_ndus: str
    terabox_events_dir: str
    terabox_continuous_dir: str
    terabox_uploader: Path
    motion_enabled: bool
    telegram_enabled: bool
    terabox_events_enabled: bool
    continuous_enabled: bool
    terabox_continuous_enabled: bool
    detect_fps: float
    detect_width: int
    detect_height: int
    pre_seconds: int
    post_seconds: int
    cooldown_seconds: int
    threshold: float
    consecutive_frames: int
    segment_seconds: int
    terabox_max_retries: int

    @classmethod
    def load(cls, root: Path) -> "Config":
        root = Path(root).resolve()
        env_file = root / ".env.windows"
        if not env_file.exists():
            example = root / ".env.windows.example"
            if example.exists():
                try:
                    shutil.copy2(example, env_file)
                except OSError as exc:
                    logging.warning("cannot create %s: %s", env_file, exc)
                    env_file = example

        load_env_file(env_file)

        runtime = root / "bin"
        suffix = ".exe" if os.name == "nt" else ""

        def first_file(*candidates: Path) -> Path:
            return next(
                (candidate for candidate in candidates if candidate.is_file()),
                candidates[0],
            )

        decoder = first_file(
            runtime / f"V380Decoder{suffix}",
            runtime / "decoder" / f"V380Decoder{suffix}",
        )
        mediamtx = first_file(
            runtime / f"mediamtx{suffix}",
            runtime / "mediamtx" / f"mediamtx{suffix}",
        )
        ffmpeg = first_file(
            runtime / f"ffmpeg{suffix}",
            runtime / "ffmpeg" / f"ffmpeg{suffix}",
            runtime / "ffmpeg" / "bin" / f"ffmpeg{suffix}",
        )
        uploader = first_file(
            root / "terabox" / "app" / "app-uploader.js",
            root / "terabox" / "app" / "app" / "app-uploader.js",
        )

        ffmpeg_path = env_str("FFMPEG_BIN")
        if not ffmpeg_path:
            ffmpeg_path = str(ffmpeg) if ffmpeg.is_file() else (
                shutil.which("ffmpeg") or str(ffmpeg)
            )

        proxy = env_str("TELEGRAM_HTTPS_PROXY")
        if not proxy and os.name == "nt":
            proxy = cls._windows_proxy()

        return cls(
            root=root,
            camera_id=env_str("V380_CAMERA_ID"),
            username=env_str("V380_USERNAME", "admin"),
            password=os.getenv("V380_PASSWORD", ""),
            ffmpeg=ffmpeg_path,
            decoder=decoder,
            mediamtx=mediamtx,
            rtsp_url=env_str("MOTION_RTSP_URL", "rtsp://127.0.0.1:8554/live"),
            events_dir=root / env_str("EVENTS_DIR", "data/events"),
            continuous_dir=root / env_str("CONTINUOUS_DIR", "data/continuous"),
            buffer_dir=root / "data" / ".event-buffer",
            telegram_token=env_str("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=env_str("TELEGRAM_CHAT_ID"),
            telegram_proxy=proxy,
            terabox_ndus=env_str("TERABOX_NDUS"),
            terabox_events_dir=env_str("TERABOX_EVENTS_DIR", "/V380/events"),
            terabox_continuous_dir=env_str(
                "TERABOX_CONTINUOUS_DIR",
                "/V380/archive",
            ),
            terabox_uploader=uploader,
            motion_enabled=env_bool("MOTION_ENABLED", True),
            telegram_enabled=env_bool("TELEGRAM_ENABLED", True),
            terabox_events_enabled=env_bool("TERABOX_EVENTS_ENABLED", False),
            continuous_enabled=env_bool("CONTINUOUS_ENABLED", True),
            terabox_continuous_enabled=env_bool(
                "TERABOX_CONTINUOUS_ENABLED",
                False,
            ),
            detect_fps=max(1.0, min(10.0, env_float("DETECT_FPS", 4.0))),
            detect_width=max(160, env_int("DETECT_WIDTH", 640)),
            detect_height=max(90, env_int("DETECT_HEIGHT", 360)),
            pre_seconds=max(0, env_int("PREBUFFER_SECONDS", 5)),
            post_seconds=max(1, env_int("POSTBUFFER_SECONDS", 20)),
            cooldown_seconds=max(0, env_int("COOLDOWN_SECONDS", 30)),
            threshold=max(
                0.001,
                min(1.0, env_float("MIN_CHANGED_RATIO", 0.035)),
            ),
            consecutive_frames=max(1, env_int("CONSECUTIVE_FRAMES", 3)),
            segment_seconds=max(
                60,
                env_int("CONTINUOUS_SEGMENT_SECONDS", 1800),
            ),
            terabox_max_retries=max(
                1,
                min(10, env_int("TERABOX_MAX_RETRIES", 3)),
            ),
        )

    @staticmethod
    def _windows_proxy() -> str:
        try:
            import winreg
        except ImportError:
            return ""
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
                server = str(winreg.QueryValueEx(key, "ProxyServer")[0]).strip()
        except OSError:
            return ""
        if not enabled or not server:
            return ""
        if ";" in server:
            # "http=host:port;https=host:port" form
            mapping = dict(
                part.split("=", 1)
                for part in server.split(";")
                if "=" in part
            )
            server = (mapping.get("https") or mapping.get("http") or "").strip()
        if not server:
            return ""
        return server if "://" in server else "http://" + server


class V380Service:
    def __init__(self, config: Config) -> None:
        self.c = config
        self.stop_event = threading.Event()
        self.children: list[subprocess.Popen[bytes]] = []
        self.critical: dict[str, subprocess.Popen[bytes]] = {}
        self.log_files: list[Any] = []
        self.capture_process: subprocess.Popen[bytes] | None = None
        self.buffer_process: subprocess.Popen[bytes] | None = None
        self.previous: bytes | None = None
        self.changed_streak = 0
        self.last_event = time.monotonic() - config.cooldown_seconds - 1.0
        self.event_lock = threading.Lock()
        self.terabox_lock = threading.Lock()
        self.metadata_lock = threading.Lock()
        self.process_lock = threading.Lock()

        self.log_dir = self.c.root / "data" / "logs"
        for directory in (
            self.c.events_dir,
            self.c.continuous_dir,
            self.c.buffer_dir,
            self.log_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        setup_logging(self.log_dir)

    # ------------------------------------------------------------------ setup

    def validate(self) -> None:
        missing: list[str] = []
        programs = (
            ("V380Decoder", self.c.decoder),
            ("MediaMTX", self.c.mediamtx),
            ("FFmpeg", Path(self.c.ffmpeg)),
        )
        for label, path in programs:
            if not self.program_available(path):
                missing.append(f"{label}: {path}")

        if not self.c.camera_id or not self.c.password:
            missing.append("V380_CAMERA_ID/V380_PASSWORD")

        if missing:
            raise RuntimeError(
                "Missing configuration or runtime: " + "; ".join(missing)
            )

    @staticmethod
    def program_available(path: Path) -> bool:
        return path.is_file() or shutil.which(str(path)) is not None

    def open_log(self, name: str, append: bool = False) -> Any:
        path = self.log_dir / f"{name}.log"
        mode = "ab" if append else "wb"
        try:
            if append and path.exists() and path.stat().st_size > 5_000_000:
                mode = "wb"
        except OSError:
            mode = "wb"
        handle = path.open(mode)
        self.log_files.append(handle)
        return handle

    def start_process(
        self,
        name: str,
        command: list[str],
        cwd: Path | None = None,
        critical: bool = False,
    ) -> subprocess.Popen[bytes]:
        log_file = self.open_log(name)
        logging.info("starting %s", name)
        try:
            process = subprocess.Popen(
                command,
                cwd=str(cwd or self.c.root),
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=creation_flags(),
            )
        except OSError as exc:
            raise RuntimeError(f"Не удалось запустить {name}: {exc}") from exc
        with self.process_lock:
            self.children.append(process)
            if critical:
                self.critical[name] = process
        return process

    @staticmethod
    def terminate_process(
        process: subprocess.Popen[Any] | None,
        timeout: float = 8.0,
    ) -> None:
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
        except OSError:
            return
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass

    @staticmethod
    def wait_for_port(host: str, port: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((host, port), timeout=1):
                    return True
            except OSError:
                time.sleep(0.5)
        return False

    def start_native(self) -> None:
        decoder = self.start_process(
            "decoder",
            [
                str(self.c.decoder),
                "--id",
                self.c.camera_id,
                "--username",
                self.c.username,
                "--password",
                self.c.password,
                "--source",
                "cloud",
                "--output",
                "rtsp",
                "--enable-api",
                "--rtsp-port",
                "8554",
                "--http-port",
                "8080",
            ],
            critical=True,
        )
        if self.stop_event.wait(3):
            return
        if decoder.poll() is not None:
            raise RuntimeError(
                "V380Decoder завершился. Проверьте data/logs/decoder.log; "
                "возможно, порт уже занят старым процессом. "
                + tail_text(self.log_dir / "decoder.log", 400)
            )

        media_config = self.c.root / "native-mediamtx.yml"
        if not media_config.is_file():
            raise RuntimeError(f"Не найден файл конфигурации: {media_config}")

        mediamtx = self.start_process(
            "mediamtx",
            [str(self.c.mediamtx), str(media_config)],
            critical=True,
        )
        if self.stop_event.wait(2):
            return
        if mediamtx.poll() is not None:
            raise RuntimeError(
                "MediaMTX завершился. Проверьте data/logs/mediamtx.log. "
                + tail_text(self.log_dir / "mediamtx.log", 400)
            )
        if not self.wait_for_port("127.0.0.1", 8554, 15):
            raise RuntimeError("RTSP-порт 8554 не стал доступен")

    def cleanup_workspace(self) -> None:
        """Remove leftovers from a previous, possibly crashed, run."""
        shutil.rmtree(
            self.c.root / "data" / ".terabox-staging",
            ignore_errors=True,
        )
        for work in self.c.events_dir.glob(".*-work"):
            if work.is_dir():
                shutil.rmtree(work, ignore_errors=True)
        for directory in (self.c.events_dir, self.c.continuous_dir):
            for temporary in directory.glob("*.json.tmp"):
                temporary.unlink(missing_ok=True)
        for partial in self.c.continuous_dir.glob("*.part.mp4"):
            partial.unlink(missing_ok=True)

    # ----------------------------------------------------------- motion input

    def read_frames(self):
        frame_size = self.c.detect_width * self.c.detect_height * 3
        args = [
            str(self.c.ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-rtsp_transport",
            "tcp",
            "-i",
            self.c.rtsp_url,
            "-an",
            "-vf",
            (
                f"fps={self.c.detect_fps},"
                f"scale={self.c.detect_width}:{self.c.detect_height}"
            ),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ]

        stderr_log = self.log_dir / "motion-capture.log"
        process: subprocess.Popen[bytes] | None = None
        try:
            # stderr goes to a file: a PIPE that nobody drains deadlocks ffmpeg
            # as soon as it prints more than the pipe buffer holds.
            with stderr_log.open("wb") as errors:
                process = subprocess.Popen(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=errors,
                    creationflags=creation_flags(),
                )
                self.capture_process = process
                stdout = process.stdout
                buffer = b""
                while stdout is not None and not self.stop_event.is_set():
                    chunk = stdout.read(65536)
                    if not chunk:
                        break
                    buffer += chunk
                    while len(buffer) >= frame_size:
                        frame = buffer[:frame_size]
                        buffer = buffer[frame_size:]
                        yield frame
        finally:
            # Always reap the child, otherwise every reconnect leaks an ffmpeg.
            self.capture_process = None
            self.terminate_process(process)

        if not self.stop_event.is_set():
            raise RuntimeError(
                "FFmpeg RTSP capture stopped: " + tail_text(stderr_log, 800)
            )

    @staticmethod
    def changed_ratio(frame: bytes, previous: bytes | None) -> float:
        if previous is None:
            return 0.0
        limit = min(len(frame), len(previous)) - 2
        if limit <= 0:
            return 0.0
        sample = max(3, (limit // 12000) // 3 * 3)
        changed = 0
        total = 0
        for index in range(0, limit, sample):
            current = (
                frame[index] * 30
                + frame[index + 1] * 59
                + frame[index + 2] * 11
            ) // 100
            old = (
                previous[index] * 30
                + previous[index + 1] * 59
                + previous[index + 2] * 11
            ) // 100
            if abs(current - old) >= 18:
                changed += 1
            total += 1
        return changed / max(1, total)

    # ----------------------------------------------------------- event buffer

    def event_buffer_loop(self) -> None:
        for old in self.c.buffer_dir.glob("buffer-*.ts"):
            old.unlink(missing_ok=True)

        wrap_count = max(90, self.c.pre_seconds + self.c.post_seconds + 30)
        pattern = self.c.buffer_dir / "buffer-%04d.ts"

        while not self.stop_event.is_set():
            args = [
                str(self.c.ffmpeg),
                "-hide_banner",
                "-loglevel",
                "warning",
                "-rtsp_transport",
                "tcp",
                "-i",
                self.c.rtsp_url,
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-f",
                "segment",
                "-segment_time",
                "1",
                "-segment_wrap",
                str(wrap_count),
                "-reset_timestamps",
                "1",
                "-y",
                str(pattern),
            ]
            process: subprocess.Popen[bytes] | None = None
            try:
                log_file = self.open_log("event-buffer", append=True)
                try:
                    process = subprocess.Popen(
                        args,
                        stdin=subprocess.DEVNULL,
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        creationflags=creation_flags(),
                    )
                    self.buffer_process = process
                    while not self.stop_event.wait(1):
                        if process.poll() is not None:
                            break
                finally:
                    log_file.close()
                    if log_file in self.log_files:
                        self.log_files.remove(log_file)
            except OSError as exc:
                logging.error("cannot start event buffer: %s", exc)
            finally:
                self.buffer_process = None
                self.terminate_process(process)

            if not self.stop_event.is_set():
                logging.error("event buffer stopped; restarting in 5 seconds")
                self.stop_event.wait(5)

    # ----------------------------------------------------------- event export

    def run_ffmpeg(self, args: list[str], timeout: int = 180) -> None:
        result = subprocess.run(
            [str(self.c.ffmpeg), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=timeout,
            creationflags=creation_flags(),
            check=False,
        )
        if result.returncode != 0:
            error = result.stderr.decode(errors="replace")[-1600:]
            raise RuntimeError(f"FFmpeg failed ({result.returncode}): {error}")

    @staticmethod
    def concat_entry(path: Path) -> str:
        # Absolute, forward-slash paths keep the concat demuxer independent of
        # the working directory; single quotes are escaped for ffmpeg.
        text = str(path).replace("\\", "/").replace("'", r"'\''")
        return f"file '{text}'"

    def collect_buffer_segments(
        self,
        start_time: float,
        end_time: float,
    ) -> list[Path]:
        found: list[tuple[float, Path]] = []
        for path in self.c.buffer_dir.glob("buffer-*.ts"):
            try:
                stat = path.stat()
            except OSError:
                # The segmenter wraps around and rewrites files while we look.
                continue
            if stat.st_size and start_time <= stat.st_mtime <= end_time:
                found.append((stat.st_mtime, path))
        found.sort(key=lambda item: item[0])
        return [path for _, path in found]

    def build_event(self, trigger_time: float) -> None:
        if not self.event_lock.acquire(blocking=False):
            logging.info("event skipped: previous event is still being built")
            return

        try:
            if self.stop_event.wait(self.c.post_seconds + 2):
                return

            start_time = trigger_time - self.c.pre_seconds - 2
            end_time = time.time() - 0.5
            candidates = self.collect_buffer_segments(start_time, end_time)
            if not candidates:
                raise RuntimeError("Буфер события пуст")

            event_id = (
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%fZ")
                + "-"
                + uuid.uuid4().hex[:8]
            )
            full = self.c.events_dir / f"{event_id}-event-full.mp4"
            telegram = self.c.events_dir / f"{event_id}-event-telegram.mp4"
            work = self.c.events_dir / f".{event_id}-work"
            work.mkdir(parents=True, exist_ok=True)

            try:
                concat_lines: list[str] = []
                for index, source in enumerate(candidates):
                    destination = work / f"segment-{index:04d}.ts"
                    try:
                        shutil.copy2(source, destination)
                    except OSError:
                        continue
                    concat_lines.append(self.concat_entry(destination))

                if not concat_lines:
                    raise RuntimeError("Не удалось скопировать сегменты события")

                concat_file = work / "concat.txt"
                concat_file.write_text(
                    "\n".join(concat_lines) + "\n",
                    encoding="utf-8",
                )

                base_args = [
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat_file),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0?",
                ]
                try:
                    self.run_ffmpeg(
                        [
                            *base_args,
                            "-c",
                            "copy",
                            "-movflags",
                            "+faststart",
                            str(full),
                        ],
                        timeout=180,
                    )
                except (RuntimeError, subprocess.TimeoutExpired) as exc:
                    logging.warning(
                        "stream copy failed, re-encoding event: %s", exc
                    )
                    self.run_ffmpeg(
                        [
                            *base_args,
                            "-c:v",
                            "libx264",
                            "-preset",
                            "veryfast",
                            "-crf",
                            "18",
                            "-pix_fmt",
                            "yuv420p",
                            "-c:a",
                            "aac",
                            "-b:a",
                            "96k",
                            "-movflags",
                            "+faststart",
                            str(full),
                        ],
                        timeout=300,
                    )

                self.run_ffmpeg(
                    [
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-i",
                        str(full),
                        "-map",
                        "0:v:0",
                        "-map",
                        "0:a:0?",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-crf",
                        "21",
                        "-pix_fmt",
                        "yuv420p",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "96k",
                        "-movflags",
                        "+faststart",
                        str(telegram),
                    ],
                    timeout=300,
                )
            finally:
                shutil.rmtree(work, ignore_errors=True)

            metadata_path = self.c.events_dir / f"{event_id}.json"
            metadata: dict[str, Any] = {
                "id": event_id,
                "kind": "event",
                "createdAt": now_iso(),
                "fullPath": str(full),
                "telegramPath": str(telegram),
                "telegram": {"state": "pending"},
                "archive": {"state": "pending", "attempts": 0},
            }
            self.save_metadata(metadata_path, metadata)
            logging.info("motion event created: %s", event_id)

            if self.telegram_ready():
                try:
                    self.send_telegram(telegram, event_id)
                    metadata["telegram"] = {
                        "state": "sent",
                        "sentAt": now_iso(),
                    }
                    telegram.unlink(missing_ok=True)
                    logging.info(
                        "local Telegram clip deleted after delivery: %s",
                        telegram.name,
                    )
                except Exception as exc:
                    metadata["telegram"] = {
                        "state": "failed",
                        "message": str(exc),
                    }
                    logging.exception("Telegram delivery failed")
            else:
                metadata["telegram"] = {"state": "disabled"}
                telegram.unlink(missing_ok=True)
            self.save_metadata(metadata_path, metadata)

            if self.c.terabox_events_enabled and self.c.terabox_ndus:
                metadata["archive"] = {"state": "queued", "attempts": 0}
                self.save_metadata(metadata_path, metadata)
                threading.Thread(
                    target=self.upload_terabox,
                    args=(
                        full,
                        self.c.terabox_events_dir,
                        metadata,
                        metadata_path,
                        True,
                    ),
                    name="terabox-event",
                    daemon=True,
                ).start()
        except Exception:
            logging.exception("event creation failed")
        finally:
            self.event_lock.release()

    def save_metadata(self, path: Path, metadata: dict[str, Any]) -> None:
        with self.metadata_lock:
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(path)

    @staticmethod
    def read_metadata(path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logging.warning("cannot read %s: %s", path.name, exc)
            return None
        if not isinstance(data, dict):
            logging.warning("unexpected metadata format in %s", path.name)
            return None
        return data

    # -------------------------------------------------------------- telegram

    def telegram_ready(self) -> bool:
        return bool(
            self.c.telegram_enabled
            and self.c.telegram_token
            and self.c.telegram_chat_id
        )

    def send_telegram(self, file_path: Path, event_id: str) -> None:
        size = file_path.stat().st_size
        if size > TELEGRAM_MAX_BYTES:
            raise RuntimeError(
                f"клип {size / 1048576:.1f} МБ превышает лимит Telegram "
                "(50 МБ): уменьшите POSTBUFFER_SECONDS или качество"
            )

        boundary = "----V380Boundary" + uuid.uuid4().hex
        data = file_path.read_bytes()
        parts: list[bytes] = [
            (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
                f"{self.c.telegram_chat_id}\r\n"
            ).encode(),
            (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="caption"\r\n\r\n'
                f"V380: движение {event_id}\r\n"
            ).encode(),
            (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="video"; '
                f'filename="{file_path.name}"\r\n'
                "Content-Type: video/mp4\r\n\r\n"
            ).encode()
            + data
            + b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
        body = b"".join(parts)
        req = request.Request(
            f"https://api.telegram.org/bot{self.c.telegram_token}/sendVideo",
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            },
            method="POST",
        )
        if self.c.telegram_proxy:
            opener = request.build_opener(
                request.ProxyHandler(
                    {
                        "http": self.c.telegram_proxy,
                        "https": self.c.telegram_proxy,
                    }
                )
            )
        else:
            # An empty ProxyHandler also disables environment proxies.
            opener = request.build_opener(request.ProxyHandler({}))

        try:
            with opener.open(req, timeout=180) as response:
                payload = response.read().decode("utf-8", errors="replace")
        except urlerror.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[-600:]
            raise RuntimeError(f"Telegram HTTP {exc.code}: {detail}") from exc
        except OSError as exc:
            raise RuntimeError(f"Telegram network error: {exc}") from exc

        try:
            result = json.loads(payload)
        except ValueError as exc:
            raise RuntimeError(
                f"Telegram returned invalid JSON: {payload[:300]}"
            ) from exc
        if not isinstance(result, dict) or not result.get("ok"):
            raise RuntimeError(str(result)[:600])

    # --------------------------------------------------------------- terabox

    def upload_terabox(
        self,
        file_path: Path,
        remote_dir: str,
        metadata: dict[str, Any] | None = None,
        metadata_path: Path | None = None,
        delete_after_success: bool = False,
    ) -> bool:
        with self.terabox_lock:
            archive: dict[str, Any] = (
                metadata.setdefault("archive", {}) if metadata is not None else {}
            )
            if not isinstance(archive, dict):
                archive = {}
                if metadata is not None:
                    metadata["archive"] = archive

            def persist() -> None:
                if metadata is None or metadata_path is None:
                    return
                try:
                    self.save_metadata(metadata_path, metadata)
                except OSError as exc:
                    logging.warning("cannot save %s: %s", metadata_path.name, exc)

            try:
                attempts = int(float(archive.get("attempts", 0) or 0))
            except (TypeError, ValueError):
                attempts = 0

            if attempts >= self.c.terabox_max_retries:
                archive["state"] = "abandoned"
                persist()
                return False

            attempts += 1
            archive.update(
                {
                    "state": "uploading",
                    "attempts": attempts,
                    "startedAt": now_iso(),
                }
            )
            persist()

            if not file_path.is_file():
                self.mark_upload_failed(
                    archive,
                    f"локальный файл отсутствует: {file_path}",
                    self.c.terabox_max_retries,
                )
                persist()
                return False

            if not self.c.terabox_uploader.is_file():
                self.mark_upload_failed(
                    archive,
                    "TeraBox uploader is not installed",
                    attempts,
                )
                persist()
                return False

            success = False
            staging = self.c.root / "data" / ".terabox-staging" / uuid.uuid4().hex
            try:
                script_dir = self.c.terabox_uploader.parent
                package_root = (
                    script_dir
                    if (script_dir / "package.json").exists()
                    else script_dir.parent
                )
                (script_dir / ".config.yaml").write_text(
                    "accounts:\n  MainAcc: "
                    + json.dumps(self.c.terabox_ndus)
                    + "\n",
                    encoding="utf-8",
                )

                node = shutil.which("node")
                if not node:
                    suffix = ".exe" if os.name == "nt" else ""
                    bundled = self.c.root / "bin" / f"node{suffix}"
                    if not bundled.is_file():
                        raise RuntimeError(
                            "Node.js не найден: установите Node или положите "
                            f"его в {bundled}"
                        )
                    node = str(bundled)

                staging.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file_path, staging / file_path.name)

                child = subprocess.run(
                    [
                        node,
                        str(self.c.terabox_uploader),
                        "-a",
                        "MainAcc",
                        "-l",
                        str(staging),
                        "-r",
                        remote_dir,
                    ],
                    cwd=str(package_root),
                    env={
                        **os.environ,
                        "CI": "1",
                        "NO_COLOR": "1",
                        "FORCE_COLOR": "0",
                    },
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=900,
                    creationflags=creation_flags(),
                    check=False,
                )
                output = clean_console_output(
                    (child.stdout or "") + (child.stderr or "")
                )
                lowered = output.lower()

                if "ndus cookie is bad" in lowered or '"ndus" cookie is bad' in lowered:
                    message = "TeraBox отклонил NDUS cookie"
                    archive.update(
                        {
                            "state": "abandoned",
                            "message": message,
                            "attempts": attempts,
                        }
                    )
                    archive.pop("nextRetryAt", None)
                    logging.error(message)
                else:
                    uploaded = ":: uploaded:" in lowered
                    duplicate = "same name, skipping" in lowered
                    success = child.returncode == 0 and (uploaded or duplicate)
                    if success:
                        archive.update(
                            {
                                "state": "uploaded",
                                "remoteDirectory": remote_dir,
                                "uploadedAt": now_iso(),
                                "attempts": attempts,
                            }
                        )
                        archive.pop("message", None)
                        archive.pop("nextRetryAt", None)
                    else:
                        message = (
                            "TeraBox не подтвердил загрузку, код "
                            f"{child.returncode}: " + output[-1600:]
                        )
                        self.mark_upload_failed(archive, message, attempts)
                        logging.error("%s", message)
            except subprocess.TimeoutExpired:
                message = "TeraBox upload timeout after 900 seconds"
                self.mark_upload_failed(archive, message, attempts)
                logging.error("%s", message)
            except Exception as exc:
                message = f"TeraBox upload error: {exc}"
                self.mark_upload_failed(archive, message, attempts)
                logging.error("%s", message)
            finally:
                shutil.rmtree(staging, ignore_errors=True)

            persist()

            if success and delete_after_success:
                try:
                    file_path.unlink(missing_ok=True)
                    if (
                        metadata_path is not None
                        and metadata is not None
                        and metadata.get("kind") == "continuous"
                    ):
                        metadata_path.unlink(missing_ok=True)
                    logging.info(
                        "local file deleted after confirmed TeraBox upload: %s",
                        file_path.name,
                    )
                except OSError as exc:
                    logging.warning(
                        "TeraBox upload succeeded, but cleanup failed for %s: %s",
                        file_path,
                        exc,
                    )
            return success

    def mark_upload_failed(
        self,
        archive: dict[str, Any],
        message: str,
        attempts: int,
    ) -> None:
        state = "abandoned" if attempts >= self.c.terabox_max_retries else "failed"
        delay = min(3600, 60 * (2 ** max(0, attempts - 1)))
        archive.update(
            {
                "state": state,
                "attempts": attempts,
                "message": message,
                "nextRetryAt": time.time() + delay,
            }
        )

    def recover_stale_uploads(self) -> None:
        """Uploads interrupted by a crash stay "uploading" forever otherwise."""
        for directory in (self.c.events_dir, self.c.continuous_dir):
            for metadata_path in directory.glob("*.json"):
                metadata = self.read_metadata(metadata_path)
                if metadata is None:
                    continue
                archive = metadata.get("archive")
                if not isinstance(archive, dict):
                    continue
                if archive.get("state") not in {"queued", "uploading"}:
                    continue
                archive.update(
                    {
                        "state": "failed",
                        "message": "прервано перезапуском сервиса",
                        "nextRetryAt": 0,
                    }
                )
                try:
                    self.save_metadata(metadata_path, metadata)
                except OSError as exc:
                    logging.warning("cannot update %s: %s", metadata_path.name, exc)

    # ------------------------------------------------------------ retry loop

    def retry_events(self) -> None:
        for metadata_path in sorted(self.c.events_dir.glob("*.json")):
            if self.stop_event.is_set():
                return
            try:
                metadata = self.read_metadata(metadata_path)
                if metadata is None:
                    continue
                event_id = str(metadata.get("id") or metadata_path.stem)
                telegram_path = Path(str(metadata.get("telegramPath") or "__none__"))
                full_path = Path(str(metadata.get("fullPath") or "__none__"))

                telegram_info = metadata.get("telegram")
                if not isinstance(telegram_info, dict):
                    telegram_info = {}
                telegram_state = telegram_info.get("state")

                if self.telegram_ready() and telegram_state in {"pending", "failed"}:
                    if telegram_path.is_file():
                        try:
                            self.send_telegram(telegram_path, event_id)
                            metadata["telegram"] = {
                                "state": "sent",
                                "sentAt": now_iso(),
                            }
                            telegram_state = "sent"
                            telegram_path.unlink(missing_ok=True)
                            logging.info(
                                "local Telegram clip deleted after delivery: %s",
                                telegram_path.name,
                            )
                        except Exception as exc:
                            metadata["telegram"] = {
                                "state": "failed",
                                "message": str(exc),
                            }
                            logging.warning(
                                "Telegram retry failed for %s: %s", event_id, exc
                            )
                    else:
                        metadata["telegram"] = {
                            "state": "missing",
                            "message": "клип для Telegram отсутствует",
                        }
                        telegram_state = "missing"
                    self.save_metadata(metadata_path, metadata)

                archive = metadata.get("archive")
                if not isinstance(archive, dict):
                    archive = {}

                if telegram_state == "sent" and telegram_path.is_file():
                    telegram_path.unlink(missing_ok=True)
                if archive.get("state") == "uploaded" and full_path.is_file():
                    full_path.unlink(missing_ok=True)

                try:
                    attempts = int(float(archive.get("attempts", 0) or 0))
                    next_retry = float(archive.get("nextRetryAt", 0) or 0)
                except (TypeError, ValueError):
                    attempts, next_retry = 0, 0.0

                if (
                    self.c.terabox_events_enabled
                    and self.c.terabox_ndus
                    and archive.get("state") == "failed"
                    and next_retry <= time.time()
                    and attempts < self.c.terabox_max_retries
                    and full_path.is_file()
                ):
                    self.upload_terabox(
                        full_path,
                        self.c.terabox_events_dir,
                        metadata,
                        metadata_path,
                        True,
                    )
            except Exception as exc:
                logging.warning("retry failed for %s: %s", metadata_path.name, exc)

    def retry_continuous(self) -> None:
        if not (self.c.terabox_continuous_enabled and self.c.terabox_ndus):
            return
        for metadata_path in sorted(self.c.continuous_dir.glob("*.json")):
            if self.stop_event.is_set():
                return
            try:
                metadata = self.read_metadata(metadata_path)
                if metadata is None:
                    continue
                segment_path = Path(str(metadata.get("fullPath") or "__none__"))
                archive = metadata.get("archive")
                if not isinstance(archive, dict):
                    continue
                try:
                    attempts = int(float(archive.get("attempts", 0) or 0))
                    next_retry = float(archive.get("nextRetryAt", 0) or 0)
                except (TypeError, ValueError):
                    attempts, next_retry = 0, 0.0
                if (
                    archive.get("state") == "failed"
                    and next_retry <= time.time()
                    and attempts < self.c.terabox_max_retries
                    and segment_path.is_file()
                ):
                    self.upload_terabox(
                        segment_path,
                        self.c.terabox_continuous_dir,
                        metadata,
                        metadata_path,
                        True,
                    )
            except Exception as exc:
                logging.warning(
                    "continuous retry failed for %s: %s",
                    metadata_path.name,
                    exc,
                )

    def retry_loop(self) -> None:
        while not self.stop_event.wait(30):
            self.retry_events()
            self.retry_continuous()

    # ----------------------------------------------------------------- loops

    def motion_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.previous = None
                self.changed_streak = 0
                for frame in self.read_frames():
                    if self.stop_event.is_set():
                        break
                    ratio = self.changed_ratio(frame, self.previous)
                    self.previous = frame

                    if ratio >= self.c.threshold:
                        self.changed_streak += 1
                    else:
                        self.changed_streak = 0

                    ready = (
                        self.changed_streak >= self.c.consecutive_frames
                        and time.monotonic() - self.last_event
                        >= self.c.cooldown_seconds
                    )
                    if ready:
                        self.last_event = time.monotonic()
                        self.changed_streak = 0
                        threading.Thread(
                            target=self.build_event,
                            args=(time.time(),),
                            name="event-builder",
                            daemon=True,
                        ).start()
            except Exception as exc:
                logging.error("motion source error: %s", exc)
            if not self.stop_event.is_set():
                self.stop_event.wait(5)

    def continuous_loop(self) -> None:
        while not self.stop_event.is_set():
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%fZ")
            partial = self.c.continuous_dir / f"{stamp}.part.mp4"
            final = self.c.continuous_dir / f"{stamp}.mp4"
            args = [
                str(self.c.ffmpeg),
                "-hide_banner",
                "-loglevel",
                "warning",
                "-rtsp_transport",
                "tcp",
                "-i",
                self.c.rtsp_url,
                "-t",
                str(self.c.segment_seconds),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-movflags",
                "+faststart",
                "-y",
                str(partial),
            ]
            failed = False
            try:
                result = subprocess.run(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=self.c.segment_seconds + 120,
                    creationflags=creation_flags(),
                    check=False,
                )
                if result.returncode == 0 and partial.is_file():
                    partial.replace(final)
                    self.publish_segment(stamp, final)
                else:
                    failed = True
                    logging.error(
                        "continuous segment failed (%s): %s",
                        result.returncode,
                        result.stderr.decode(errors="replace")[-1000:],
                    )
                    partial.unlink(missing_ok=True)
            except Exception as exc:
                failed = True
                logging.error("continuous segment failed: %s", exc)
                partial.unlink(missing_ok=True)

            # Without this backoff a broken RTSP source spawns ffmpeg in a
            # tight loop and floods the log.
            if failed and not self.stop_event.is_set():
                self.stop_event.wait(5)

    def publish_segment(self, stamp: str, final: Path) -> None:
        if not (self.c.terabox_continuous_enabled and self.c.terabox_ndus):
            return
        metadata_path = final.with_name(final.stem + ".json")
        metadata: dict[str, Any] = {
            "id": stamp,
            "kind": "continuous",
            "createdAt": now_iso(),
            "fullPath": str(final),
            "archive": {"state": "queued", "attempts": 0},
        }
        self.save_metadata(metadata_path, metadata)
        threading.Thread(
            target=self.upload_terabox,
            args=(
                final,
                self.c.terabox_continuous_dir,
                metadata,
                metadata_path,
                True,
            ),
            name="terabox-continuous",
            daemon=True,
        ).start()

    # ------------------------------------------------------------- lifecycle

    def run(self) -> None:
        self.validate()
        self.cleanup_workspace()
        self.recover_stale_uploads()
        self.start_native()
        if self.stop_event.is_set():
            return

        targets: list[tuple[str, Any]] = []
        if self.c.motion_enabled:
            targets.append(("event-buffer", self.event_buffer_loop))
            targets.append(("motion", self.motion_loop))
        if self.c.continuous_enabled:
            targets.append(("continuous", self.continuous_loop))
        targets.append(("delivery-retry", self.retry_loop))

        threads = [
            threading.Thread(target=target, name=name, daemon=True)
            for name, target in targets
        ]
        for thread in threads:
            thread.start()

        logging.info(
            "V380 Python service started (motion=%s, continuous=%s)",
            self.c.motion_enabled,
            self.c.continuous_enabled,
        )

        dead = ""
        while not self.stop_event.wait(1):
            with self.process_lock:
                items = list(self.critical.items())
            dead = next((name for name, proc in items if proc.poll() is not None), "")
            if dead:
                break

        if dead:
            self.stop()
            raise RuntimeError(
                f"Процесс {dead} неожиданно завершился. "
                f"Проверьте data/logs/{dead}.log: "
                + tail_text(self.log_dir / f"{dead}.log", 400)
            )
        logging.info("V380 Python service stopped")

    def stop(self) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        logging.info("stopping V380 service")

        self.terminate_process(self.capture_process, timeout=5)
        self.terminate_process(self.buffer_process, timeout=5)

        with self.process_lock:
            children = list(reversed(self.children))
            self.children.clear()
            self.critical.clear()
        for child in children:
            self.terminate_process(child)

        for handle in self.log_files:
            try:
                handle.close()
            except OSError:
                pass
        self.log_files.clear()


def resolve_root(argv: list[str]) -> Path:
    if len(argv) > 1:
        return Path(argv[1]).resolve()
    script_dir = Path(__file__).resolve().parent
    if (script_dir / "bin").exists() or (script_dir / "native-mediamtx.yml").exists():
        return script_dir
    return script_dir.parent


def main() -> int:
    root = resolve_root(sys.argv)
    try:
        service = V380Service(Config.load(root))
    except Exception:
        setup_logging(root / "data" / "logs")
        logging.exception("V380 configuration failed")
        return 2

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        handler = getattr(signal, name, None)
        if handler is not None:
            try:
                signal.signal(handler, lambda *_: service.stop())
            except (ValueError, OSError):
                pass

    try:
        service.run()
    except Exception:
        logging.exception("V380 service failed")
        return 1
    finally:
        service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
