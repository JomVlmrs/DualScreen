import json
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import cv2
import mss
import numpy as np

import capture
from capture import (
    AudioCapture,
    CaptureSettings,
    _camera_overlay,
    audio_filter_graph,
    loopback_device_ids,
    quality_crf,
    record_capture,
    recording_dimensions,
)
from main import (
    default_recording_name,
    fit_preview_size,
    format_elapsed,
    optimize_recording_settings,
    safe_recording_name,
    set_window_capture_exclusion,
    toggle_event,
)
from profiles import AUTO_DESKTOP_AUDIO_ID, default_profile, load_profile, save_profile


class RecordingNameTests(unittest.TestCase):
    def test_default_name_includes_random_six_digit_serial(self):
        with patch("main.secrets.randbelow", return_value=12345):
            name = default_recording_name()

        self.assertRegex(name, r"^Recording_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_112345$")

    def test_adds_mp4_extension(self):
        self.assertEqual(safe_recording_name("demo"), "demo.mp4")

    def test_removes_duplicate_mp4_extension(self):
        self.assertEqual(safe_recording_name("demo.MP4"), "demo.mp4")

    def test_replaces_windows_path_characters(self):
        self.assertEqual(safe_recording_name('lesson: "one"'), "lesson_ _one_.mp4")

    def test_rejects_empty_name(self):
        with self.assertRaises(ValueError):
            safe_recording_name(" . ")


class ElapsedTimeTests(unittest.TestCase):
    def test_formats_hours_minutes_and_seconds(self):
        self.assertEqual(format_elapsed(3661.9), "01:01:01")

    def test_clamps_negative_time(self):
        self.assertEqual(format_elapsed(-5), "00:00:00")


class MicrophoneMuteTests(unittest.TestCase):
    def test_mute_toggle_alternates_event_state(self):
        muted = threading.Event()

        self.assertTrue(toggle_event(muted))
        self.assertTrue(muted.is_set())
        self.assertFalse(toggle_event(muted))
        self.assertFalse(muted.is_set())


class CaptureExclusionTests(unittest.TestCase):
    def test_requests_windows_exclude_from_capture_affinity(self):
        calls = []

        class FakeUser32:
            @staticmethod
            def GetAncestor(handle, flags):
                calls.append(("root", handle, flags))
                return 456

            @staticmethod
            def SetWindowDisplayAffinity(handle, affinity):
                calls.append(("affinity", handle, affinity))
                return 1

        self.assertTrue(
            set_window_capture_exclusion(123, FakeUser32(), platform="win32")
        )
        self.assertEqual(calls, [("root", 123, 2), ("affinity", 456, 0x11)])

    def test_non_windows_platform_skips_windows_capture_api(self):
        self.assertFalse(
            set_window_capture_exclusion(123, platform="linux")
        )


class PreviewSizingTests(unittest.TestCase):
    def test_preview_fits_inside_canvas_without_changing_aspect_ratio(self):
        size = fit_preview_size(3840, 2160, 600, 210)

        self.assertEqual(size, (373, 210))

    def test_preview_is_not_upscaled(self):
        self.assertEqual(fit_preview_size(320, 180, 600, 210), (320, 180))

    def test_rejects_invalid_preview_dimensions(self):
        with self.assertRaises(ValueError):
            fit_preview_size(0, 100, 200, 200)


