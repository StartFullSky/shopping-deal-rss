"""Load a model credential from the environment or a Windows DPAPI secret."""

import os
import pathlib
import subprocess
import tomllib


def load_codex_provider_token(provider_name):
    config_path = pathlib.Path(os.environ.get("CODEX_HOME", pathlib.Path.home() / ".codex")) / "config.toml"
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        raise RuntimeError("codex_config_unavailable") from None
    provider = config.get("model_providers", {}).get(provider_name, {})
    token = provider.get("experimental_bearer_token") or provider.get("api_key")
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError("codex_provider_token_not_configured")
    return token.strip()


def load_api_key(config):
    key = os.environ.get(config.get("api_key_env", "MODEL_API_KEY"), "").strip()
    if key:
        return key
    if config.get("api_key_source") == "codex_config":
        return load_codex_provider_token(config.get("codex_provider", "xiaomi"))
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
