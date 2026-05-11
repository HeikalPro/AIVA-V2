# Zoho OAuth (`zoho_auth`)

Self-contained package for Zoho OAuth in the terminal or from your Python code. Configuration and the default token file live **in this folder** next to the code.

## Layout

| Item | Purpose |
|------|---------|
| `.env` | Your secrets and URLs (create from `.env.example`; do not commit). |
| `.env.example` | Template with all supported variables. |
| `.zoho_login_tokens.json` | Created automatically: saved refresh session (do not commit). |

Override the token path with `ZOHO_TOKEN_STORE_PATH` if you install the package somewhere read-only (for example under `site-packages`).

## Setup

1. From the repository root, install dependencies:

   `pip install -r requirements.txt`

2. Copy the example env file into this folder:

   `copy zoho_auth\.env.example zoho_auth\.env` (Windows) or `cp zoho_auth/.env.example zoho_auth/.env` (Unix).

3. Edit `zoho_auth/.env` with your Zoho API Console values. Required keys:

   - `ZOHO_CLIENT_ID`
   - `ZOHO_CLIENT_SECRET`
   - `ZOHO_REDIRECT_URI` (must match the redirect URL registered in Zoho; often a localhost callback)
   - `ZOHO_AUTH_URL`
   - `ZOHO_TOKEN_URL`
   - `ZOHO_USER_INFO_URL`
   - `ZOHO_SCOPE`

Optional:

- `ZOHO_ALLOWED_EMAIL_DOMAIN` — restrict sign-in to one or more email domains (comma-separated).
- `ZOHO_SESSION_MAX_AGE_DAYS` — days to reuse a saved session before forcing browser login (default: 7).
- `ZOHO_TOKEN_STORE_PATH` — absolute or user-expanded path to the JSON token store file.

`EnvConfigLoader` loads `zoho_auth/.env` by default. Process environment variables still override values after load.

## Run

From the **repository root** (so `zoho_auth` is importable):

- Terminal login: `python main.py` or `python -m zoho_auth`
- Desktop demo: `python demo_ui.py`
- Programmatic example: `python after_login.py`

CLI flags (see `zoho_auth/cli.py`): `--no-browser`, `--no-prompt`, `--force-login`, `--logout`.

## Use as a library

```python
from zoho_auth import ServiceContainer, ZohoUserSession

container = ServiceContainer()
session: ZohoUserSession = container.auth_service.login_or_resume()
```

Custom config: pass `config_loader=EnvConfigLoader(dotenv_path="...")` or a ready-made `config=` into `ServiceContainer`.
