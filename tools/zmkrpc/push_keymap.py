"""キーマップ変更をZMK Studio RPCで実機に即時反映する (ビルド不要)

使い方:
  python3 push_keymap.py status                 # 実機の状態確認(挙動一覧/座標空間)
  python3 push_keymap.py set 0 33 "&kp K"       # layer0 の L位置33 を設定 (確認後saveする)
  python3 push_keymap.py apply < summary.txt    # GUIの変更サマリを一括反映
  python3 push_keymap.py save                   # flashに保存
  echoオプション: --no-save (setの自動保存を抑止)

位置番号は keymap.keymap と同じ L(66キー) 基準で指定する。
実機がSレイアウトで動作している場合(bindings=44個)は自動でS位置へ変換する。
"""
import os, re, subprocess, sys, tempfile

sys.path.insert(0, os.path.dirname(__file__))
from client import StudioClient  # noqa: E402
import studio_pb2  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TOOLCHAIN = os.environ.get("ZMK_TOOLCHAIN") or os.path.expanduser("~/dev/zmk-toolchain")
ZMK_INC = os.path.join(TOOLCHAIN, "ws/zmk/app/include")
ZEPHYR_INC = os.path.join(TOOLCHAIN, "ws/zephyr/include")

# devicetree参照名 → ZMK挙動のdisplay_name (実機のlistAllBehaviorsと突き合わせる)
BEH_NAMES = {
    "kp": ["Key Press"],
    "mt": ["Mod-Tap"],
    "lt": ["Layer-Tap", "Layer Tap"],
    "mo": ["Momentary Layer"],
    "tog": ["Toggle Layer"],
    "sl": ["Sticky Layer"],
    "sk": ["Sticky Key"],
    "mkp": ["Mouse Button Press", "Mouse Key Press"],
    "msc": ["Mouse Scroll"],
    "mmv": ["Mouse Move"],
    "bt": ["Bluetooth"],
    "out": ["Output Selection"],
    "trans": ["Transparent"],
    "none": ["None"],
    "bootloader": ["Bootloader"],
    "sys_reset": ["Reset"],
    "caps_word": ["Caps Word"],
    "key_repeat": ["Key Repeat"],
    "gresc": ["Grave / Escape", "Grave/Escape"],
}


def resolve_params(tokens):
    """ZMKヘッダのマクロ(A, LS(N1), BT_NXT, LCLK等)をclang -Eで数値化する"""
    vals = {}
    todo = []
    for t in tokens:
        t = t.strip()
        if re.fullmatch(r"-?\d+", t):
            vals[t] = int(t)
        else:
            todo.append(t)
    if todo:
        src = (
            "#include <dt-bindings/zmk/keys.h>\n"
            "#include <dt-bindings/zmk/bt.h>\n"
            "#include <dt-bindings/zmk/outputs.h>\n"
            "#include <dt-bindings/zmk/pointing.h>\n"
        )
        for i, t in enumerate(todo):
            src += f"@@{i}@@ ({t})\n"
        r = subprocess.run(
            ["clang", "-E", "-P", "-x", "c", "-", "-I", ZMK_INC, "-I", ZEPHYR_INC],
            input=src, capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"プリプロセス失敗: {r.stderr[:400]}")
        for line in r.stdout.splitlines():
            m = re.match(r"@@(\d+)@@ (.+)", line.strip())
            if m:
                expr = m.group(2)
                try:
                    vals[todo[int(m.group(1))]] = int(eval(expr, {"__builtins__": {}}))  # noqa: S307
                except Exception as e:
                    raise RuntimeError(f"評価失敗 '{todo[int(m.group(1))]}' → {expr}: {e}")
    missing = [t for t in tokens if t.strip() not in vals]
    if missing:
        raise RuntimeError(f"未解決トークン: {missing}")
    return vals


def load_position_map_s():
    """L位置i → S位置 (>43はSに存在しない)"""
    dtsi = open(os.path.join(REPO, "boards/shields/torabo_tsuki_lp/torabo_tsuki_lp_layouts.dtsi")).read()
    pm = re.search(r"position_map_s_1\s*\{.*?positions\s*=\s*<(.*?)>;", dtsi, re.S).group(1)
    return [int(t) for t in pm.split()]


