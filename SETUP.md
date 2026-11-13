# Python Environment Setup

Setup for the detection and tracking pipeline. Requires **Python 3.13 (64-bit x64)**.

## Packages

| Package | Version | Used for |
|---------|---------|----------|
| ultralytics | 8.4.174 | YOLO detection + ByteTrack / BoT-SORT tracking |
| torch | 2.14.1 | Model runtime (CPU or CUDA build) |
| torchvision | 0.29.1 | Image ops used by YOLO |
| opencv-python | 5.0.0.93 | Reading/writing video, drawing boxes |
| lap | 0.5.13 | Matching step used by ByteTrack |
| supervision | 0.30.9 | Line counters, lane polygons, annotators |
| numpy | 2.5.3 | Arrays |
| pandas | 3.0.6 | Event logging to `events.csv` |
| huggingface_hub | 2.2.0 | Uploading/downloading the model on Hugging Face |

These are pinned in [requirements.txt](requirements.txt). Other packages (matplotlib, scipy, pillow, etc.) are installed automatically as dependencies.

## Setup Commands

Run from the project root.

**1. Create the virtual environment**

```powershell
# Windows
py -3.13 -m venv .venv
```

**2. Activate it** (do this every time you open a new terminal)

```powershell
# Windows (PowerShell)
.venv\Scripts\Activate.ps1
```

> If PowerShell blocks the script, run once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

**3. Install PyTorch** (pick one)

```bash
# CPU only (no NVIDIA GPU)
pip install torch==2.14.1 torchvision==0.29.1 --index-url https://download.pytorch.org/whl/cpu

# NVIDIA GPU: get the matching CUDA command from https://pytorch.org/get-started/locally/
```

**4. Install everything else**

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

**5. Verify**

```bash
python --version                                   # should print 3.13.x
python -c "import ultralytics; ultralytics.checks()"
```

**Deactivate** when done: `deactivate`