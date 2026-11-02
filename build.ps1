$ErrorActionPreference = "Stop"

python -m pip install -r requirements.txt
python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name DualScreen-1.1.2 `
    --collect-all mss `
    --collect-all cv2 `
    --collect-all soundcard `
    --collect-all imageio_ffmpeg `
    --collect-all cffi `
    main.py

Write-Host "Built dist\DualScreen-1.1.2.exe"
