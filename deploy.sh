#!/usr/bin/env bash
# ビルド→書き込みの全自動化: 最新masterコミットのCIビルドを待ち、完成したuf2を書き込む。
#
# 使い方:
#   ./deploy.sh                      # 右central(既定)を最新ビルドで書き込み
#   ./deploy.sh <artifact-name>      # 例: ./deploy.sh torabo_tsuki_lp_left_peripheral
#   ./deploy.sh --watch              # 常駐モード: masterに新コミットが来るたびに ビルド待ち→書き込み
#                                    # (keymap-editorで保存→放っておくと実機に反映される)
#
# 環境変数: PORT=/dev/cu.xxx でポート指定(flash.shへ引き継ぎ)
set -euo pipefail

ART_DEFAULT="torabo_tsuki_lp_right_central"
WATCH=0
[ "${1:-}" = "--watch" ] && { WATCH=1; shift; }
ART="${1:-$ART_DEFAULT}"

cd "$(dirname "$0")"
command -v gh >/dev/null || { echo "gh (GitHub CLI) が必要です"; exit 1; }

_url="$(git config --get remote.origin.url)"; _url="${_url%.git}"
REPO="$(printf '%s\n' "$_url" | sed -E 's#^.*github\.com[:/]##')"

# 指定SHAのビルドrunを待つ (run出現→完了まで)。成功したらrun idをechoする
wait_for_build() {
  local sha="$1" rid="" st="" con=""
  echo "==> コミット ${sha:0:7} のCIビルドを待機中..." >&2
  for _ in $(seq 1 30); do   # run出現待ち 最大約1分
    rid=$(gh api "repos/$REPO/actions/runs?branch=master&head_sha=$sha&per_page=1" \
            --jq '.workflow_runs[0].id // empty')
    [ -n "$rid" ] && break
    sleep 2
  done
  [ -z "$rid" ] && { echo "CIビルドが起動しません (run未検出)" >&2; return 1; }
  echo "==> run $rid を監視中 (通常4-6分)..." >&2
  while :; do
    read -r st con < <(gh api "repos/$REPO/actions/runs/$rid" --jq '"\(.status) \(.conclusion // "-")"')
    [ "$st" = "completed" ] && break
    sleep 15
  done
  if [ "$con" != "success" ]; then
    echo "ビルド失敗 ($con): https://github.com/$REPO/actions/runs/$rid" >&2
    return 1
  fi
  echo "==> ビルド成功" >&2
  echo "$rid"
}

# キーボード接続とポート解放を待つ
wait_for_port() {
  local warned=0 p=""
  while :; do
    p=$(ls /dev/cu.usbmodem* 2>/dev/null | head -1)
    if [ -z "$p" ]; then
      [ $warned -eq 0 ] && { echo "==> キーボードのUSB接続を待っています..."; warned=1; }
    elif lsof "$p" >/dev/null 2>&1; then
      [ $warned -lt 2 ] && { echo "==> ポートが使用中です (ZMK Studio/keymap-editorを切断してください)..."; warned=2; }
    else
      return 0
    fi
    sleep 3
  done
}

deploy_once() {
  local sha rid
  sha=$(gh api "repos/$REPO/commits/master" --jq .sha)
  echo "==> 対象: $ART @ ${sha:0:7} ($(gh api "repos/$REPO/commits/master" --jq '.commit.message' | head -1))"
  rid=$(wait_for_build "$sha") || return 1
  wait_for_port
  RUN="$rid" ./flash.sh "$ART"
  echo "==> デプロイ完了: ${sha:0:7} → $ART"
  LAST_SHA="$sha"
}

if [ "$WATCH" -eq 0 ]; then
  deploy_once
else
  echo "==> 常駐モード開始: masterの新コミットを監視します (Ctrl+Cで終了)"
  LAST_SHA=$(gh api "repos/$REPO/commits/master" --jq .sha)
  echo "==> 現在のHEAD ${LAST_SHA:0:7} はスキップし、次のコミットから反映します"
  while :; do
    sleep 20
    sha=$(gh api "repos/$REPO/commits/master" --jq .sha 2>/dev/null) || continue
    if [ "$sha" != "$LAST_SHA" ]; then
      echo ""
      echo "==> 新しいコミット検出: ${sha:0:7}"
      deploy_once || echo "==> デプロイ失敗。次のコミットを待ちます"
      echo "==> 監視を継続します..."
    fi
  done
fi
