from __future__ import annotations

import os
import logging
import subprocess
import tempfile
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Protocol

import cv2
import imageio_ffmpeg
import mss
import numpy as np
import soundcard
from profiles import AUTO_DESKTOP_AUDIO_ID


class LoopbackDevice(Protocol):
    id: str
    isloopback: bool


@dataclass(frozen=True)
class CaptureSettings:
    monitor: dict[str, int]
    fps: int
    resolution: str = "Original"
    quality: str = "Balanced"
    camera_index: int | None = None
    camera_corner: str = "Bottom right"
    microphone_id: str | None = None
    desktop_audio_id: str | None = None
    microphone_muted: threading.Event = field(
        default_factory=threading.Event, compare=False, repr=False
    )


def recording_dimensions(
    width: int, height: int, resolution: str
) -> tuple[int, int]:
    if width < 2 or height < 2:
        raise ValueError("The selected display is too small to record.")
    limits = {
        "Original": (width, height),
        "1080p": (1920, 1080),
        "720p": (1280, 720),
    }
    if resolution not in limits:
        raise ValueError("Choose Original, 1080p, or 720p recording resolution.")
    max_width, max_height = limits[resolution]
    scale = min(max_width / width, max_height / height, 1.0)
    return max(2, int(width * scale) & ~1), max(2, int(height * scale) & ~1)


def quality_crf(quality: str) -> str:
    values = {"High": "18", "Balanced": "23", "Compact": "28"}
    try:
        return values[quality]
    except KeyError as exc:
        raise ValueError("Choose High, Balanced, or Compact video quality.") from exc


MICROPHONE_NOISE_REDUCTION_FILTER = (
    "adeclick=threshold=5,"
    "afftdn=nr=18:nf=-45:tn=1,"
    "agate=threshold=0.02:ratio=4:attack=10:release=250:range=0.25"
)


def audio_filter_graph(audio_paths: list[Path], microphone_path: Path) -> str | None:
    if len(audio_paths) < 2:
        return None
    inputs = []
    filters = []
    for index, path in enumerate(audio_paths, start=1):
        source = f"[{index}:a:0]"
        if path == microphone_path:
            output = f"[clean{index}]"
            filters.append(
                f"{source}{MICROPHONE_NOISE_REDUCTION_FILTER}{output}"
            )
            inputs.append(output)
        else:
            inputs.append(source)
    filters.append(
        "".join(inputs)
        + f"amix=inputs={len(audio_paths)}:duration=longest:"
        "dropout_transition=0[aout]"
    )
    return ";".join(filters)


