@echo off
REM GPU Training Quick Start for Windows
REM Usage: train-windows.bat [symbols] [timesteps] [num-seeds]
REM Example: train-windows.bat "SOFI,NVDA,MED,GCTS,AVXL,EBS,PRTA" 500000 3

set SYMBOLS=%~1
set TIMESTEPS=%~2
set NUM_SEEDS=%~3

if "%SYMBOLS%"=="" set SYMBOLS=SOFI,NVDA,MED,GCTS,AVXL,EBS,PRTA
if "%TIMESTEPS%"=="" set TIMESTEPS=500000
if "%NUM_SEEDS%"=="" set NUM_SEEDS=3

echo ============================================
echo GPU Training - Windows (RTX 5070)
echo ============================================
echo Symbols: %SYMBOLS%
echo Timesteps: %TIMESTEPS%
echo Number of models: %NUM_SEEDS%
echo ============================================
echo.

REM Check if venv exists
if not exist ".venv\Scripts\activate.ps1" (
    echo Creating virtual environment...
    python -m venv .venv
)

REM Activate venv
call .venv\Scripts\activate.bat

REM Install dependencies
echo Installing dependencies...
python -m pip install torch --index-url https://download.pytorch.org/whl/cu121
python -m pip install -r requirements-gpu.txt
python -m pip install -e .

REM Check GPU
echo Checking GPU...
python -c "import torch; print('CUDA:', torch.cuda.is_available()); print('GPU:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none')"

echo.
echo Starting training...
python scripts/train_rl_gpu.py --symbols %SYMBOLS% --timesteps %TIMESTEPS% --num-seeds %NUM_SEEDS% --verbose 2

echo.
echo Training complete. Check state/rl_logs/ for results.