class RecordingSettingsTests(unittest.TestCase):
    def test_resolution_caps_output_without_upscaling(self):
        self.assertEqual(recording_dimensions(3840, 2160, "1080p"), (1920, 1080))
        self.assertEqual(recording_dimensions(1280, 720, "1080p"), (1280, 720))
        self.assertEqual(recording_dimensions(1920, 1080, "720p"), (1280, 720))

    def test_dimensions_are_even_for_mp4_encoding(self):
        self.assertEqual(recording_dimensions(1365, 767, "Original"), (1364, 766))

    def test_rejects_unknown_resolution_and_quality(self):
        with self.assertRaises(ValueError):
            recording_dimensions(1920, 1080, "4K")
        with self.assertRaises(ValueError):
            quality_crf("Lossless")

    def test_quality_presets_map_to_encoder_quality(self):
        self.assertEqual(quality_crf("High"), "18")
        self.assertEqual(quality_crf("Balanced"), "23")
        self.assertEqual(quality_crf("Compact"), "28")

    def test_default_profile_automatically_captures_output_audio(self):
        self.assertEqual(
            default_profile()["desktop_audio_id"], AUTO_DESKTOP_AUDIO_ID
        )

    def test_automatic_audio_deduplicates_output_loopbacks(self):
        devices = [
            type("Device", (), {"id": "speaker-1", "isloopback": True})(),
            type("Device", (), {"id": "mic-1", "isloopback": False})(),
            type("Device", (), {"id": "speaker-1", "isloopback": True})(),
            type("Device", (), {"id": "headset-1", "isloopback": True})(),
        ]

        self.assertEqual(
            loopback_device_ids(devices), ["speaker-1", "headset-1"]
        )

    def test_noise_reduction_applies_to_microphone_not_desktop_audio(self):
        microphone = Path("microphone.wav")
        desktop = Path("desktop.wav")

        self.assertEqual(
            audio_filter_graph([microphone, desktop], microphone),
            "[1:a:0]adeclick=threshold=5,afftdn=nr=18:nf=-45:tn=1,"
            "agate=threshold=0.02:ratio=4:attack=10:release=250:range=0.25"
            "[clean1];"
            "[clean1][2:a:0]amix=inputs=2:duration=longest:"
            "dropout_transition=0[aout]",
        )
        self.assertIsNone(audio_filter_graph([desktop], microphone))
        self.assertIsNone(audio_filter_graph([], microphone))

    def test_microphone_only_uses_all_noise_reduction_filters(self):
        self.assertIn("adeclick=", capture.MICROPHONE_NOISE_REDUCTION_FILTER)
        self.assertIn("afftdn=", capture.MICROPHONE_NOISE_REDUCTION_FILTER)
        self.assertIn("agate=", capture.MICROPHONE_NOISE_REDUCTION_FILTER)


class AutomaticOptimizationTests(unittest.TestCase):
    def test_selects_high_performance_preset_for_capable_system(self):
        self.assertEqual(
            optimize_recording_settings(3840, 2160, 16, 32),
            {"fps": "60", "resolution": "1080p", "quality": "High"},
        )

    def test_selects_balanced_preset_for_typical_system(self):
        self.assertEqual(
            optimize_recording_settings(2560, 1440, 8, 16),
            {"fps": "30", "resolution": "1080p", "quality": "Balanced"},
        )

    def test_selects_lightweight_preset_for_limited_system(self):
        self.assertEqual(
            optimize_recording_settings(1920, 1080, 4, 8),
            {"fps": "24", "resolution": "720p", "quality": "Compact"},
        )

    def test_preserves_low_resolution_display_without_upscaling(self):
        self.assertEqual(
            optimize_recording_settings(1280, 720, 16, 32)["resolution"],
            "Original",
        )

    def test_rejects_missing_display_or_system_resources(self):
        for values in ((0, 1080, 8, 16), (1920, 1080, 0, 16),
                       (1920, 1080, 8, 0)):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    optimize_recording_settings(*values)


