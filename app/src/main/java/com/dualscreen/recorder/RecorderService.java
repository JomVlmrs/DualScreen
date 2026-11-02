package com.dualscreen.recorder;

import android.app.Activity;
import android.app.ActivityManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.ContentValues;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.graphics.Color;
import android.graphics.PixelFormat;
import android.hardware.display.DisplayManager;
import android.hardware.display.VirtualDisplay;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioPlaybackCaptureConfiguration;
import android.media.AudioRecord;
import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaFormat;
import android.media.MediaMuxer;
import android.media.audiofx.NoiseSuppressor;
import android.media.projection.MediaProjection;
import android.media.projection.MediaProjectionManager;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.ParcelFileDescriptor;
import android.os.SystemClock;
import android.provider.MediaStore;
import android.util.DisplayMetrics;
import android.util.Log;
import android.view.Gravity;
import android.view.Surface;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.io.File;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.security.SecureRandom;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.concurrent.atomic.AtomicBoolean;

public final class RecorderService extends Service {
    public static final String ACTION_START = "com.dualscreen.recorder.START";
    public static final String ACTION_PAUSE = "com.dualscreen.recorder.PAUSE";
    public static final String ACTION_RESUME = "com.dualscreen.recorder.RESUME";
    public static final String ACTION_STOP = "com.dualscreen.recorder.STOP";
    public static final String ACTION_MUTE = "com.dualscreen.recorder.MUTE";
    public static final String ACTION_STATUS = "com.dualscreen.recorder.STATUS";
    public static final String EXTRA_RUNNING = "running";
    public static final String EXTRA_PAUSED = "paused";
    public static final String EXTRA_MESSAGE = "message";
    public static final String EXTRA_MICROPHONE = "microphone";
    public static final String EXTRA_DEVICE_AUDIO = "deviceAudio";
    public static final String EXTRA_MICROPHONE_MUTED = "microphoneMuted";
    public static final String EXTRA_NOISE_REDUCTION = "noiseReduction";
    public static final String EXTRA_RESULT_CODE = "resultCode";
    public static final String EXTRA_CAPTURE_DATA = "captureData";
    public static final String EXTRA_FPS = "fps";
    public static final String EXTRA_RESOLUTION = "resolution";
    public static final String EXTRA_QUALITY = "quality";

    private static final String TAG = "DualScreenRecorder";
    private static final String CHANNEL_ID = "dualscreen_recording";
    private static final int NOTIFICATION_ID = 41;
    private static final SecureRandom RANDOM = new SecureRandom();
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final AtomicBoolean stopping = new AtomicBoolean();
    private final AtomicBoolean running = new AtomicBoolean();
    private volatile boolean paused;
    private volatile boolean microphoneMuted;
    private volatile long pausedAt;
    private volatile long totalPausedMs;
    private long startedAt;
    private int fps;
    private boolean includeMicrophone;
    private boolean includeDeviceAudio;
    private MediaProjection projection;
    private VirtualDisplay virtualDisplay;
    private Surface videoSurface;
    private MediaCodec videoEncoder;
    private MediaCodec audioEncoder;
    private AudioRecord audioRecord;
    private AudioRecord playbackAudioRecord;
    private NoiseSuppressor noiseSuppressor;
    private volatile boolean microphoneNoiseReductionActive;
    private MediaMuxer muxer;
    private Thread audioThread;
    private Uri outputUri;
    private File legacyOutputFile;
    private ParcelFileDescriptor outputDescriptor;
    private WindowManager windowManager;
    private LinearLayout floatingControls;
    private TextView timerText;
    private TextView muteButton;
    private TextView pauseButton;
    private final Runnable controlsTicker = new Runnable() {
        @Override
        public void run() {
            if (!running.get()) {
                return;
            }
            updateControls();
            mainHandler.postDelayed(this, 1000);
        }
    };

