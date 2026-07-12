#!/usr/bin/env bash
# ローカルビルド→書き込み (GitHub Actions不使用、nixエフェメラルシェルでツール調達)
#
# 使い方:
#   ./local.sh                        # 右central(既定)をビルドして書き込み
#   ./local.sh <artifact-name>        # build.yamlのartifact-name指定
#   ./local.sh --build-only [name]    # ビルドのみ(書き込みしない)
#
# 前提(初回セットアップ済み): ~/dev/zmk-toolchain/ 配下に集約
#   ws/                 westワークスペース (west update済み)
#   venv/               python venv (west, protobuf, grpcio-tools, setuptools<81)
#   zephyr-sdk-0.16.9/  ARMツールチェーン
set -euo pipefail

# GUIアプリ(Finder起動)経由だと最小PATHで nix が見えないため、標準の場所を明示的に通す
export PATH="/nix/var/nix/profiles/default/bin:$HOME/.nix-profile/bin:/run/current-system/sw/bin:$PATH"

export ZMK_TOOLCHAIN="${ZMK_TOOLCHAIN:-$HOME/dev/zmk-toolchain}"
export ZMK_WS="$ZMK_TOOLCHAIN/ws"
export ZMK_VENV="$ZMK_TOOLCHAIN/venv"
export ZEPHYR_BASE="$ZMK_WS/zephyr"
export ZEPHYR_SDK_INSTALL_DIR="$ZMK_TOOLCHAIN/zephyr-sdk-0.16.9"
export ZMK_REPO="$(cd "$(dirname "$0")" && pwd)"

BUILD_ONLY=0
[ "${1:-}" = "--build-only" ] && { BUILD_ONLY=1; shift; }
export ART="${1:-torabo_tsuki_lp_right_central}"

# build.yaml から artifact-name に対応する shield/snippet を取得
# (ヒアドキュメントを $() 内に置くと bash 3.2 でパース不能なため別ファイルに分離)
cd "$ZMK_REPO"
eval "$("$ZMK_VENV/bin/python3" "$ZMK_REPO/tools/build_query.py" "$ART" "$ZMK_REPO/build.yaml")"
echo "==> build: $ART (shield=$SHIELD, snippets=${SNIPPET:-なし})"

cd "$ZMK_WS"
LOG="$(mktemp -t zmk-build)"
if ! nix shell nixpkgs#cmake nixpkgs#ninja nixpkgs#dtc -c sh -c '
  export PATH="$ZMK_VENV/bin:$PATH"
  if [ -n "$SNIPPET" ]; then set -- -S "$SNIPPET"; else set --; fi
  exec west build -s zmk/app -d "build/$ART" -b bmp_boost "$@" -- \
    -DZephyr_DIR="$ZEPHYR_BASE/share/zephyr-package/cmake" \
    -DZMK_CONFIG="$ZMK_REPO/config" -DSHIELD="$SHIELD" -DZMK_EXTRA_MODULES="$ZMK_REPO"
' > "$LOG" 2>&1; then
  echo "==> ビルド失敗。エラー抜粋:"
  _exc="$(grep -B2 -A8 -iE "error|FAILED|command not found|No such file" "$LOG" | head -40)"
  if [ -n "$_exc" ]; then echo "$_exc"; else tail -25 "$LOG"; fi   # 抜粋が空でもログ末尾を出す
  echo "==> フルログ: $LOG"; exit 1
fi
grep -E "Wrote .* zmk.uf2|region.*used" "$LOG" | tail -1

UF2="$ZMK_WS/build/$ART/zephyr/zmk.uf2"
[ -f "$UF2" ] || { echo "==> uf2が見つかりません。フルログ: $LOG"; exit 1; }
echo "==> ビルド完了: $UF2"

[ "$BUILD_ONLY" -eq 1 ] && exit 0
exec "$ZMK_REPO/flash.sh" "$UF2"