class Pusher:
    def __init__(self):
        self.c = StudioClient()
        self.keymap = self._get_keymap()
        self.behs = self._get_behaviors()
        self.l2dev, self.space = self._detect_mapping()

    def _detect_mapping(self):
        """layer0の指紋照合でL→デバイス位置マッピングを実測判定する。
        実機はアクティブ物理レイアウトに応じてキーマップ配列が置換されている
        (例: S動作時は66スロットのままS並びに置換。J=L33がdev16に居る)。"""
        n = len(self.keymap.layers[0].bindings)
        if n != 66:
            raise RuntimeError(f"想定外のbindings数: {n}")
        # 期待値: config/keymap.keymap の layer_0 (&kp 単独パラメータのみ照合)
        src = open(os.path.join(REPO, "config/keymap.keymap")).read()
        body = re.search(r"layer_0\s*\{\s*bindings\s*=\s*<(.*?)>;", src, re.S).group(1)
        toks = [re.sub(r"\s+", " ", t.strip()) for t in re.findall(r"&\S+(?:\s+[^&\s][^&]*?)?(?=\s*&|\s*$)", body.strip())]
        kp_id = self.behavior_id("kp")
        expected = {}
        simple = [(i, t.split()[1]) for i, t in enumerate(toks) if t.startswith("&kp ") and len(t.split()) == 2 and "(" not in t]
        vals = resolve_params([p for _, p in simple])
        for i, param in simple:
            expected[i] = vals[param]
        dev = self.keymap.layers[0].bindings
        cands = {"L(そのまま)": list(range(66)), "S置換": load_position_map_s()}
        scores = {}
        for name, m in cands.items():
            scores[name] = sum(1 for i, usage in expected.items()
                               if dev[m[i]].behavior_id == kp_id and dev[m[i]].param1 == usage)
        best = max(scores, key=scores.get)
        total = len(expected)
        if scores[best] < total * 0.9 or len([s for s in scores.values() if s == scores[best]]) > 1:
            raise RuntimeError(f"位置マッピングを特定できません: {scores} (母数{total})")
        return cands[best], f"{best} (照合 {scores[best]}/{total})"

    def _req(self):
        return studio_pb2.Request()

    def _get_keymap(self):
        r = self._req(); r.keymap.get_keymap = True
        return self.c.call(r).keymap.get_keymap

    def _get_behaviors(self):
        r = self._req(); r.behaviors.list_all_behaviors = True
        ids = self.c.call(r).behaviors.list_all_behaviors.behaviors
        out = {}
        for bid in ids:
            r = self._req(); r.behaviors.get_behavior_details.behavior_id = bid
            d = self.c.call(r).behaviors.get_behavior_details
            out[d.display_name] = d.id
        return out

    def behavior_id(self, ref):
        for name in BEH_NAMES.get(ref, []):
            if name in self.behs:
                return self.behs[name]
        raise RuntimeError(f"&{ref} に対応する挙動が実機にありません。実機の挙動一覧: {list(self.behs)}")

    def set_binding(self, layer_idx, l_pos, binding, verify=True):
        dev_pos = self.l2dev[l_pos]
        if "S置換" in self.space and dev_pos > 43:
            print(f"  ℹ L位置{l_pos}はSレイアウトに物理キーが無い(退避スロットdev{dev_pos}に書き込みは行う)")
        toks = binding.strip().split()
        ref = toks[0].lstrip("&")
        params = toks[1:]
        vals = resolve_params(params) if params else {}
        p = [vals[t.strip()] for t in params] + [0, 0]
        layer = self.keymap.layers[layer_idx]
        r = self._req()
        sb = r.keymap.set_layer_binding
        sb.layer_id = layer.id
        sb.key_position = dev_pos
        sb.binding.behavior_id = self.behavior_id(ref)
        sb.binding.param1 = p[0] & 0xFFFFFFFF
        sb.binding.param2 = p[1] & 0xFFFFFFFF
        resp = self.c.call(r).keymap.set_layer_binding
        ok = resp == 0  # SET_LAYER_BINDING_RESP_OK
        status = "OK" if ok else f"エラー({studio_pb2.keymap__pb2.SetLayerBindingResponse.Name(resp) if hasattr(studio_pb2, 'keymap__pb2') else resp})"
        print(f"  layer{layer_idx}(id={layer.id}) L{l_pos}→dev{dev_pos}: {binding}  ... {status}")
        if ok and verify:
            km = self._get_keymap()
            b = km.layers[layer_idx].bindings[dev_pos]
            exp_id = self.behavior_id(ref)
            if b.behavior_id != exp_id or b.param1 != (p[0] & 0xFFFFFFFF) or b.param2 != (p[1] & 0xFFFFFFFF):
                print(f"  ⚠ 読み戻し不一致: id={b.behavior_id} p1={b.param1:#x} p2={b.param2:#x}")
                return False
            self.keymap = km
        return ok

    def save(self):
        r = self._req(); r.keymap.save_changes = True
        resp = self.c.call(r).keymap.save_changes
        ok = resp.WhichOneof("result") == "ok"
        print("save:", "OK (flashに保存)" if ok else f"失敗 err={resp.err}")
        return ok

    def status(self):
        r = self._req(); r.core.get_device_info = True
        info = self.c.call(r).core.get_device_info
        print(f"device : {info.name}")
        print(f"座標空間: {self.space}")
        for i, l in enumerate(self.keymap.layers):
            print(f"layer[{i}] id={l.id} name={l.name!r} bindings={len(l.bindings)}")
        print("挙動一覧:", ", ".join(f"{k}(id={v})" for k, v in sorted(self.behs.items(), key=lambda x: x[1])))


SUMMARY_RE = re.compile(r"layer_(\d+)\s+pos(\d+):\s*(.+?)\s*(?:→|->)\s*(.+)")


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__); return 1
    cmd = args[0]
    p = Pusher()
    try:
        if cmd == "status":
            p.status()
        elif cmd == "set":
            no_save = "--no-save" in args
            args = [a for a in args if a != "--no-save"]
            ok = p.set_binding(int(args[1]), int(args[2]), args[3])
            if ok and not no_save:
                p.save()
        elif cmd == "apply":
            changes = []
            for line in sys.stdin:
                m = SUMMARY_RE.search(line)
                if m:
                    changes.append((int(m.group(1)), int(m.group(2)), m.group(4).strip()))
            if not changes:
                print("変更サマリが読み取れません"); return 1
            print(f"{len(changes)}件を反映します:")
            results = [p.set_binding(l, pos, b) for l, pos, b in changes]
            if any(results):
                p.save()
            print(f"完了: {sum(results)}/{len(results)}件反映")
        elif cmd == "save":
            p.save()
        else:
            print(__doc__); return 1
    finally:
        p.c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