    @Override
    public void onCreate() {
        super.onCreate();
        createNotificationChannel();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null) {
            return START_NOT_STICKY;
        }
        String action = intent.getAction();
        if (ACTION_START.equals(action)) {
            beginForeground(intent);
            return START_NOT_STICKY;
        }
        if (ACTION_PAUSE.equals(action) && running.get() && !paused) {
            paused = true;
            pausedAt = SystemClock.elapsedRealtime();
            if (virtualDisplay != null) {
                virtualDisplay.setSurface(null);
            }
            refreshControls();
        } else if (ACTION_RESUME.equals(action) && running.get() && paused) {
            totalPausedMs += SystemClock.elapsedRealtime() - pausedAt;
            paused = false;
            if (virtualDisplay != null && videoEncoder != null) {
                virtualDisplay.setSurface(videoSurface);
            }
            refreshControls();
        } else if (ACTION_MUTE.equals(action) && running.get() && includeMicrophone) {
            microphoneMuted = !microphoneMuted;
            refreshControls();
        } else if (ACTION_STOP.equals(action) && running.get()) {
            stopping.set(true);
            paused = false;
            refreshControls();
        }
        return START_NOT_STICKY;
    }

    private void beginForeground(Intent intent) {
        if (running.get() || stopping.get()) {
            return;
        }
        int resultCode = intent.getIntExtra(EXTRA_RESULT_CODE, Activity.RESULT_CANCELED);
        Intent captureData = intent.getParcelableExtra(EXTRA_CAPTURE_DATA);
        if (resultCode != Activity.RESULT_OK || captureData == null) {
            stopSelf();
            return;
        }
        fps = intent.getIntExtra(EXTRA_FPS, 30);
        includeMicrophone = intent.getBooleanExtra(EXTRA_MICROPHONE, false);
        includeDeviceAudio = Build.VERSION.SDK_INT >= 29
                && intent.getBooleanExtra(EXTRA_DEVICE_AUDIO, false);
        int serviceTypes = ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION;
        if ((includeMicrophone || includeDeviceAudio) && Build.VERSION.SDK_INT >= 29) {
            serviceTypes |= ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE;
        }
        try {
            if (Build.VERSION.SDK_INT >= 29) {
                startForeground(NOTIFICATION_ID, buildNotification("Preparing recorder…"),
                        serviceTypes);
            } else {
                startForeground(NOTIFICATION_ID, buildNotification("Preparing recorder…"));
            }
        } catch (SecurityException exception) {
            Log.e(TAG, "Foreground recording permission was denied.", exception);
            sendStatus("Recording permission was denied.");
            stopSelf();
            return;
        }
        running.set(true);
        stopping.set(false);
        sendStatus("Preparing recorder…");
        int resolution = intent.getStringExtra(EXTRA_RESOLUTION) == null ? 0
                : resolutionIndex(intent.getStringExtra(EXTRA_RESOLUTION));
        int quality = intent.getStringExtra(EXTRA_QUALITY) == null ? 1
                : qualityIndex(intent.getStringExtra(EXTRA_QUALITY));
        new Thread(() -> runRecorder(resultCode, captureData, resolution, quality),
                "DualScreen-Capture").start();
    }

    private void runRecorder(int resultCode, Intent captureData, int resolution, int quality) {
        boolean completed = false;
        String failure = null;
        try {
            prepareRecorder(resultCode, captureData, resolution, quality);
            startedAt = SystemClock.elapsedRealtime();
            createFloatingControls();
            refreshControls();
            captureFrames();
            finalizeOutput();
            completed = true;
        } catch (Exception exception) {
            failure = exception.getMessage() == null ? exception.toString() : exception.getMessage();
            Log.e(TAG, "Screen recording failed", exception);
            discardOutput();
        } finally {
            releaseRecorder();
            removeFloatingControls();
            running.set(false);
            stopping.set(false);
            paused = false;
            String message = failure != null ? "Recording failed: " + failure
                    : (completed ? "Saved to Movies/DualScreen" : "Recording stopped");
            sendStatus(message);
            mainHandler.post(() -> {
                stopForeground(STOP_FOREGROUND_REMOVE);
                stopSelf();
            });
        }
    }

    private void prepareRecorder(int resultCode, Intent captureData, int resolution, int quality)
            throws Exception {
        MediaProjectionManager projectionManager =
                (MediaProjectionManager) getSystemService(Context.MEDIA_PROJECTION_SERVICE);
        projection = projectionManager.getMediaProjection(resultCode, captureData);
        if (projection == null) {
            throw new IllegalStateException("Android did not create a screen-capture session.");
        }
        projection.registerCallback(new MediaProjection.Callback() {
            @Override
            public void onStop() {
                stopping.set(true);
            }
        }, mainHandler);

        DisplayMetrics metrics = new DisplayMetrics();
        ((WindowManager) getSystemService(WINDOW_SERVICE)).getDefaultDisplay().getRealMetrics(metrics);
        int width = metrics.widthPixels;
        int height = metrics.heightPixels;
        if (resolution == 1) {
            double scale = Math.min(1.0, 1080.0 / Math.max(width, height));
            width = even((int) (width * scale));
            height = even((int) (height * scale));
        } else if (resolution == 2) {
            double scale = Math.min(1.0, 720.0 / Math.max(width, height));
            width = even((int) (width * scale));
            height = even((int) (height * scale));
        } else {
            width = even(width);
            height = even(height);
        }
        int bitRate = quality == 0 ? 12_000_000 : (quality == 2 ? 3_000_000 : 6_000_000);
        MediaFormat videoFormat = MediaFormat.createVideoFormat(
                MediaFormat.MIMETYPE_VIDEO_AVC, width, height);
        videoFormat.setInteger(MediaFormat.KEY_COLOR_FORMAT,
                MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface);
        videoFormat.setInteger(MediaFormat.KEY_BIT_RATE, bitRate);
        videoFormat.setInteger(MediaFormat.KEY_FRAME_RATE, fps);
        videoFormat.setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 2);
        videoEncoder = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC);
        videoEncoder.configure(videoFormat, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE);
        videoSurface = videoEncoder.createInputSurface();
        videoEncoder.start();

        if (includeMicrophone || includeDeviceAudio) {
            prepareAudioCapture();
        }
        createOutput();
        virtualDisplay = projection.createVirtualDisplay(
                "DualScreen", width, height, metrics.densityDpi,
                DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, videoSurface, null, mainHandler);
        if (virtualDisplay == null) {
            throw new IllegalStateException("Android could not create the screen capture display.");
        }
        if (audioRecord != null) {
            audioRecord.startRecording();
        }
        if (playbackAudioRecord != null) {
            playbackAudioRecord.startRecording();
        }
        if (audioEncoder != null) {
            audioThread = new Thread(this::feedMicrophone, "DualScreen-Microphone");
            audioThread.start();
        }
        sendStatus("Recording in progress");
    }

    private void prepareAudioCapture() throws IOException {
        if (checkSelfPermission(android.Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED) {
            throw new SecurityException("Audio capture permission was revoked.");
        }
        int sampleRate = 44_100;
        AudioFormat pcmFormat = new AudioFormat.Builder()
                .setSampleRate(sampleRate)
                .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .build();
        int minimumBuffer = AudioRecord.getMinBufferSize(
                sampleRate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        if (minimumBuffer <= 0) {
            throw new IllegalStateException("Audio capture is unavailable on this device.");
        }
        int bufferSize = Math.max(minimumBuffer, sampleRate * 2);
        if (includeMicrophone) {
            audioRecord = new AudioRecord.Builder()
                    .setAudioSource(android.media.MediaRecorder.AudioSource.MIC)
                    .setAudioFormat(pcmFormat)
                    .setBufferSizeInBytes(bufferSize)
                    .build();
            if (audioRecord.getState() != AudioRecord.STATE_INITIALIZED) {
                throw new IllegalStateException("The microphone could not be initialized.");
            }
            if (NoiseSuppressor.isAvailable()) {
                try {
                    noiseSuppressor = NoiseSuppressor.create(audioRecord.getAudioSessionId());
                    if (noiseSuppressor != null) {
                        noiseSuppressor.setEnabled(true);
                        microphoneNoiseReductionActive = noiseSuppressor.getEnabled();
                    }
                } catch (RuntimeException exception) {
                    Log.w(TAG, "Device microphone noise suppression is unavailable.", exception);
                    if (noiseSuppressor != null) {
                        noiseSuppressor.release();
                        noiseSuppressor = null;
                    }
                }
            }
        }
        if (includeDeviceAudio) {
            if (Build.VERSION.SDK_INT < 29 || projection == null) {
                throw new IllegalStateException(
                        "App audio capture requires Android 10 or later.");
            }
            AudioPlaybackCaptureConfiguration captureConfig =
                    new AudioPlaybackCaptureConfiguration.Builder(projection)
                            .addMatchingUsage(AudioAttributes.USAGE_MEDIA)
                            .addMatchingUsage(AudioAttributes.USAGE_GAME)
                            .addMatchingUsage(AudioAttributes.USAGE_UNKNOWN)
                            .build();
            playbackAudioRecord = new AudioRecord.Builder()
                    .setAudioFormat(pcmFormat)
                    .setBufferSizeInBytes(bufferSize)
                    .setAudioPlaybackCaptureConfig(captureConfig)
                    .build();
            if (playbackAudioRecord.getState() != AudioRecord.STATE_INITIALIZED) {
                throw new IllegalStateException(
                        "App audio capture could not be initialized on this device.");
            }
        }
        MediaFormat audioFormat = MediaFormat.createAudioFormat(
                MediaFormat.MIMETYPE_AUDIO_AAC, sampleRate, 1);
        audioFormat.setInteger(MediaFormat.KEY_AAC_PROFILE,
                MediaCodecInfo.CodecProfileLevel.AACObjectLC);
        audioFormat.setInteger(MediaFormat.KEY_BIT_RATE, 96_000);
        audioFormat.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, 16_384);
        audioEncoder = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_AAC);
        audioEncoder.configure(audioFormat, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE);
        audioEncoder.start();
    }

    private void createOutput() throws IOException {
        String serialNumber = String.format(Locale.US, "%06d", RANDOM.nextInt(1_000_000));
        String name = "DualScreen_" + new SimpleDateFormat(
                "yyyyMMdd_HHmmss", Locale.US).format(new Date())
                + "_" + serialNumber + ".mp4";
        if (Build.VERSION.SDK_INT >= 29) {
            ContentValues values = new ContentValues();
            values.put(MediaStore.Video.Media.DISPLAY_NAME, name);
            values.put(MediaStore.Video.Media.MIME_TYPE, "video/mp4");
            values.put(MediaStore.Video.Media.RELATIVE_PATH,
                    Environment.DIRECTORY_MOVIES + "/DualScreen");
            values.put(MediaStore.Video.Media.IS_PENDING, 1);
            outputUri = getContentResolver().insert(
                    MediaStore.Video.Media.EXTERNAL_CONTENT_URI, values);
            if (outputUri == null) {
                throw new IOException("Android could not create the recording file.");
            }
            outputDescriptor = getContentResolver().openFileDescriptor(outputUri, "w");
            if (outputDescriptor == null) {
                throw new IOException("Android could not open the recording file.");
            }
            muxer = new MediaMuxer(outputDescriptor.getFileDescriptor(),
                    MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4);
        } else {
            File directory = getExternalFilesDir(Environment.DIRECTORY_MOVIES);
            if (directory == null || (!directory.exists() && !directory.mkdirs())) {
                throw new IOException("The recording folder could not be created.");
            }
            legacyOutputFile = new File(directory, name);
            muxer = new MediaMuxer(legacyOutputFile.getAbsolutePath(),
                    MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4);
        }
    }

    private void feedMicrophone() {
        long samplesQueued = 0;
        byte[] microphonePcm = new byte[882];
        byte[] playbackPcm = new byte[882];
        byte[] mixedPcm = new byte[882];
        try {
            while (!stopping.get()) {
                if (paused) {
                    Thread.sleep(25);
                    continue;
                }
                int inputIndex = audioEncoder.dequeueInputBuffer(10_000);
                if (inputIndex < 0) {
                    continue;
                }
                ByteBuffer input = audioEncoder.getInputBuffer(inputIndex);
                if (input == null) {
                    throw new IllegalStateException("The audio encoder stopped unexpectedly.");
                }
                int microphoneBytes = readAudio(audioRecord, microphonePcm);
                int playbackBytes = readAudio(playbackAudioRecord, playbackPcm);
                int sampleCount = Math.max(microphoneBytes, playbackBytes) / 2;
                if (sampleCount == 0) {
                    Thread.sleep(10);
                    audioEncoder.queueInputBuffer(inputIndex, 0, 0,
                            samplesQueued * 1_000_000L / 44_100, 0);
                    continue;
                }
                for (int sample = 0; sample < sampleCount; sample++) {
                    int mixed = 0;
                    if (includeMicrophone && !microphoneMuted && sample * 2 < microphoneBytes) {
                        mixed += (short) ((microphonePcm[sample * 2] & 0xff)
                                | (microphonePcm[sample * 2 + 1] << 8));
                    }
                    if (sample * 2 < playbackBytes) {
                        mixed += (short) ((playbackPcm[sample * 2] & 0xff)
                                | (playbackPcm[sample * 2 + 1] << 8));
                    }
                    short clipped = (short) Math.max(Short.MIN_VALUE,
                            Math.min(Short.MAX_VALUE, mixed));
                    mixedPcm[sample * 2] = (byte) (clipped & 0xff);
                    mixedPcm[sample * 2 + 1] = (byte) ((clipped >> 8) & 0xff);
                }
                input.clear();
                int count = sampleCount * 2;
                if (count > input.remaining()) {
                    throw new IllegalStateException("The audio encoder input buffer is too small.");
                }
                input.put(mixedPcm, 0, count);
                long presentationTimeUs = samplesQueued * 1_000_000L / 44_100;
                samplesQueued += sampleCount;
                audioEncoder.queueInputBuffer(inputIndex, 0, count,
                        presentationTimeUs, 0);
                long frameDurationMs = sampleCount * 1000L / 44_100;
                if (frameDurationMs > 0) {
                    Thread.sleep(frameDurationMs);
                }
            }
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
        } catch (Exception exception) {
            Log.e(TAG, "Microphone capture failed", exception);
            stopping.set(true);
        } finally {
            sendAudioEndOfStream(samplesQueued);
        }
    }

    private int readAudio(AudioRecord recorder, byte[] destination) {
        if (recorder == null) {
            return 0;
        }
        int count = recorder.read(destination, 0, destination.length,
                AudioRecord.READ_NON_BLOCKING);
        if (count < 0) {
            throw new IllegalStateException("Reading audio failed: " + count);
        }
        return count & ~1;
    }

    private void captureFrames() throws Exception {
        MediaCodec.BufferInfo videoInfo = new MediaCodec.BufferInfo();
        MediaCodec.BufferInfo audioInfo = new MediaCodec.BufferInfo();
        int videoTrack = -1;
        int audioTrack = -1;
        boolean muxerStarted = false;
        boolean videoEos = false;
        boolean audioEos = audioEncoder == null;
        boolean videoEosSignaled = false;
        long firstVideoPtsUs = -1;
        long lastVideoPtsUs = -1;
        while (!videoEos || !audioEos) {
            if (stopping.get() && !videoEosSignaled) {
                virtualDisplay.release();
                virtualDisplay = null;
                videoEncoder.signalEndOfInputStream();
                videoEosSignaled = true;
            }
            if (audioEncoder != null && audioThread != null && !audioThread.isAlive()
                    && !audioEos && !stopping.get()) {
                stopping.set(true);
            }

            int videoIndex = videoEncoder.dequeueOutputBuffer(videoInfo, 10_000);
            if (videoIndex == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                videoTrack = muxer.addTrack(videoEncoder.getOutputFormat());
            } else if (videoIndex >= 0) {
                ByteBuffer data = videoEncoder.getOutputBuffer(videoIndex);
                if ((videoInfo.flags & MediaCodec.BUFFER_FLAG_CODEC_CONFIG) != 0) {
                    videoInfo.size = 0;
                }
                if (videoInfo.size > 0) {
                    if (firstVideoPtsUs < 0) {
                        firstVideoPtsUs = videoInfo.presentationTimeUs;
                    }
                    long normalizedPtsUs = Math.max(0,
                            videoInfo.presentationTimeUs - firstVideoPtsUs
                                    - totalPausedMs * 1000L);
                    videoInfo.presentationTimeUs = Math.max(lastVideoPtsUs + 1, normalizedPtsUs);
                    lastVideoPtsUs = videoInfo.presentationTimeUs;
                    if (data != null && muxerStarted) {
                        data.position(videoInfo.offset);
                        data.limit(videoInfo.offset + videoInfo.size);
                        muxer.writeSampleData(videoTrack, data, videoInfo);
                    }
                }
                videoEos = (videoInfo.flags & MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0;
                videoEncoder.releaseOutputBuffer(videoIndex, false);
            }

            if (audioEncoder != null) {
                int audioIndex = audioEncoder.dequeueOutputBuffer(audioInfo, 0);
                if (audioIndex == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                    audioTrack = muxer.addTrack(audioEncoder.getOutputFormat());
                } else if (audioIndex >= 0) {
                    ByteBuffer data = audioEncoder.getOutputBuffer(audioIndex);
                    if ((audioInfo.flags & MediaCodec.BUFFER_FLAG_CODEC_CONFIG) != 0) {
                        audioInfo.size = 0;
                    }
                    if (audioInfo.size > 0 && data != null && muxerStarted) {
                        data.position(audioInfo.offset);
                        data.limit(audioInfo.offset + audioInfo.size);
                        muxer.writeSampleData(audioTrack, data, audioInfo);
                    }
                    audioEos = (audioInfo.flags & MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0;
                    audioEncoder.releaseOutputBuffer(audioIndex, false);
                }
            }

            if (!muxerStarted && videoTrack >= 0
                    && (audioEncoder == null || audioTrack >= 0)) {
                muxer.start();
                muxerStarted = true;
            }
        }
        if (!muxerStarted) {
            throw new IllegalStateException("The video encoder produced no recording.");
        }
    }

    private void finalizeOutput() throws IOException {
        if (muxer != null) {
            muxer.stop();
            muxer.release();
            muxer = null;
        }
        if (outputDescriptor != null) {
            outputDescriptor.close();
            outputDescriptor = null;
        }
        if (Build.VERSION.SDK_INT >= 29 && outputUri != null) {
            ContentValues values = new ContentValues();
            values.put(MediaStore.Video.Media.IS_PENDING, 0);
            getContentResolver().update(outputUri, values, null, null);
        }
    }

    private void releaseRecorder() {
        if (audioThread != null && audioThread.isAlive()) {
            audioThread.interrupt();
            try {
                audioThread.join(1000);
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
            }
        }
        if (noiseSuppressor != null) {
            noiseSuppressor.release();
            noiseSuppressor = null;
        }
        microphoneNoiseReductionActive = false;
        if (audioRecord != null) {
            try {
                audioRecord.stop();
            } catch (IllegalStateException ignored) {
                Log.w(TAG, "Microphone was already stopped.");
            }
            audioRecord.release();
            audioRecord = null;
        }
        if (playbackAudioRecord != null) {
            try {
                playbackAudioRecord.stop();
            } catch (IllegalStateException exception) {
                Log.w(TAG, "App audio capture was already stopped.");
            }
            playbackAudioRecord.release();
            playbackAudioRecord = null;
        }
        releaseCodec(videoEncoder);
        videoEncoder = null;
        if (videoSurface != null) {
            videoSurface.release();
            videoSurface = null;
        }
        releaseCodec(audioEncoder);
        audioEncoder = null;
        if (virtualDisplay != null) {
            virtualDisplay.release();
            virtualDisplay = null;
        }
        if (projection != null) {
            projection.stop();
            projection = null;
        }
        if (muxer != null) {
            try {
                muxer.release();
            } catch (RuntimeException exception) {
                Log.w(TAG, "Could not release the incomplete MP4.", exception);
            }
            muxer = null;
        }
        if (outputDescriptor != null) {
            try {
                outputDescriptor.close();
            } catch (IOException exception) {
                Log.w(TAG, "Could not close the recording file.", exception);
            }
            outputDescriptor = null;
        }
    }

    private void discardOutput() {
        if (outputUri != null) {
            getContentResolver().delete(outputUri, null, null);
            outputUri = null;
        }
        if (legacyOutputFile != null && legacyOutputFile.exists()
                && !legacyOutputFile.delete()) {
            Log.w(TAG, "Could not remove incomplete recording " + legacyOutputFile);
        }
    }

    private void releaseCodec(MediaCodec codec) {
        if (codec == null) {
            return;
        }
        try {
            codec.stop();
        } catch (RuntimeException exception) {
            Log.w(TAG, "Could not stop media encoder.", exception);
        }
        try {
            codec.release();
        } catch (RuntimeException exception) {
            Log.w(TAG, "Could not release media encoder.", exception);
        }
    }

    private void createFloatingControls() {
        mainHandler.post(() -> {
            if (running.get()) {
                addFloatingControls();
            }
        });
    }

    private void addFloatingControls() {
        if (Build.VERSION.SDK_INT >= 23 && !android.provider.Settings.canDrawOverlays(this)) {
            Log.w(TAG, "Overlay permission is unavailable; recording notification controls remain active.");
            return;
        }
        windowManager = (WindowManager) getSystemService(WINDOW_SERVICE);
        floatingControls = new LinearLayout(this);
        floatingControls.setOrientation(LinearLayout.HORIZONTAL);
        floatingControls.setGravity(Gravity.CENTER_VERTICAL);
        floatingControls.setPadding(dp(10), dp(6), dp(10), dp(6));
        floatingControls.setBackgroundColor(Color.rgb(28, 31, 40));
        timerText = new TextView(this);
        timerText.setTextColor(Color.WHITE);
        timerText.setTextSize(14);
        muteButton = controlText("Mic: N/A");
        pauseButton = controlText("Pause");
        TextView stopButton = controlText("Stop");
        floatingControls.addView(timerText);
        floatingControls.addView(muteButton);
        floatingControls.addView(pauseButton);
        floatingControls.addView(stopButton);
        muteButton.setOnClickListener(view -> sendAction(ACTION_MUTE));
        pauseButton.setOnClickListener(view -> sendAction(paused ? ACTION_RESUME : ACTION_PAUSE));
        stopButton.setOnClickListener(view -> sendAction(ACTION_STOP));

        int type = Build.VERSION.SDK_INT >= 26
                ? WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
                : WindowManager.LayoutParams.TYPE_PHONE;
        WindowManager.LayoutParams params = new WindowManager.LayoutParams(
                WindowManager.LayoutParams.WRAP_CONTENT,
                WindowManager.LayoutParams.WRAP_CONTENT,
                type,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                        | WindowManager.LayoutParams.FLAG_SECURE
                        | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
                PixelFormat.TRANSLUCENT);
        params.gravity = Gravity.TOP | Gravity.CENTER_HORIZONTAL;
        params.y = dp(48);
        try {
            windowManager.addView(floatingControls, params);
        } catch (RuntimeException exception) {
            Log.e(TAG, "Could not create floating controls.", exception);
            floatingControls = null;
        }
    }

    private TextView controlText(String label) {
        TextView text = new TextView(this);
        text.setText(label);
        text.setTextColor(Color.WHITE);
        text.setTextSize(14);
        text.setPadding(dp(8), dp(6), dp(8), dp(6));
        return text;
    }

    private void removeFloatingControls() {
        mainHandler.removeCallbacks(controlsTicker);
        mainHandler.post(this::removeFloatingControlsOnMainThread);
    }

    private void removeFloatingControlsOnMainThread() {
        if (windowManager != null && floatingControls != null) {
            try {
                windowManager.removeView(floatingControls);
            } catch (RuntimeException exception) {
                Log.w(TAG, "Could not remove floating controls.", exception);
            }
        }
        floatingControls = null;
    }

    private void refreshControls() {
        mainHandler.removeCallbacks(controlsTicker);
        mainHandler.post(controlsTicker);
    }

    private void updateControls() {
        if (timerText != null) {
            timerText.setText(formatElapsed(elapsedSeconds()));
        }
        if (muteButton != null) {
            muteButton.setText(includeMicrophone
                    ? (microphoneMuted ? "Mute: ON" : "Mute: OFF") : "Mic: N/A");
            muteButton.setTextColor(!includeMicrophone ? Color.LTGRAY
                    : (microphoneMuted ? Color.rgb(255, 93, 104) : Color.rgb(93, 220, 147)));
            muteButton.setEnabled(includeMicrophone);
        }
        if (pauseButton != null) {
            pauseButton.setText(paused ? "Resume" : "Pause");
        }
        NotificationManager manager = getSystemService(NotificationManager.class);
        manager.notify(NOTIFICATION_ID, buildNotification(
                (paused ? "Paused · " : "Recording · ")
                        + formatElapsed(elapsedSeconds()) + " · " + microphoneMuteStatus()));
        sendStatus(null);
    }

    private String microphoneMuteStatus() {
        return includeMicrophone
                ? "Mic mute: " + (microphoneMuted ? "ON" : "OFF")
                : "Mic: unavailable";
    }

    private void sendAudioEndOfStream(long samplesQueued) {
        boolean queued = false;
        while (!queued && audioEncoder != null && running.get()) {
            int inputIndex = audioEncoder.dequeueInputBuffer(100_000);
            if (inputIndex >= 0) {
                audioEncoder.queueInputBuffer(inputIndex, 0, 0,
                        samplesQueued * 1_000_000L / 44_100,
                        MediaCodec.BUFFER_FLAG_END_OF_STREAM);
                queued = true;
            }
        }
    }

    private Notification buildNotification(String status) {
        Intent open = new Intent(this, MainActivity.class);
        PendingIntent openPending = PendingIntent.getActivity(this, 0, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder builder = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CHANNEL_ID)
                : new Notification.Builder(this);
        builder.setSmallIcon(android.R.drawable.presence_video_online)
                .setContentTitle("DualScreen")
                .setContentText(status)
                .setContentIntent(openPending)
                .setOngoing(true)
                .setOnlyAlertOnce(true);
        if (running.get()) {
            builder.addAction(new Notification.Action.Builder(
                    0, paused ? "Resume" : "Pause", actionPending(paused ? ACTION_RESUME : ACTION_PAUSE)).build());
            if (includeMicrophone) {
                builder.addAction(new Notification.Action.Builder(
                        0, microphoneMuted ? "Unmute mic" : "Mute mic", actionPending(ACTION_MUTE)).build());
            }
            builder.addAction(new Notification.Action.Builder(
                    0, "Stop", actionPending(ACTION_STOP)).build());
        }
        return builder.build();
    }

    private PendingIntent actionPending(String action) {
        Intent intent = new Intent(this, RecorderService.class);
        intent.setAction(action);
        return PendingIntent.getService(this, action.hashCode(), intent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    private void sendAction(String action) {
        Intent intent = new Intent(this, RecorderService.class);
        intent.setAction(action);
        startService(intent);
    }

    private void sendStatus(String message) {
        Intent intent = new Intent(ACTION_STATUS);
        intent.setPackage(getPackageName());
        intent.putExtra(EXTRA_RUNNING, running.get());
        intent.putExtra(EXTRA_PAUSED, paused);
        intent.putExtra(EXTRA_MICROPHONE, includeMicrophone);
        intent.putExtra(EXTRA_DEVICE_AUDIO, includeDeviceAudio);
        intent.putExtra(EXTRA_MICROPHONE_MUTED, microphoneMuted);
        intent.putExtra(EXTRA_NOISE_REDUCTION, microphoneNoiseReductionActive);
        if (message != null) {
            intent.putExtra(EXTRA_MESSAGE, message);
        }
        sendBroadcast(intent);
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel channel = new NotificationChannel(
                    CHANNEL_ID, "Recording controls", NotificationManager.IMPORTANCE_LOW);
            channel.setDescription("Status and controls for an active screen recording.");
            getSystemService(NotificationManager.class).createNotificationChannel(channel);
        }
    }

    private int elapsedSeconds() {
        long currentPause = paused ? SystemClock.elapsedRealtime() - pausedAt : 0;
        return (int) Math.max(0,
                (SystemClock.elapsedRealtime() - startedAt - totalPausedMs - currentPause) / 1000);
    }

    private String formatElapsed(int total) {
        return String.format(Locale.US, "%02d:%02d:%02d",
                total / 3600, (total / 60) % 60, total % 60);
    }

    private int resolutionIndex(String value) {
        return "1080p".equals(value) ? 1 : ("720p".equals(value) ? 2 : 0);
    }

    private int qualityIndex(String value) {
        return "High".equals(value) ? 0 : ("Compact".equals(value) ? 2 : 1);
    }

    private int even(int value) {
        return Math.max(2, value & ~1);
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    @Override
    public void onDestroy() {
        if (running.get()) {
            stopping.set(true);
        }
        removeFloatingControls();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
