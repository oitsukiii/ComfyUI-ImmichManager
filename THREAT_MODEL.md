# Threat model

Scope: the HTTP routes this package registers plus the node, as shipped. Reviewed
2026-11-01 against `server_routes.py`, `immich_client.py`, `config.py`.

This file exists because reading a file top to bottom and asking "is this safe?"
does not find bugs that live between two files. The table below is the check that
does: every route gets "who can call it", "which secret does it touch", "which
host does it talk to". A row where the answer is *anyone* × *API key* × *a host
the caller chose* is the bug, and it is visible here even when each function looks
fine on its own.

Keep this table current when you add a route. The `Reviewing a change` section at
the bottom says what to check.

## Assets

| Asset | Where it lives | Impact if leaked |
|---|---|---|
| Immich API key | `config.json`, `api_key` | Full read/write on the user's Immich library, including delete |
| Panel token | `config.json`, `panel_token` | Can drive every route from any host on the LAN |
| ComfyUI filesystem | process cwd | The node writes uploads and reads `config.json` |
| Generated media | user's `output/` | Private by assumption; the plugin uploads it to Immich |

## Trust boundaries

```
                 ┌─────────────────────────────┐
 operator  ─────▶│ ComfyUI :8188 (no auth)     │
 (this machine)  │   ├─ /api/immich_plus/*     │──▶ Immich (api_key in header)
                 │   └─ node upload path       │
                 └─────────────────────────────┘
                            ▲
 LAN peer ──────────────────┘   (ComfyUI listens 0.0.0.0 by default)
 reverse proxy ─────────────┘   (request.remote is the proxy's address)
```

Three callers matter:

1. **The operator on this machine** — `request.remote` is loopback. Trusted.
2. **Any other device on the LAN** — reaches the routes whenever ComfyUI is
   listening on `0.0.0.0`, which is the documented way to use it from another
   machine. **Assume hostile until a token proves otherwise.**
3. **A reverse proxy** — `request.remote` is the proxy's internal address, so it
   is *not* loopback. Only a valid token distinguishes it from (2).

ComfyUI itself has no authentication (`comfy/cli_args.py` has no `--auth`), so
**this package's token is the only gate on `/api/immich_plus/*`**. Anything that
is reachable from (2) without a token is reachable by every phone, TV and guest
laptop on the network.

### How the two guards behave

`_check_auth(request, config)`
: Passes loopback when `_TRUST_LOCALHOST`. Otherwise requires
  `Authorization: Bearer <panel_token>`. **If no token is configured it returns
  `True` for everyone** — deliberate (a fresh install should work without
  configuration), and the reason every route that mutates something must also
  call the next guard.

`_sensitive_denied(request, config)`
: For config writes, reset, token management, and anything that mutates the
  user's Immich library. Allows loopback, or a valid token. **Ignores
  `_TRUST_LOCALHOST` on purpose** so that turning local trust off cannot lock the
  operator out of their own config.

## Routes

`key` = the request is made with the Immich API key in an `Authorization` header.
`host` = the destination comes from `config.base_url` (operator-controlled, behind
`_sensitive_denied`).

| Method | Path | Caller allowed | Secret | Host | Notes |
|---|---|---|---|---|---|
| GET | `/config` | token \| loopback | — | — | Returns `api_key_configured` as a bool, never the key |
| PUT | `/config` | **sensitive** | writes key | writes host | The original review finding |
| POST | `/config/reset` | **sensitive** | clears key | resets host | |
| GET | `/panel-token` | **sensitive** | — | — | |
| POST | `/panel-token` | **sensitive** | writes token | — | |
| DELETE | `/panel-token` | **sensitive** | clears token | — | |
| GET | `/health` | token \| loopback | key | config | |
| GET | `/buckets` | token \| loopback | key | config | |
| GET | `/bucket` | token \| loopback | key | config | `timeBucket` → query param (encoded) |
| GET | `/assets/{id}` | token \| loopback | key | config | id validated (F2) |
| GET | `/assets/{id}/thumbnail` | token \| loopback | key | config | id validated (F2) |
| GET | `/assets/{id}/original` | token \| loopback | key | config | id validated (F2) |
| PUT | `/assets` | **sensitive** | key | config | Bulk favorite. Was `token` only (F1) |
| PUT | `/assets/{id}` | **sensitive** | key | config | Was `token` only (F1) |
| DELETE | `/assets` | **sensitive** | key | config | Bulk delete, up to 1000. Was `token` only (F1) |
| GET | `/albums` | token \| loopback | key | config | |
| GET | `/jobs/{id}` | token \| loopback | — | — | Local job records only |

