#!/usr/bin/env bash
# torabo-tsuki-lp 自動書き込みスクリプト (macOS)
#
# CIの成果物(uf2)を取得し、1200bps touch でブートローダを起動して自動で書き込む。
# ブートローダ突入は zmk-feature-cdc-acm-bootloader-trigger (Arduino互換 1200bps) を利用。
#
# 使い方:
#   ./flash.sh <artifact-name>        # 例: ./flash.sh torabo_tsuki_lp_right_central
#   ./flash.sh <path/to/firmware.uf2> # ローカルのuf2を直接書き込む
#
# 環境変数:
#   REPO=owner/name   対象リポジトリ (既定: origin から自動取得)
#   RUN=<run-id>      使用するCI run (既定: build.yml の最新成功run)
#   PORT=/dev/cu.xxx  シリアルポート (既定: /dev/cu.usbmodem* が1つなら自動)
#
# 注意: USB接続している側の半分だけが書き込まれます。左右それぞれ挿し替えて実行してください。
set -euo pipefail

ARG="${1:-}"
case "$ARG" in /*) ;; *) if [ -f "$ARG" ]; then ARG="$PWD/$ARG"; fi ;; esac  # 相対uf2パスを絶対化
cd "$(dirname "$0")"   # どこから呼ばれてもリポジトリ基準で動く
[ -z "$ARG" ] && { echo "usage: ./flash.sh <artifact-name | path.uf2>"; exit 1; }

WORKFLOW="build.yml"
if [ -z "${REPO:-}" ]; then
  _url="$(git config --get remote.origin.url)"
  _url="${_url%.git}"
  REPO="$(printf '%s\n' "$_url" | sed -E 's#^.*github\.com[:/]##')"
fi

# --- 1. uf2 を用意 -----------------------------------------------------------
if [[ "$ARG" == *.uf2 && -f "$ARG" ]]; then
  UF2="$ARG"
  echo "==> ローカルuf2を使用: $UF2"
else
  command -v gh >/dev/null || { echo "gh (GitHub CLI) が必要です"; exit 1; }
  RUN="${RUN:-$(gh run list -R "$REPO" --workflow "$WORKFLOW" --status success \
                  --limit 1 --json databaseId --jq '.[0].databaseId')}"
  [ -z "${RUN:-}" ] && { echo "成功したCI runが見つかりません"; exit 1; }
  ARTIFACT="${ARTIFACT:-firmware}"   # ビルド成果物は単一バンドル(firmware)にuf2がまとまっている
  DL="$(mktemp -d)"
  echo "==> バンドル '$ARTIFACT' を run $RUN ($REPO) から取得中..."
  gh run download "$RUN" -R "$REPO" -n "$ARTIFACT" -D "$DL"
  # ARG(例: torabo_tsuki_lp_right_central)に一致するuf2を選択
  UF2="$(find "$DL" -name "*${ARG}*.uf2" | head -1)"
  if [ -z "$UF2" ]; then
    echo "'$ARG' に一致するuf2が見つかりません。候補:"; find "$DL" -name '*.uf2' -exec basename {} \;
    exit 1
  fi
  echo "==> 取得: $UF2"
fi

# --- 2. シリアルポート特定 ---------------------------------------------------
if [ -z "${PORT:-}" ]; then
  PORTS=()
  for p in /dev/cu.usbmodem*; do [ -e "$p" ] && PORTS+=("$p"); done
  case "${#PORTS[@]}" in
    0) echo "シリアルポートが見つかりません。キーボードをUSB接続してください"; exit 1 ;;
    1) PORT="${PORTS[0]}" ;;
    *) echo "複数ポート検出。PORT=... を指定してください:"; printf '  %s\n' "${PORTS[@]}"; exit 1 ;;
  esac
fi
echo "==> ポート: $PORT"

# --- 3. マウント済みボリュームをスナップショット ------------------------------
before="$(ls /Volumes 2>/dev/null || true)"

# --- 4. 1200bps touch でブートローダ起動 -------------------------------------
# ポートを1200bpsで開き、HUPCL付きでクローズしてDTRを落とす = Arduino互換トリガ。
echo "==> 1200bps touch でブートローダ起動..."
python3 - "$PORT" <<'PY' || stty -f "$PORT" 1200 || true
import os, sys, time, termios
port = sys.argv[1]
fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
try:
    a = termios.tcgetattr(fd)          # [iflag,oflag,cflag,lflag,ispeed,ospeed,cc]
    a[4] = termios.B1200               # ispeed
    a[5] = termios.B1200               # ospeed
    a[2] |= termios.HUPCL              # クローズ時にDTRを落とす
    termios.tcsetattr(fd, termios.TCSANOW, a)
    time.sleep(0.3)
finally:
    os.close(fd)                       # DTR drop @1200 -> bootloader
time.sleep(0.2)
PY

# --- 5. UF2ドライブのマウント待ち --------------------------------------------
echo "==> ブートローダドライブのマウント待ち..."
VOL=""
for i in $(seq 1 40); do   # 最大約20秒
  for v in /Volumes/*; do
    [ -e "$v/INFO_UF2.TXT" ] || continue
    case "$before" in *"$(basename "$v")"*) : ;; *) VOL="$v"; break ;; esac
  done
  [ -n "$VOL" ] && break
  # ディスクは居るのにmacOSが自動マウントしないことがある → 自力でマウント
  if [ $((i % 8)) -eq 0 ]; then
    D=$(diskutil list 2>/dev/null | awk '/BLEMICROPRO/ {print $NF; exit}')
    if [ -n "${D:-}" ]; then diskutil mount "$D" >/dev/null 2>&1 || true; fi
  fi
  sleep 0.5
done
[ -z "$VOL" ] && { echo "ブートローダドライブが現れませんでした（手動でダブルリセットを試してください）"; exit 1; }
echo "==> マウント: $VOL"

# --- 6. 書き込み -------------------------------------------------------------
echo "==> 書き込み中..."
cp "$UF2" "$VOL/" || true   # 書き込み完了時にドライブが切断されcpが非0を返すことがある
sync
echo "==> 完了。デバイスが再起動します。"
