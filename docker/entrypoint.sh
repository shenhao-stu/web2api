#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${WEB2API_DATA_DIR:-/data}"
CONFIG_PATH="${WEB2API_CONFIG_PATH:-${DATA_DIR}/config.yaml}"
DB_PATH="${WEB2API_DB_PATH:-${DATA_DIR}/db.sqlite3}"
XVFB_ARGS="${XVFB_SCREEN_ARGS:--screen 0 1600x900x24}"
DISPLAY_NUM="${XVFB_DISPLAY_NUM:-99}"
DISPLAY_VALUE=":${DISPLAY_NUM}"

mkdir -p "${DATA_DIR}"

export HOME="${DATA_DIR}"
export WEB2API_CONFIG_PATH="${CONFIG_PATH}"
export WEB2API_DB_PATH="${DB_PATH}"
export PYTHONUNBUFFERED=1

# 清理残留的浏览器 profile，避免 Singleton* 锁导致 Chromium 认为 profile 正在被使用。
rm -rf "${HOME}/fp-data"

if [[ ! -f "${CONFIG_PATH}" ]]; then
  cp /app/docker/config.container.yaml "${CONFIG_PATH}"
fi

mkdir -p "${HOME}/fp-data"

if [[ $# -gt 0 ]]; then
  exec "$@"
fi

cleanup() {
  if [[ -n "${XRAY_PID:-}" ]]; then
    kill "${XRAY_PID}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${XVFB_PID:-}" ]]; then
    kill "${XVFB_PID}" >/dev/null 2>&1 || true
  fi
}

trap cleanup EXIT INT TERM

# ---- Xray proxy (optional) ----
# Set WEB2API_XRAY_CONFIG to a base64-encoded xray JSON config to enable.
XRAY_BIN="/opt/xray/xray"
XRAY_CONFIG="/tmp/xray-config.json"
XRAY_PID=""

if [[ -n "${WEB2API_XRAY_CONFIG:-}" ]] && [[ -x "${XRAY_BIN}" ]]; then
  echo "${WEB2API_XRAY_CONFIG}" | base64 -d > "${XRAY_CONFIG}" 2>/dev/null
  if [[ -s "${XRAY_CONFIG}" ]]; then
    "${XRAY_BIN}" run -c "${XRAY_CONFIG}" &
    XRAY_PID=$!
    # Wait for xray to start listening
    for _ in $(seq 1 30); do
      if command -v ss >/dev/null 2>&1; then
        ss -tlnp 2>/dev/null | grep -q ":10806 " && break
      elif command -v netstat >/dev/null 2>&1; then
        netstat -tlnp 2>/dev/null | grep -q ":10806 " && break
      else
        sleep 0.5 && break
      fi
      sleep 0.3
    done
    echo "Xray proxy started (PID=${XRAY_PID})"
  else
    echo "Warning: WEB2API_XRAY_CONFIG decode failed, skipping xray" >&2
  fi
fi

# ---- Xvfb virtual display ----
mkdir -p /tmp/.X11-unix
rm -f "/tmp/.X${DISPLAY_NUM}-lock"

Xvfb "${DISPLAY_VALUE}" ${XVFB_ARGS} -nolisten tcp -ac &
XVFB_PID=$!

for _ in $(seq 1 100); do
  if [[ -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ]]; then
    break
  fi
  sleep 0.1
done

if [[ ! -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ]]; then
  echo "Xvfb failed to create display ${DISPLAY_VALUE}" >&2
  exit 1
fi

export DISPLAY="${DISPLAY_VALUE}"

exec python -u /app/main.py
