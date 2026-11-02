from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


AUTO_DESKTOP_AUDIO_ID = "__dualscreen_auto_desktop_audio__"


@dataclass
class Scene:
    name: str
    monitor_index: int = 0
    camera_index: int | None = None
    camera_corner: str = "Bottom right"


def default_profile() -> dict[str, Any]:
    return {
        "scenes": [asdict(Scene("Scene 1"))],
        "selected_scene": "Scene 1",
        "fps": 30,
        "resolution": "Original",
        "quality": "Balanced",
        "output_dir": str(Path.home() / "Videos" / "DualScreen"),
        "microphone_id": None,
        "desktop_audio_id": AUTO_DESKTOP_AUDIO_ID,
    }


def load_profile(path: Path) -> dict[str, Any]:
    if not path.exists():
        return default_profile()
    with path.open("r", encoding="utf-8") as file:
        profile = json.load(file)
    if not isinstance(profile, dict):
        raise ValueError("Profile data must be a JSON object.")

    default = default_profile()
    scenes_data = profile.get("scenes", default["scenes"])
    if not isinstance(scenes_data, list) or not scenes_data:
        raise ValueError("At least one scene must be saved in the profile.")
    scenes: list[dict[str, Any]] = []
    scene_names: set[str] = set()
    for data in scenes_data:
        if not isinstance(data, dict):
            raise ValueError("Each scene must be a JSON object.")
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Scene names cannot be empty.")
        if name in scene_names:
            raise ValueError("Scene names must be unique.")
        scene_names.add(name)
        monitor_index = data.get("monitor_index", 0)
        camera_index = data.get("camera_index")
        corner = data.get("camera_corner", "Bottom right")
        if not isinstance(monitor_index, int) or monitor_index < 0:
            raise ValueError(f"Scene {name!r} has an invalid display index.")
        if camera_index is not None and (
            not isinstance(camera_index, int) or camera_index < 0
        ):
            raise ValueError(f"Scene {name!r} has an invalid camera index.")
        if corner not in ("Top left", "Top right", "Bottom left", "Bottom right"):
            raise ValueError(f"Scene {name!r} has an invalid camera position.")
        scenes.append(
            Scene(name, monitor_index, camera_index, corner).__dict__
        )

    selected_scene = profile.get("selected_scene", scenes[0]["name"])
    if not isinstance(selected_scene, str) or selected_scene not in scene_names:
        selected_scene = scenes[0]["name"]
    fps = profile.get("fps", default["fps"])
    if fps not in (15, 24, 30, 60):
        fps = default["fps"]
    resolution = profile.get("resolution", default["resolution"])
    if resolution not in ("Original", "1080p", "720p"):
        resolution = default["resolution"]
    quality = profile.get("quality", default["quality"])
    if quality not in ("High", "Balanced", "Compact"):
        quality = default["quality"]
    output_dir = profile.get("output_dir", default["output_dir"])
    if not isinstance(output_dir, str) or not output_dir.strip():
        output_dir = default["output_dir"]
    microphone_id = profile.get("microphone_id")
    desktop_audio_id = profile.get("desktop_audio_id")
    if microphone_id is not None and not isinstance(microphone_id, str):
        raise ValueError("Microphone device identifier is invalid.")
    if desktop_audio_id is not None and not isinstance(desktop_audio_id, str):
        raise ValueError("Desktop audio device identifier is invalid.")

    return {
        "scenes": scenes,
        "selected_scene": selected_scene,
        "fps": fps,
        "resolution": resolution,
        "quality": quality,
        "output_dir": output_dir,
        "microphone_id": microphone_id,
        "desktop_audio_id": desktop_audio_id,
    }


def save_profile(path: Path, profile: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(profile, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