class SceneProfileTests(unittest.TestCase):
    def test_profile_round_trips_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            profile = default_profile()
            profile["scenes"].append(
                {
                    "name": "Camera",
                    "monitor_index": 1,
                    "camera_index": 0,
                    "camera_corner": "Top left",
                }
            )
            profile["selected_scene"] = "Camera"
            profile["resolution"] = "720p"
            profile["quality"] = "High"
            save_profile(path, profile)

            loaded = load_profile(path)
            self.assertEqual(loaded, profile)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_rejects_invalid_scene_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            path.write_text(json.dumps({"scenes": []}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "At least one scene"):
                load_profile(path)


class AudioCaptureTests(unittest.TestCase):
    def _record_audio(self, muted):
        stop_event = threading.Event()
        pause_event = threading.Event()
        muted_event = threading.Event()
        if muted:
            muted_event.set()

        class FakeRecorder:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def record(self, numframes):
                stop_event.set()
                return np.full((numframes, 1), 0.25, dtype=np.float32)

        class FakeDevice:
            channels = 1

            def recorder(self, **_kwargs):
                return FakeRecorder()

        with tempfile.TemporaryDirectory() as directory:
            audio_path = Path(directory) / "audio.wav"
            with patch.object(capture.soundcard, "get_microphone",
                              return_value=FakeDevice()):
                recorder = AudioCapture(
                    "test-device", audio_path, stop_event, pause_event,
                    muted_event,
                )
                recorder.start()
                self.assertTrue(recorder.ready.wait(3))
                recorder.join(timeout=3)

            self.assertIsNone(recorder.error)
            with wave.open(str(audio_path), "rb") as wav_file:
                self.assertEqual(wav_file.getnchannels(), 1)
                self.assertEqual(wav_file.getframerate(), 48000)
                self.assertEqual(wav_file.getnframes(), 4800)
                return wav_file.readframes(wav_file.getnframes())

    def test_audio_capture_writes_a_valid_wave_file(self):
        audio = self._record_audio(muted=False)
        self.assertTrue(any(audio))

    def test_muted_microphone_writes_silence_without_stopping_audio(self):
        audio = self._record_audio(muted=True)
        self.assertTrue(audio)
        self.assertFalse(any(audio))


class CameraOverlayTests(unittest.TestCase):
    def test_overlay_is_placed_in_selected_corner(self):
        frame = np.zeros((120, 200, 3), dtype=np.uint8)
        camera_frame = np.full((60, 80, 3), 255, dtype=np.uint8)
        result = _camera_overlay(frame, camera_frame, "Top left")

        self.assertGreater(result[10, 10].min(), 0)
        self.assertEqual(result[-10, -10].max(), 0)
        self.assertEqual(result.shape, frame.shape)


@unittest.skipUnless(sys.platform == "win32", "screen capture requires Windows")
class RecordingIntegrationTests(unittest.TestCase):
    def test_existing_recording_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "existing.mp4"
            output_path.write_bytes(b"keep this recording")
            with self.assertRaisesRegex(FileExistsError, "already exists"):
                record_capture(
                    CaptureSettings(monitor={}, fps=30),
                    output_path,
                    threading.Event(),
                    threading.Event(),
                )
            self.assertEqual(output_path.read_bytes(), b"keep this recording")

    def test_records_a_playable_mp4(self):
        with mss.MSS() as capture_context:
            if len(capture_context.monitors) < 2:
                self.skipTest("No display is available to capture")
            monitor = dict(capture_context.monitors[1])

        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "screen-test.mp4"
            stop_event = threading.Event()
            pause_event = threading.Event()
            worker = threading.Thread(
                target=record_capture,
                args=(
                    CaptureSettings(monitor=monitor, fps=15),
                    output_path,
                    stop_event,
                    pause_event,
                ),
            )
            worker.start()
            time.sleep(1.5)
            stop_event.set()
            worker.join(timeout=30)

            self.assertFalse(worker.is_alive(), "Recorder did not stop in time")
            self.assertGreater(output_path.stat().st_size, 1000)
            video = cv2.VideoCapture(str(output_path))
            try:
                self.assertTrue(video.isOpened(), "MP4 could not be opened")
                self.assertGreater(
                    video.get(cv2.CAP_PROP_FRAME_COUNT), 0,
                    "MP4 contains no video frames",
                )
            finally:
                video.release()

    def test_pause_stops_frames_without_stopping_the_recording(self):
        with mss.MSS() as capture_context:
            if len(capture_context.monitors) < 2:
                self.skipTest("No display is available to capture")
            monitor = dict(capture_context.monitors[1])

        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "pause-test.mp4"
            stop_event = threading.Event()
            pause_event = threading.Event()
            worker = threading.Thread(
                target=record_capture,
                args=(
                    CaptureSettings(monitor=monitor, fps=15),
                    output_path,
                    stop_event,
                    pause_event,
                ),
            )
            worker.start()
            time.sleep(0.35)
            pause_event.set()
            time.sleep(0.5)
            pause_event.clear()
            time.sleep(0.35)
            stop_event.set()
            worker.join(timeout=30)

            self.assertFalse(worker.is_alive(), "Recorder did not stop in time")
            video = cv2.VideoCapture(str(output_path))
            try:
                self.assertTrue(video.isOpened())
                self.assertGreater(video.get(cv2.CAP_PROP_FRAME_COUNT), 0)
                self.assertLess(video.get(cv2.CAP_PROP_FRAME_COUNT), 18)
            finally:
                video.release()


if __name__ == "__main__":
    unittest.main()
