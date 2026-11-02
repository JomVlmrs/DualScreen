package com.dualscreen.recorder;

import android.Manifest;
import android.app.Activity;
import android.app.ActivityManager;
import android.annotation.SuppressLint;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.media.projection.MediaProjectionManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.Settings;
import android.util.DisplayMetrics;
import android.view.Gravity;
import android.view.WindowInsets;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Spinner;
import android.widget.ArrayAdapter;
import android.widget.TextView;
import android.widget.Toast;

public final class MainActivity extends Activity {
    private static final int REQUEST_PERMISSIONS = 10;
    private static final int REQUEST_OVERLAY = 11;
    private static final int REQUEST_CAPTURE = 12;
    private static final String PREFERENCES = "recorder_settings";

    private Spinner fpsSpinner;
    private Spinner resolutionSpinner;
    private Spinner qualitySpinner;
    private CheckBox microphoneCheckbox;
    private CheckBox deviceAudioCheckbox;
    private TextView statusView;
    private TextView microphoneStateView;
    private Button recordButton;
    private boolean waitingForOverlay;
    private boolean waitingForPermissions;

    private final BroadcastReceiver statusReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            boolean running = intent.getBooleanExtra(RecorderService.EXTRA_RUNNING, false);
            boolean paused = intent.getBooleanExtra(RecorderService.EXTRA_PAUSED, false);
            boolean microphone = intent.getBooleanExtra(RecorderService.EXTRA_MICROPHONE, false);
            boolean microphoneMuted = intent.getBooleanExtra(
                    RecorderService.EXTRA_MICROPHONE_MUTED, false);
            boolean deviceAudio = intent.getBooleanExtra(
                    RecorderService.EXTRA_DEVICE_AUDIO, false);
            boolean noiseReduction = intent.getBooleanExtra(
                    RecorderService.EXTRA_NOISE_REDUCTION, false);
            String message = intent.getStringExtra(RecorderService.EXTRA_MESSAGE);
            String micStatus = microphone
                    ? " · Mic: " + (microphoneMuted ? "MUTED" : "LIVE")
                    : " · Mic: unavailable";
            String deviceAudioStatus = deviceAudio ? " · App audio enabled" : "";
            if (message != null) {
                statusView.setText(message + (running ? micStatus + deviceAudioStatus : ""));
                if (running) {
                    microphoneStateView.setText(microphone
                            ? (microphoneMuted ? "Mic MUTED" : "Mic LIVE")
                            : "Mic off");
                    microphoneStateView.append(deviceAudio
                            ? "  ·  App audio enabled" : "");
                    microphoneStateView.append(microphone
                            ? (noiseReduction ? "  ·  Noise reduction ON"
                                    : "  ·  Noise reduction unavailable") : "");
                    microphoneStateView.setTextColor(!microphone
                            ? Color.rgb(155, 163, 178)
                            : (microphoneMuted ? Color.rgb(255, 93, 104)
                                    : Color.rgb(93, 220, 147)));
                }
            } else if (running) {
                statusView.setText((paused ? "Recording paused" : "Recording in progress")
                        + micStatus + deviceAudioStatus);
                microphoneStateView.setText(microphone
                        ? (microphoneMuted ? "Mic MUTED" : "Mic LIVE")
                        : "Mic off");
                microphoneStateView.append(deviceAudio ? "  ·  App audio enabled" : "");
                microphoneStateView.append(microphone
                        ? (noiseReduction ? "  ·  Noise reduction ON"
                                : "  ·  Noise reduction unavailable") : "");
                microphoneStateView.setTextColor(!microphone
                        ? Color.rgb(155, 163, 178)
                        : (microphoneMuted ? Color.rgb(255, 93, 104)
                                : Color.rgb(93, 220, 147)));
            } else {
                statusView.setText("Ready to record");
                boolean microphoneEnabled = microphoneCheckbox.isChecked();
                microphoneStateView.setText(microphoneEnabled
                        ? "Microphone ready · LIVE" : "Microphone audio is off");
                microphoneStateView.setTextColor(microphoneCheckbox.isChecked()
                        ? Color.rgb(93, 220, 147) : Color.rgb(155, 163, 178));
            }
            recordButton.setText(running ? "Recording…" : "Start recording");
            recordButton.setEnabled(!running);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        buildInterface();
        loadSettings();
    }

    @Override
    @SuppressLint("UnspecifiedRegisterReceiverFlag")
    protected void onStart() {
        super.onStart();
        IntentFilter filter = new IntentFilter(RecorderService.ACTION_STATUS);
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(statusReceiver, filter, Context.RECEIVER_NOT_EXPORTED);
        } else {
            registerReceiver(statusReceiver, filter);
        }
    }

    @Override
    protected void onStop() {
        unregisterReceiver(statusReceiver);
        super.onStop();
    }

    private void buildInterface() {
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.setClipToPadding(false);
        scroll.setOnApplyWindowInsetsListener((view, insets) -> {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                android.graphics.Insets safeInsets = insets.getInsets(
                        WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
                view.setPadding(
                        safeInsets.left, safeInsets.top, safeInsets.right, safeInsets.bottom);
            } else {
                view.setPadding(
                        insets.getSystemWindowInsetLeft(),
                        insets.getSystemWindowInsetTop(),
                        insets.getSystemWindowInsetRight(),
                        insets.getSystemWindowInsetBottom());
            }
            return insets;
        });
        FrameLayout viewport = new FrameLayout(this);
        scroll.addView(viewport, new ScrollView.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.WRAP_CONTENT));
        int screenWidthDp = getResources().getConfiguration().screenWidthDp;
        int contentWidth = Math.min(dp(720), dp(Math.max(1, screenWidthDp - 32)));
        LinearLayout content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        int horizontalPadding = screenWidthDp < 360 ? 14 : 20;
        content.setPadding(dp(horizontalPadding), dp(22), dp(horizontalPadding), dp(24));
        content.setBackgroundColor(Color.rgb(17, 19, 24));
        FrameLayout.LayoutParams contentParams = new FrameLayout.LayoutParams(
                contentWidth, FrameLayout.LayoutParams.WRAP_CONTENT,
                Gravity.TOP | Gravity.CENTER_HORIZONTAL);
        contentParams.setMargins(0, dp(8), 0, dp(16));
        viewport.addView(content, contentParams);

        TextView title = text("DualScreen", 30, Color.WHITE);
        content.addView(title);
        TextView subtitle = text("Simple, reliable screen recording", 15, Color.rgb(155, 163, 178));
        content.addView(subtitle, params(0, 2, 0, 18));

        LinearLayout settingsCard = card(content);
        settingsCard.addView(text("Recording quality", 18, Color.WHITE), params(0, 0, 0, 4));
        settingsCard.addView(text("Choose a preset or optimize for this device.", 13,
                Color.rgb(155, 163, 178)), params(0, 0, 0, 10));

        boolean tabletLayout = screenWidthDp >= 600;
        LinearLayout settingsFields = new LinearLayout(this);
        settingsFields.setOrientation(tabletLayout
                ? LinearLayout.HORIZONTAL : LinearLayout.VERTICAL);
        settingsCard.addView(settingsFields, params(0, 0, 0, 0));
        fpsSpinner = addSetting(settingsFields, "Frame rate",
                new String[]{"15 FPS", "24 FPS", "30 FPS", "60 FPS"}, tabletLayout);
        resolutionSpinner = addSetting(settingsFields, "Resolution",
                new String[]{"Original", "1080p", "720p"}, tabletLayout);
        qualitySpinner = addSetting(settingsFields, "Video quality",
                new String[]{"High", "Balanced", "Compact"}, tabletLayout);

        microphoneCheckbox = new CheckBox(this);
        microphoneCheckbox.setText("Include microphone audio");
        microphoneCheckbox.setTextColor(Color.WHITE);
        microphoneCheckbox.setButtonTintList(android.content.res.ColorStateList.valueOf(Color.rgb(108, 124, 255)));
        microphoneCheckbox.setMinHeight(dp(52));
        microphoneCheckbox.setContentDescription("Include microphone audio");
        settingsCard.addView(microphoneCheckbox, params(0, 8, 0, 0));
        microphoneCheckbox.setOnCheckedChangeListener((buttonView, checked) -> {
            if (microphoneStateView == null || !recordButton.isEnabled()) {
                return;
            }
            microphoneStateView.setText(checked
                    ? "Microphone ready · LIVE" : "Microphone audio is off");
            microphoneStateView.setTextColor(checked
                    ? Color.rgb(93, 220, 147) : Color.rgb(155, 163, 178));
        });

        deviceAudioCheckbox = new CheckBox(this);
        deviceAudioCheckbox.setText(Build.VERSION.SDK_INT >= 29
                ? "Auto-capture supported app playback (Android 10+)"
                : "App playback audio requires Android 10+");
        deviceAudioCheckbox.setTextColor(Color.WHITE);
        deviceAudioCheckbox.setButtonTintList(
                android.content.res.ColorStateList.valueOf(Color.rgb(108, 124, 255)));
        deviceAudioCheckbox.setMinHeight(dp(52));
        deviceAudioCheckbox.setEnabled(Build.VERSION.SDK_INT >= 29);
        deviceAudioCheckbox.setContentDescription(
                "Capture supported audio from other apps in the recording");
        deviceAudioCheckbox.setChecked(Build.VERSION.SDK_INT >= 29);
        settingsCard.addView(deviceAudioCheckbox, params(0, 2, 0, 0));
        settingsCard.addView(text(
                "Android and source apps restrict voice-chat capture. Discord calls may not be captured.",
                12, Color.rgb(155, 163, 178)), params(0, 0, 0, 2));
        settingsCard.addView(text(
                "Automatic microphone noise reduction uses the device filter when available. "
                        + "Loud voices and animal sounds may still be heard.",
                12, Color.rgb(155, 163, 178)), params(0, 0, 0, 2));

        Button optimize = button("Optimize settings");
        optimize.setMinHeight(dp(50));
        settingsCard.addView(optimize, params(0, 6, 0, 0));
        optimize.setOnClickListener(view -> optimizeSettings());

        LinearLayout actionsCard = card(content);
        recordButton = button("Start recording");
        recordButton.setTextSize(17);
        recordButton.setMinHeight(dp(58));
        actionsCard.addView(recordButton, params(0, 0, 0, 8));
        recordButton.setOnClickListener(view -> requestRecordingPermissions());

        statusView = text("Ready to record", 15, Color.rgb(155, 163, 178));
        statusView.setGravity(Gravity.CENTER_HORIZONTAL);
        statusView.setContentDescription("Recording status");
        actionsCard.addView(statusView, params(0, 4, 0, 6));

        microphoneStateView = text("Microphone audio is off", 14,
                Color.rgb(155, 163, 178));
        microphoneStateView.setGravity(Gravity.CENTER_HORIZONTAL);
        microphoneStateView.setContentDescription("Microphone mute status");
        actionsCard.addView(microphoneStateView, params(0, 2, 0, 0));

        TextView note = text(
                "Recordings are saved to Movies/DualScreen. Android asks for screen-capture approval each time. "
                        + "Allow floating controls when prompted. Scroll for all options.",
                13, Color.rgb(155, 163, 178));
        content.addView(note, params(2, 4, 2, 0));
        setContentView(scroll);
    }

    private LinearLayout card(LinearLayout parent) {
        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setPadding(dp(16), dp(16), dp(16), dp(16));
        GradientDrawable background = new GradientDrawable();
        background.setColor(Color.rgb(25, 28, 35));
        background.setCornerRadius(dp(14));
        background.setStroke(dp(1), Color.rgb(48, 54, 68));
        card.setBackground(background);
        parent.addView(card, params(0, 0, 0, 12));
        return card;
    }

    private Spinner addSetting(
            LinearLayout parent, String label, String[] choices, boolean horizontal) {
        LinearLayout field = new LinearLayout(this);
        field.setOrientation(LinearLayout.VERTICAL);
        field.addView(text(label, 13, Color.rgb(200, 206, 218)),
                params(0, 0, 0, 2));
        Spinner spinner = addSpinner(field, choices);
        spinner.setContentDescription(label);
        LinearLayout.LayoutParams fieldParams = new LinearLayout.LayoutParams(
                horizontal ? 0 : LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT, horizontal ? 1 : 0);
        fieldParams.setMargins(0, dp(2), horizontal ? dp(8) : 0, dp(6));
        parent.addView(field, fieldParams);
        return spinner;
    }

    private void requestRecordingPermissions() {
        saveSettings();
        java.util.ArrayList<String> missing = new java.util.ArrayList<>();
        if ((microphoneCheckbox.isChecked() || deviceAudioCheckbox.isChecked())
                && checkSelfPermission(Manifest.permission.RECORD_AUDIO) != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            missing.add(Manifest.permission.RECORD_AUDIO);
        }
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            missing.add(Manifest.permission.POST_NOTIFICATIONS);
        }
        if (!missing.isEmpty()) {
            waitingForPermissions = true;
            requestPermissions(missing.toArray(new String[0]), REQUEST_PERMISSIONS);
            return;
        }
        continueToOverlayPermission();
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode != REQUEST_PERMISSIONS || !waitingForPermissions) {
            return;
        }
        waitingForPermissions = false;
        for (int result : grantResults) {
            if (result != android.content.pm.PackageManager.PERMISSION_GRANTED) {
                Toast.makeText(this, "Recording permission was not granted.", Toast.LENGTH_LONG).show();
                return;
            }
        }
        continueToOverlayPermission();
    }

    private void continueToOverlayPermission() {
        if (Build.VERSION.SDK_INT >= 23 && !Settings.canDrawOverlays(this)) {
            waitingForOverlay = true;
            Intent overlayIntent = new Intent(
                    Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                    Uri.parse("package:" + getPackageName()));
            startActivityForResult(overlayIntent, REQUEST_OVERLAY);
            return;
        }
        requestScreenCapture();
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQUEST_OVERLAY && waitingForOverlay) {
            waitingForOverlay = false;
            if (Build.VERSION.SDK_INT < 23 || Settings.canDrawOverlays(this)) {
                requestScreenCapture();
            } else {
                Toast.makeText(this, "Overlay access is needed for floating recording controls.", Toast.LENGTH_LONG).show();
            }
        } else if (requestCode == REQUEST_CAPTURE) {
            if (resultCode != RESULT_OK || data == null) {
                statusView.setText("Screen capture was cancelled");
                return;
            }
            Intent service = new Intent(this, RecorderService.class);
            service.setAction(RecorderService.ACTION_START);
            service.putExtra(RecorderService.EXTRA_RESULT_CODE, resultCode);
            service.putExtra(RecorderService.EXTRA_CAPTURE_DATA, data);
            service.putExtra(RecorderService.EXTRA_FPS, selectedFps());
            service.putExtra(RecorderService.EXTRA_RESOLUTION, resolutionSpinner.getSelectedItem().toString());
            service.putExtra(RecorderService.EXTRA_QUALITY, qualitySpinner.getSelectedItem().toString());
            service.putExtra(RecorderService.EXTRA_MICROPHONE, microphoneCheckbox.isChecked());
            service.putExtra(RecorderService.EXTRA_DEVICE_AUDIO,
                    deviceAudioCheckbox.isChecked());
            if (Build.VERSION.SDK_INT >= 26) {
                startForegroundService(service);
            } else {
                startService(service);
            }
            statusView.setText("Starting recorder…");
        }
    }

    private void requestScreenCapture() {
        MediaProjectionManager projectionManager =
                (MediaProjectionManager) getSystemService(Context.MEDIA_PROJECTION_SERVICE);
        startActivityForResult(projectionManager.createScreenCaptureIntent(), REQUEST_CAPTURE);
    }

    private void optimizeSettings() {
        DisplayMetrics metrics = new DisplayMetrics();
        ((WindowManager) getSystemService(WINDOW_SERVICE)).getDefaultDisplay().getRealMetrics(metrics);
        int cores = Runtime.getRuntime().availableProcessors();
        ActivityManager.MemoryInfo memoryInfo = new ActivityManager.MemoryInfo();
        ((ActivityManager) getSystemService(ACTIVITY_SERVICE)).getMemoryInfo(memoryInfo);
        long memoryBytes = memoryInfo.totalMem;
        int width = metrics.widthPixels;
        int height = metrics.heightPixels;
        boolean limited = cores <= 4 || memoryBytes < 3L * 1024L * 1024L * 1024L;
        resolutionSpinner.setSelection(width <= 1280 && height <= 720 ? 0 : (limited ? 2 : 1));
        fpsSpinner.setSelection(limited ? 1 : (cores >= 8 && width >= 1920 ? 3 : 2));
        qualitySpinner.setSelection(limited ? 2 : (cores >= 8 ? 0 : 1));
        saveSettings();
        Toast.makeText(this, "Settings optimized for this device.", Toast.LENGTH_SHORT).show();
    }

    private void loadSettings() {
        SharedPreferences preferences = getSharedPreferences(PREFERENCES, MODE_PRIVATE);
        fpsSpinner.setSelection(preferences.getInt("fps", 2));
        resolutionSpinner.setSelection(preferences.getInt("resolution", 0));
        qualitySpinner.setSelection(preferences.getInt("quality", 1));
        microphoneCheckbox.setChecked(preferences.getBoolean("microphone", false));
        deviceAudioCheckbox.setChecked(
                Build.VERSION.SDK_INT >= 29
                        && preferences.getBoolean("deviceAudio", true));
    }

    private void saveSettings() {
        if (fpsSpinner == null) {
            return;
        }
        getSharedPreferences(PREFERENCES, MODE_PRIVATE).edit()
                .putInt("fps", fpsSpinner.getSelectedItemPosition())
                .putInt("resolution", resolutionSpinner.getSelectedItemPosition())
                .putInt("quality", qualitySpinner.getSelectedItemPosition())
                .putBoolean("microphone", microphoneCheckbox.isChecked())
                .putBoolean("deviceAudio", deviceAudioCheckbox.isChecked())
                .apply();
    }

    private int selectedFps() {
        String selection = fpsSpinner.getSelectedItem().toString();
        return Integer.parseInt(selection.substring(0, selection.indexOf(' ')));
    }

    private Spinner addSpinner(LinearLayout parent, String[] choices) {
        Spinner spinner = new Spinner(this);
        ArrayAdapter<String> adapter = new ArrayAdapter<>(
                this, android.R.layout.simple_spinner_item, choices);
        adapter.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
        spinner.setAdapter(adapter);
        spinner.setMinimumHeight(dp(48));
        parent.addView(spinner, params(0, 4, 0, 4));
        return spinner;
    }

    private Button button(String label) {
        Button button = new Button(this);
        button.setText(label);
        button.setAllCaps(false);
        button.setTextColor(Color.WHITE);
        button.setBackgroundTintList(android.content.res.ColorStateList.valueOf(Color.rgb(49, 56, 76)));
        button.setMinimumHeight(dp(48));
        return button;
    }

    private TextView text(String value, int size, int color) {
        TextView text = new TextView(this);
        text.setText(value);
        text.setTextSize(size);
        text.setTextColor(color);
        return text;
    }

    private LinearLayout.LayoutParams params(int left, int top, int right, int bottom) {
        LinearLayout.LayoutParams result = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        result.setMargins(dp(left), dp(top), dp(right), dp(bottom));
        return result;
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
