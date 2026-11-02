# DualScreen

Current app version: **1.1.2** (Windows and Android).

DualScreen is a Windows desktop recording app with OBS-inspired scene and
source controls. It records a selected display to MP4 and can mix a microphone
and desktop-audio loopbacks into the recording. An optional webcam can be
overlaid in any corner.

## Features

- Create, rename, remove, and save multiple scenes.
- Choose a display per scene and optionally add one webcam overlay.
- View a continuously updating preview of the selected display.
- Automatically reflows and scrolls the controls on smaller desktop windows;
  recording controls remain touch-friendly on touchscreen displays.
- Record at 15, 24, 30, or 60 FPS.
- Open **Settings** or use the recording settings controls to choose frame
  rate, Original/1080p/720p output resolution, High/Balanced/Compact H.264
  quality, and the recording folder.
- Use **Optimize** in Settings to automatically select and save a recording
  preset based on the selected display, processor cores, and installed memory.
  The current recording folder is preserved.
- Capture a microphone, desktop loopback audio, both, or neither. Windows
  automatically captures available output loopbacks by default, including
  Discord or another voice client routed to a non-default headset. You can
  instead choose one output or turn desktop audio off. Mirrored outputs may
  cause duplicated audio in the recording.
- Automatically reduce steady microphone noise and sharp clicks on Windows;
  Android uses its built-in microphone noise suppressor when available.
  Windows also applies a gentle noise gate to mic silence. Desktop/teammate
  audio is not filtered. Shouting, barking, and other voice-like sounds may
  remain, since they can be indistinguishable from wanted speech; stronger
  suppression can also distort quiet speech.
- Use the draggable always-on-top recording toolbar to mute/unmute the
  microphone, pause/resume, or end a recording.
- The floating toolbar includes a live recording timer and Windows capture
  exclusion where supported, so it does not appear in supported screen captures.
- The floating toolbar appears as soon as recording starts, stays visible while
  recording or paused (even if the main window is minimized), and hides after
  the recording is finalized.
- Pause and resume; elapsed time excludes paused time.
- Choose the output folder and recording name. The generated default name
  includes a random six-digit serial number to distinguish recordings.
- Recoverable intermediate files are retained if recording or MP4 finalization
  fails. The error dialog identifies their location.
- Application and recording errors are logged to
  `%LOCALAPPDATA%\DualScreen\logs\dualscreen.log`.
- Scene and recording preferences are saved to
  `%APPDATA%\DualScreen\profile.json`.

Shortcuts, when the application window is focused:

- `Ctrl+Alt+R`: start or stop recording.
- `Ctrl+Alt+P`: pause or resume recording.

## Not included

DualScreen is not a full OBS Studio replacement. This build does not provide
live streaming, virtual camera output, browser/game capture, plugins, scene
transitions, filters, replay buffer, or multi-track audio. Camera sources are
selected by Windows camera index; unavailable devices report an error.

## Run the application

Run `dist\DualScreen-1.1.2.exe`. No Python installation is needed to run the
packaged application.

## Build

On Windows with Python 3.10 or later:

```powershell
.\build.ps1
```

The standalone executable is written to `dist\DualScreen-1.1.2.exe`.

## Development

```powershell
python -m pip install -r requirements.txt
python main.py
```

Run the unit and recording integration tests with:

```powershell
python -m unittest discover -s tests -v
```

## Android app

The native Android app is in `app\`. It records the display to MP4 using
Android's screen-capture consent flow, supports optional microphone audio,
pause/resume, a live timer, microphone mute, floating controls, and Android
notification controls. Recordings use a timestamp and random six-digit serial
number in the filename. On Android 10 and newer they are saved to the device's
`Movies\DualScreen` gallery folder. Android 8 and 9 use the app's external
Movies folder. On Android 10+, app playback capture is enabled automatically
and captures playback only when the source app allows it. Android's privacy
rules block communication audio for many voice-chat apps (including Discord in
common configurations), so Android cannot guarantee capture of a teammate's
voice. For dependable voice chat recording, use the Windows version with
automatic output capture.
When the microphone is enabled, Android applies its built-in noise suppressor
when the device supports it; the recording status reports whether it is active.
It can reduce ambient noise, but cannot reliably distinguish barking or
shouting from speech.
The settings use a scrollable phone layout and expand to a centered, wider
layout on tablets; rotation recreates the screen to reflow the controls.

The Android build uses Android Gradle Plugin 9.5.0, Gradle 9.8.2, JDK 17 or
newer, and Android SDK platform 36 (target API 36). From the project root,
build a debug APK with:

```powershell
.\gradlew.bat assembleDebug
```

The APK is written to
`app\build\outputs\apk\debug\app-debug.apk`. Android requires the user to
approve screen capture for each recording and to grant overlay access for the
floating controls. Microphone capture is optional and requires runtime
permission. Unrestricted system audio capture, webcam overlays, and Windows
scene profiles are not included in this Android port. App playback capture is
available only for source audio allowed by Android and the source app.
