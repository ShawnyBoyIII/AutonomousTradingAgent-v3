# GPU Training Setup — Windows PC (RTX 5070)

## 1. Install Python
- Download Python 3.11 or 3.12 from python.org (NOT 3.14 — wheel issues on Windows)
- Check "Add Python to PATH" during install

## 2. Clone the repo
```powershell
cd C:\Users\YourName\Documents
git clone <repo-url> AutonomousTradingAgentcopy
cd AutonomousTradingAgentcopy
```

## 3. Create virtual environment
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

## 4. Install GPU dependencies
```powershell
pip install torch --index-url https://download.pytorch.org/whl/cu121
pip install -r requirements-gpu.txt
```

Verify GPU is detected:
```powershell
python -c "import torch; print('CUDA:', torch.cuda.is_available()); print('GPU:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none')"
```
Expected output: `CUDA: True` and `GPU: NVIDIA GeForce RTX 5070`

## 5. Install project package
```powershell
pip install -e .
```

## 6. Copy training data
Copy `state/universe.txt` from Mac to Windows (same path):
```
C:\Users\YourName\Documents\AutonomousTradingAgentcopy\state\universe.txt
```

## 7. Run training
```powershell
python scripts/train_rl_gpu.py --symbols SOFI,NVDA,MED,GCTS,AVXL,EBS,PRTA --timesteps 500000 --num-seeds 3 --verbose 2
```

## Expected speeds on RTX 5070
- 50K steps: ~30-60 seconds
- 500K steps: ~5-10 minutes per model
- 1M steps: ~10-20 minutes per model

## 8. After training
Copy the trained models back to Mac:
```
state/rl_logs/gpu_run_* -> Mac repo state/rl_logs/
```

Update `burn-in-config.yaml` `model_paths` to point to the new models.

## Troubleshooting

**"CUDA out of memory"** — Reduce `--batch-size` to 128 or `--n-steps` to 256

**"No module named 'stable_baselines3'"** — Make sure venv is activated: `.venv\Scripts\Activate.ps1`

**"CUDA not available"** — Verify you installed the cu121 version of torch, not the CPU-only version

**Slow training** — Check that GPU is actually being used:
```powershell
python -c "import torch; print(torch.cuda.is_available())"
```
Should print `True`. If `False`, you installed the CPU-only version.