## Findings

### F1 — Destructive routes accepted any LAN caller (fixed)

`PUT /assets`, `PUT /assets/{id}` and `DELETE /assets` called only `_check_auth`.
On a default install (no `panel_token`) that returns `True` for every caller, so
any device on the LAN could bulk-delete up to 1000 assets from the user's Immich
library, or flip favourites on everything. Deletes land in Immich's trash
(`force` defaults to false), so it was recoverable — but it was an
unauthenticated destructive action driven by a stranger.

**Fixed:** all three now call `_sensitive_denied` first. Regression test:
`tests/test_routes.py` section 16.

### F2 — `asset_id` was interpolated into the upstream URL unvalidated (fixed)

`ImmichClient` builds `f"/assets/{asset_id}"`. `requests` normalises `..` in the
path, so

```
asset_id = "abc/../../users"  ->  http://immich/api/users
asset_id = "../admin/users"   ->  http://immich/api/admin/users
```

A caller who could reach the route made the plugin send the operator's API key to
**any Immich endpoint**, including ones this package never exposes. Reaching the
route needed the token, so this was privilege expansion rather than an open door,
but it defeated the point of proxying a narrow subset.

**Fixed:** `_ASSET_ID_RE = ^[A-Za-z0-9_-]{1,64}$`, checked in the four
`{asset_id}` handlers after authentication. Regression test: section 17.

### F3 — No token is the default, and it means "open" (accepted, documented)

A fresh install has `panel_token` empty, so `_check_auth` passes everyone. The
panel only generates a token when the operator opens the config page on the
machine itself. Between install and that moment, every read route above is open
to the LAN.

Accepted because requiring configuration before first use is worse for the common
single-machine case, and because the destructive routes are now behind
`_sensitive_denied`. **If you expose ComfyUI beyond your own LAN, set a token
first.**

### F4 — On Windows the config file is not mode-protected (accepted, documented)

`ConfigManager._save` opens with `0o600`. That is a POSIX feature: on Windows the
mode argument only maps to the read-only flag, and the file inherits the directory
ACL. The API key is therefore protected by directory permissions only.

Accepted for a single-user desktop. Documented in `_save` so nobody assumes
otherwise. Test asserts the mode on POSIX and prints `[SKIP]` elsewhere.

### F5 — `proxy_url` lets the key transit a host the operator types (accepted)

`proxy_mode=custom` + `proxy_url` routes the key-bearing request through
whatever URL is configured. It sits behind `_sensitive_denied`, so only the
operator or a token holder can set it, and it exists because corporate proxies
are real. Note it if you ever loosen that guard.

## Not in scope

- Immich itself. Version, auth and ACLs on that server are its own problem.
- ComfyUI core. It has no authentication, which is the reason this file treats
  "reachable on the LAN" as "reachable by a stranger".
- The `ImmichSaveImage` / `ImmichSaveVideo` nodes. They run inside the graph the
  operator built; they take no untrusted input.

## Reviewing a change

For each new or modified route, answer:

1. **Who can call it?** loopback-only, token, or anyone-when-no-token-is-set?
2. **Which secret does it touch?** If it touches the Immich key, it must be
   behind `_sensitive_denied`, not just `_check_auth`.
3. **Where does the request go?** If the destination is derived from request
   input rather than `config.base_url`, that is an SSRF. Validate it the way
   `_valid_asset_id` does.
4. **Does it write state the user would miss?** Then it needs the sensitive guard.
5. **Does it read request input into a filesystem path or a URL path?** Then it
   needs an allow-list pattern, not a blacklist.

And for the config: if you store another secret, note here **which host it is for**
and refuse to attach it to any other host — that is the structural fix for F-type
bugs, and it is not implemented yet.
