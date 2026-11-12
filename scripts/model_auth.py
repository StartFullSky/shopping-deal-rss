"""Load a model credential from the environment or a Windows DPAPI secret."""

import os
import pathlib
import subprocess


def load_api_key(config):
    key = os.environ.get(config.get("api_key_env", "AGNES_API_KEY"), "").strip()
    if key:
        return key
    path = pathlib.Path(os.path.expandvars(config.get("api_key_secret_path", ""))).expanduser()
    if not config.get("api_key_secret_path") or not path.is_file():
        raise RuntimeError("api_key_not_configured")
    if os.name != "nt":
        raise RuntimeError("dpapi_secret_requires_windows_use_api_key_env")
    env = {**os.environ, "SHOPPING_MODEL_SECRET_PATH": str(path)}
    command = "$ErrorActionPreference='Stop'; $encoded=(Get-Content -LiteralPath $env:SHOPPING_MODEL_SECRET_PATH -Raw).Trim(); $s=ConvertTo-SecureString $encoded; [Console]::Out.Write(([PSCredential]::new('model',$s)).GetNetworkCredential().Password)"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, text=True, timeout=15, env=env)
    if result.returncode or not result.stdout.strip():
        raise RuntimeError("model_secret_decryption_failed")
    return result.stdout.strip()