class AudioCapture:
    def __init__(
        self,
        device_id: str,
        wav_path: Path,
        stop_event: threading.Event,
        pause_event: threading.Event,
        muted_event: threading.Event | None = None,
    ) -> None:
        self.device_id = device_id
        self.wav_path = wav_path
        self.stop_event = stop_event
        self.pause_event = pause_event
        self.muted_event = muted_event or threading.Event()
        self.ready = threading.Event()
        self.error: str | None = None
        self.started = False
        self.thread = threading.Thread(
            target=self._capture,
            name="DualScreenAudioCapture",
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()
        self.started = True

    def join(self, timeout: float = 10) -> None:
        if not self.started:
            return
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise RuntimeError("Audio capture did not stop in time.")

    def _capture(self) -> None:
        try:
            device = soundcard.get_microphone(
                self.device_id, include_loopback=True
            )
            channels = min(max(int(device.channels), 1), 2)
            with wave.open(str(self.wav_path), "wb") as wav_file:
                wav_file.setnchannels(channels)
                wav_file.setsampwidth(2)
                wav_file.setframerate(48000)
                with device.recorder(
                    samplerate=48000, channels=channels, blocksize=4800
                ) as recorder:
                    self.ready.set()
                    while not self.stop_event.is_set():
                        if self.pause_event.is_set():
                            self.stop_event.wait(0.05)
                            continue
                        audio = recorder.record(numframes=4800)
                        if self.muted_event.is_set():
                            pcm = np.zeros(audio.shape, dtype="<i2")
                        else:
                            pcm = np.rint(
                                np.clip(audio, -1.0, 1.0) * 32767
                            ).astype("<i2")
                        wav_file.writeframesraw(pcm.tobytes())
        except Exception as exc:
            self.error = str(exc)
            self.ready.set()


def loopback_device_ids(devices: Iterable[LoopbackDevice]) -> list[str]:
    return list(
        dict.fromkeys(
            device.id for device in devices if device.isloopback
        )
    )


def _camera_overlay(
    frame: np.ndarray,
    camera_frame: np.ndarray,
    corner: str,
) -> np.ndarray:
    frame_height, frame_width = frame.shape[:2]
    overlay_width = max(2, frame_width // 4)
    overlay_height = max(
        2,
        min(
            frame_height // 3,
            int(overlay_width * camera_frame.shape[0] / camera_frame.shape[1]),
        ),
    )
    overlay_width = min(
        frame_width // 3,
        max(2, int(overlay_height * camera_frame.shape[1] / camera_frame.shape[0])),
    )
    camera_frame = cv2.resize(
        camera_frame, (overlay_width, overlay_height), interpolation=cv2.INTER_AREA
    )
    margin = max(8, frame_width // 50)
    positions = {
        "Top left": (margin, margin),
        "Top right": (frame_width - overlay_width - margin, margin),
        "Bottom left": (
            margin,
            frame_height - overlay_height - margin,
        ),
        "Bottom right": (
            frame_width - overlay_width - margin,
            frame_height - overlay_height - margin,
        ),
    }
    x, y = positions.get(corner, positions["Bottom right"])
    x = max(0, min(x, frame_width - overlay_width))
    y = max(0, min(y, frame_height - overlay_height))
    frame[y : y + overlay_height, x : x + overlay_width] = camera_frame
    return frame


def _publish_file(source: Path, destination: Path) -> None:
    os.link(source, destination)
    source.unlink()


def record_capture(
    settings: CaptureSettings,
    output_path: Path,
    stop_event: threading.Event,
    pause_event: threading.Event,
    progress: Callable[[str], None] | None = None,
) -> None:
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise FileExistsError(f"Recording already exists:\n{output_path}")
    work_dir = Path(
        tempfile.mkdtemp(prefix=".dualscreen-recovery-", dir=output_path.parent)
    )
    video_path = work_dir / "video.mp4"
    audio_captures: list[AudioCapture] = []
    camera = None
    writer = None
    failure: Exception | None = None

    try:
        monitor = settings.monitor
        source_width = int(monitor["width"])
        source_height = int(monitor["height"])
        width, height = recording_dimensions(
            source_width, source_height, settings.resolution
        )
        if settings.fps not in (15, 24, 30, 60):
            raise ValueError("Choose a supported frame rate.")
        crf = quality_crf(settings.quality)

        writer = cv2.VideoWriter(
            str(video_path),
            cv2.VideoWriter_fourcc(*"mp4v"),
            settings.fps,
            (width, height),
        )
        if not writer.isOpened():
            raise RuntimeError("Could not initialize the MP4 video encoder.")

        if settings.camera_index is not None:
            camera = cv2.VideoCapture(settings.camera_index, cv2.CAP_DSHOW)
            if not camera.isOpened():
                camera.release()
                camera = cv2.VideoCapture(settings.camera_index)
            if not camera.isOpened():
                raise RuntimeError(
                    f"Could not open camera {settings.camera_index}. "
                    "Disconnect and reconnect it, then try again."
                )
            camera.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

        if settings.microphone_id:
            audio_captures.append(
                AudioCapture(
                    settings.microphone_id,
                    work_dir / "microphone.wav",
                    stop_event,
                    pause_event,
                    settings.microphone_muted,
                )
            )
        if settings.desktop_audio_id == AUTO_DESKTOP_AUDIO_ID:
            device_ids = loopback_device_ids(
                soundcard.all_microphones(include_loopback=True)
            )
            if not device_ids:
                raise RuntimeError(
                    "No playback loopback devices are available. "
                    "Reconnect your speakers or headset and try again."
                )
            for index, device_id in enumerate(device_ids, start=1):
                audio_captures.append(
                    AudioCapture(
                        device_id,
                        work_dir / f"desktop-audio-{index}.wav",
                        stop_event,
                        pause_event,
                    )
                )
        elif settings.desktop_audio_id:
            audio_captures.append(
                AudioCapture(
                    settings.desktop_audio_id,
                    work_dir / "desktop-audio.wav",
                    stop_event,
                    pause_event,
                )
            )
        for capture in audio_captures:
            capture.start()
        for capture in audio_captures:
            if not capture.ready.wait(10):
                raise RuntimeError("Timed out while starting audio capture.")
            if capture.error:
                raise RuntimeError(f"Could not start audio capture: {capture.error}")

        interval = 1.0 / settings.fps
        next_frame = time.perf_counter()
        with mss.MSS() as screen:
            while not stop_event.is_set():
                if pause_event.is_set():
                    stop_event.wait(0.05)
                    next_frame = time.perf_counter() + interval
                    continue
                bgra = np.asarray(screen.grab(monitor))
                frame = cv2.cvtColor(bgra, cv2.COLOR_BGRA2BGR)
                if frame.shape[1] != width or frame.shape[0] != height:
                    frame = cv2.resize(
                        frame, (width, height), interpolation=cv2.INTER_AREA
                    )
                if camera is not None:
                    success, camera_frame = camera.read()
                    if success:
                        frame = _camera_overlay(
                            frame, camera_frame, settings.camera_corner
                        )
                writer.write(frame)
                for capture in audio_captures:
                    if capture.error:
                        raise RuntimeError(
                            f"Audio capture stopped: {capture.error}"
                        )
                next_frame += interval
                delay = next_frame - time.perf_counter()
                if delay > 0:
                    stop_event.wait(delay)
                elif delay < -interval:
                    next_frame = time.perf_counter()
    except Exception as exc:
        failure = exc
    finally:
        stop_event.set()
        if camera is not None:
            camera.release()
        if writer is not None:
            writer.release()
        for capture in audio_captures:
            try:
                capture.join()
            except Exception as exc:
                if failure is None:
                    failure = exc
            if capture.error and failure is None:
                failure = RuntimeError(f"Audio capture failed: {capture.error}")

    if failure is not None:
        raise RuntimeError(
            f"{failure}\nRecovery files, if any, are in:\n{work_dir}"
        ) from failure

    if not video_path.exists() or video_path.stat().st_size == 0:
        raise RuntimeError(
            f"No video frames were written. Recovery folder:\n{work_dir}"
        )

    try:
        audio_paths = [
            capture.wav_path
            for capture in audio_captures
            if capture.wav_path.exists() and capture.wav_path.stat().st_size > 44
        ]
        temporary_output = work_dir / "finalizing.mp4"
        command = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-i", str(video_path)]
        for audio_path in audio_paths:
            command.extend(["-i", str(audio_path)])
        microphone_path = work_dir / "microphone.wav"
        if len(audio_paths) == 1:
            command.extend(["-map", "0:v:0", "-map", "1:a:0"])
            if audio_paths[0] == microphone_path:
                command.extend(["-af", MICROPHONE_NOISE_REDUCTION_FILTER])
        elif len(audio_paths) > 1:
            command.extend(
                [
                    "-filter_complex",
                    audio_filter_graph(audio_paths, microphone_path),
                    "-map", "0:v:0", "-map", "[aout]",
                ]
            )
        command.extend(
            [
                "-c:v", "libx264", "-preset", "veryfast", "-crf", crf,
                "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            ]
        )
        if audio_paths:
            command.extend(["-c:a", "aac", "-b:a", "192k", "-shortest"])
        command.append(str(temporary_output))
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0 or not temporary_output.exists():
            detail = result.stderr.strip()[-2000:] or "Unknown FFmpeg error."
            raise RuntimeError(
                f"Could not encode the selected MP4 quality: {detail}\n"
                f"Recovery video is available at:\n{video_path}"
            )
        _publish_file(temporary_output, output_path)
        if progress:
            progress(f"Saved  ·  {output_path.name}")
    except Exception as exc:
        raise RuntimeError(
            f"{exc}\nRecovery files are in:\n{work_dir}"
        ) from exc

    try:
        video_path.unlink(missing_ok=True)
        for capture in audio_captures:
            capture.wav_path.unlink(missing_ok=True)
        work_dir.rmdir()
    except OSError:
        logging.warning("Could not remove temporary recording files in %s", work_dir)
