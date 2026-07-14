"""キーマップGUIのローカルサーバ

  GET  /             GUI (gui.html)
  GET  /keymap       現在のkeymap (config/keymap.keymapをパース) + Sレイアウト座標
  POST /apply        キーマップ変更を実機へRPC反映 + keymap.keymapへ書き込み (ビルド不要)
  GET  /extras       コンボ + 細かな設定 (ファーム側設定) の現在値
  POST /extras       コンボ/設定をソースへ書き込み (反映にはビルドが必要)
  POST /build        {"artifact": name} をビルドして書き込み (local.sh、バックグラウンド)
  GET  /build-status 進行中ビルドのログと状態
  POST /quit         サーバ終了
"""
import json, os, re, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "zmkrpc"))
from push_keymap import Pusher, load_position_map_s  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
KEYMAP = os.path.join(REPO, "config/keymap.keymap")
LAYOUTS = os.path.join(REPO, "boards/shields/torabo_tsuki_lp/torabo_tsuki_lp_layouts.dtsi")
OVERLAYS = [os.path.join(REPO, f"boards/shields/torabo_tsuki_lp/torabo_tsuki_lp_{s}.overlay") for s in ("right", "left")]
CONFS = [os.path.join(REPO, f"boards/shields/torabo_tsuki_lp/torabo_tsuki_lp_{s}.conf") for s in ("right", "left")]
TRACKBALL_OVERLAY = os.path.join(REPO, "snippets/input-trackball/input-trackball.overlay")
ROWS = [12, 12, 14, 14, 14]
PORT = 8756
NAMES_FILE = os.path.join(os.path.dirname(__file__), "layer-names.json")


def load_names(n=None):
    """レイヤー名リストを返す。ファイルが無い/長さ不一致なら補完する。"""
    try:
        names = json.load(open(NAMES_FILE))
    except Exception:
        names = []
    if n is None:
        n = len(parse_layers())
    while len(names) < n:
        names.append(f"layer_{len(names)}")
    return names[:n]


def save_names(names):
    json.dump(names, open(NAMES_FILE, "w"), ensure_ascii=False)
# PAW3222のハード分解能は38刻み(608〜4826)だが、GUIは分かりやすく50刻みで扱う。
# 設定ファイルには50の倍数を書き、ドライバが内部で38に丸める(差は体感ゼロ)。
# 範囲はハード有効域に収まる50の倍数 650〜4800 に制限。未設定時はセンサー既定(約800cpi)
CPI_STEP, CPI_MIN, CPI_MAX, CPI_DEFAULT = 50, 650, 4800, 800

BIND_RE = re.compile(r"&\S+(?:\s+[^&\s][^&]*?)?(?=\s*&|\s*$)")

# ===== keymap =====

def parse_layers():
    src = open(KEYMAP).read()
    layers = []
    for m in re.finditer(r"layer_(\d+)\s*\{\s*bindings\s*=\s*<(.*?)>;", src, re.S):
        toks = [re.sub(r"\s+", " ", t.strip()) for t in BIND_RE.findall(m.group(2).strip())]
        assert len(toks) == 66, f"layer{m.group(1)}: {len(toks)}"
        layers.append(toks)
    return layers


def geometry():
    dtsi = open(LAYOUTS).read()
    s_block = re.search(r"physical_layout_s:.*?keys.*?=(.*?);", dtsi, re.S).group(1)
    keys = []
    for m in re.finditer(r"<&key_physical_attrs\s+\d+\s+\d+\s+(\d+)\s+(\d+)\s+(\(?-?\d+\)?)\s+(\d+)\s+(\d+)>", s_block):
        x, y, rot, rx, ry = m.groups()
        keys.append(dict(x=int(x) / 100, y=int(y) / 100, r=int(rot.strip("()")) / 100, rx=int(rx) / 100, ry=int(ry) / 100))
    map_s = load_position_map_s()
    geom, parked = [], 0
    for i in range(66):
        s = map_s[i]
        if s <= 43:
            k = keys[s]
            g = {"i": i, "x": k["x"], "y": k["y"], "onS": True}
            if k["r"]:
                g.update(r=k["r"], rx=k["rx"], ry=k["ry"])
        else:
            g = {"i": i, "x": (parked % 11) * 1.05, "y": 6.8 + (parked // 11) * 1.05, "onS": False}
            parked += 1
        geom.append(g)
    return geom


def write_keymap(layers, only_layers=None):
    src = open(KEYMAP).read()
    for li, lay in enumerate(layers):
        if only_layers is not None and li not in only_layers:
            continue
        rows, p = [], 0
        for n in ROWS:
            rows.append("  " + "  ".join(lay[p:p + n]))
            p += n
        block = "\n".join(rows)
        src = re.sub(
            rf"(layer_{li}\s*\{{\s*bindings\s*=\s*<)(.*?)(>;)",
            lambda m: m.group(1) + "\n" + block + "\n            " + m.group(3),
            src, flags=re.S,
        )
    open(KEYMAP, "w").write(src)

def add_layer():
    """keymap.keymapの末尾に全&transの新レイヤーを追記し、新しいレイヤー数を返す"""
    layers = parse_layers()
    n = len(layers)
    if n >= 10:
        raise RuntimeError("レイヤーは最大10までにしています")
    lay = ["&trans"] * 66
    rows, p = [], 0
    for cnt in ROWS:
        rows.append("  " + "  ".join(lay[p:p + cnt]))
        p += cnt
    block = f"\n\n        layer_{n} {{\n            bindings = <\n" + "\n".join(rows) + "\n            >;\n        };"
    src = open(KEYMAP).read()
    last = list(re.finditer(r"layer_\d+\s*\{.*?\};", src, re.S))[-1]
    src = src[:last.end()] + block + src[last.end():]
    open(KEYMAP, "w").write(src)
    return n + 1


def _remap_binding(b, m):
    """バインディング文字列中のレイヤー番号参照(&lt/&mo/&to/&tog/&sl)を m(old->new)で置換"""
    for beh in ("mo", "to", "tog", "sl"):
        b = re.sub(rf"&{beh} (\d+)", lambda x: f"&{beh} {m[int(x.group(1))]}", b)
    b = re.sub(r"&lt (\d+)", lambda x: f"&lt {m[int(x.group(1))]}", b)
    return b


def apply_layer_permutation(old_to_new):
    """レイヤーを並べ替え、全参照を自動更新する再利用関数。
    old_to_new: {旧index: 新index} の全単射。
    更新対象: 各層の中身/順序, 層内の&lt/&mo/&to/&tog/&sl, combosのlayers,
    overlayの #define AUTO_MOUSE_LAYER と scroller の layers。"""
    layers = parse_layers()
    n = len(layers)
    assert sorted(old_to_new) == list(range(n)) and sorted(old_to_new.values()) == list(range(n)), \
        f"全単射ではありません: {old_to_new}"
    new_layers = [None] * n
    for old, new in old_to_new.items():
        new_layers[new] = [_remap_binding(b, old_to_new) for b in layers[old]]
    write_keymap(new_layers)
    # combos の layers = <...>
    src = open(KEYMAP).read()
    src = re.sub(r"(layers = <)([\d ]+)(>)",
                 lambda x: x.group(1) + " ".join(str(old_to_new[int(v)]) for v in x.group(2).split()) + x.group(3),
                 src)
    open(KEYMAP, "w").write(src)
    # overlays
    for path in OVERLAYS:
        s = open(path).read()
        s = re.sub(r"(#define AUTO_MOUSE_LAYER )(\d+)",
                   lambda x: x.group(1) + str(old_to_new[int(x.group(2))]), s)
        s = re.sub(r"(layers = <)(\d+)(>)",
                   lambda x: x.group(1) + str(old_to_new[int(x.group(2))]) + x.group(3), s)
        open(path, "w").write(s)
    # レイヤー名も同じ並びに
    names = load_names(n)
    new_names = [None] * n
    for old, new in old_to_new.items():
        new_names[new] = names[old]
    save_names(new_names)


def insert_layer(pos, name="追加レイヤー"):
    """位置posに空レイヤーを挿入(pos以降は後ろにずれる)。参照も名前も自動更新。"""
    n = len(parse_layers())
    if not (1 <= pos <= n):
        raise RuntimeError(f"挿入位置が不正: {pos} (1〜{n})")
    add_layer()                      # 末尾(index n)に空レイヤー追加
    names = load_names(n)
    names.append(name)               # 名前も末尾に(index n)
    save_names(names)
    # 末尾の新レイヤーをposへ移動、pos以降を+1
    perm = {i: (i if i < pos else i + 1) for i in range(n)}
    perm[n] = pos
    apply_layer_permutation(perm)
    return n + 1


# ===== コンボ =====

def parse_combos():
    src = open(KEYMAP).read()
    block = re.search(r"combos\s*\{(.*?)\n    \};", src, re.S)
    combos = []
    if block:
        for m in re.finditer(r"(\w+)\s*\{([^{}]*?)\}\s*;", block.group(1)):
            name, body = m.group(1), m.group(2)
            if name == "compatible":
                continue
            def grab(prop):
                mm = re.search(rf"{prop}\s*=\s*<(.*?)>;", body, re.S)
                return mm.group(1).strip() if mm else None
            c = {"name": name, "binding": re.sub(r"\s+", " ", (grab("bindings") or "").strip()),
                 "positions": [int(x) for x in (grab("key-positions") or "").split()],
                 "layers": [int(x) for x in grab("layers").split()] if grab("layers") else [],
                 "timeout": int(grab("timeout-ms")) if grab("timeout-ms") else None}
            combos.append(c)
    return combos


def write_combos(combos):
    src = open(KEYMAP).read()
    inner = ""
    for c in combos:
        inner += f"\n        {c['name']} {{\n"
        inner += f"            bindings = <{c['binding']}>;\n"
        inner += f"            key-positions = <{' '.join(str(p) for p in c['positions'])}>;\n"
        if c.get("layers"):
            inner += f"            layers = <{' '.join(str(l) for l in c['layers'])}>;\n"
        if c.get("timeout"):
            inner += f"            timeout-ms = <{int(c['timeout'])}>;\n"
        inner += "        };\n"
    new_block = 'combos {\n        compatible = "zmk,combos";\n' + inner + "    };"
    src = re.sub(r"combos\s*\{.*?\n    \};", new_block, src, count=1, flags=re.S)
    open(KEYMAP, "w").write(src)

# ===== 細かな設定 =====

def parse_settings():
    r_ov = open(OVERLAYS[0]).read()
    conf = open(CONFS[0]).read()
    tb = open(TRACKBALL_OVERLAY).read()
    aml = re.search(r"&zip_temp_layer\s+AUTO_MOUSE_LAYER\s+(\d+)", r_ov)
    excl = re.search(r"excluded-positions\s*=\s*<([\d\s]*)>;", r_ov)
    map_s = load_position_map_s()
    s2l = {s: i for i, s in enumerate(map_s)}
    excl_l = [s2l[int(x)] for x in excl.group(1).split() if int(x) in s2l] if excl else []
    sleep = re.search(r"CONFIG_ZMK_IDLE_SLEEP_TIMEOUT=(\d+)", conf)
    cpi = re.search(r"res-cpi\s*=\s*<(\d+)>;", tb)
    # scroller ノード (スクロールレイヤーの入力処理)
    scr = re.search(r"scroller\s*\{(.*?)\n    \};", r_ov, re.S)
    scr_body = scr.group(1) if scr else ""
    scr_div = re.search(r"zip_scroll_scaler\s+1\s+(\d+)", scr_body)
    # AML判定用のinput-processorsはpointing_listener直下だけを見る(scroller内を除外)
    pl = re.search(r"&pointing_listener\s*\{(.*?)\n\};", r_ov, re.S)
    pl_body = (pl.group(1) if pl else "").replace(scr_body, "")
    return {
        "amlEnabled": bool(aml),
        "amlTimeout": int(aml.group(1)) if aml else 1000,
        "amlExcluded": excl_l,  # L番号で返す(GUIはL番号で扱う)
        "invertX": "INPUT_TRANSFORM_X_INVERT" in pl_body,
        "invertY": "INPUT_TRANSFORM_Y_INVERT" in pl_body,
        "sleepMin": int(sleep.group(1)) // 60000 if sleep else 150,
        "cpi": int(cpi.group(1)) if cpi else None,
        "cpiMin": CPI_MIN, "cpiMax": CPI_MAX, "cpiStep": CPI_STEP, "cpiDefault": CPI_DEFAULT,
        "scrollEnabled": bool(scr),
        "scrollInvertX": "INPUT_TRANSFORM_X_INVERT" in scr_body,
        "scrollInvertY": "INPUT_TRANSFORM_Y_INVERT" in scr_body,
        "scrollDiv": int(scr_div.group(1)) if scr_div else 12,
    }


def write_settings(s):
    map_s = load_position_map_s()
    # overlays (right/left 共通内容)
    flags = [f for f, on in (("INPUT_TRANSFORM_X_INVERT", s["invertX"]), ("INPUT_TRANSFORM_Y_INVERT", s["invertY"])) if on]
    procs = []
    if flags:
        procs.append(f"<&zip_xy_transform ({' | '.join(flags)})>")
    if s["amlEnabled"]:
        procs.append(f"<&zip_temp_layer AUTO_MOUSE_LAYER {int(s['amlTimeout'])}>")
    listener = "&pointing_listener {\n    input-processors =\n        " + ",\n        ".join(procs) + ";\n};" if procs else "&pointing_listener {\n};"
    excl_s = sorted(map_s[l] for l in s["amlExcluded"] if map_s[l] <= 43)
    for path in OVERLAYS:
        src = open(path).read()
        src = re.sub(r"&pointing_listener\s*\{.*?\};", listener, src, count=1, flags=re.S)
        src = re.sub(r"excluded-positions\s*=\s*<[\d\s]*>;", f"excluded-positions = <{' '.join(str(x) for x in excl_s)}>;", src, count=1)
        open(path, "w").write(src)
    # sleep (両conf)
    for path in CONFS:
        src = open(path).read()
        src = re.sub(r"CONFIG_ZMK_IDLE_SLEEP_TIMEOUT=\d+", f"CONFIG_ZMK_IDLE_SLEEP_TIMEOUT={int(s['sleepMin']) * 60000}", src)
        open(path, "w").write(src)
    # スクロール (scrollerノードの input-processors を再生成、両overlay)
    sflags = [f for f, on in (("INPUT_TRANSFORM_X_INVERT", s.get("scrollInvertX")),
                              ("INPUT_TRANSFORM_Y_INVERT", s.get("scrollInvertY"))) if on]
    sprocs = []
    if sflags:
        sprocs.append(f"<&zip_xy_transform ({' | '.join(sflags)})>")
    sprocs.append("<&zip_xy_to_scroll_mapper>")
    sprocs.append(f"<&zip_scroll_scaler 1 {max(1, int(s.get('scrollDiv', 12)))}>")
    for path in OVERLAYS:
        src = open(path).read()
        m = re.search(r"scroller\s*\{.*?layers = <(\d+)>.*?\n    \};", src, re.S)
        if m:
            scroll_layer = m.group(1)   # 既存のスクロールレイヤー番号を保持(ハードコードしない)
            scroller_node = (f"scroller {{\n        layers = <{scroll_layer}>;\n        input-processors =\n            "
                             + ",\n            ".join(sprocs) + ";\n    };")
            src = re.sub(r"scroller\s*\{.*?\n    \};", scroller_node, src, count=1, flags=re.S)
            open(path, "w").write(src)
    # CPI (トラックボールスニペット)。範囲外はビルドを壊す(BUILD_ASSERT)ため clamp+step丸め
    tb = open(TRACKBALL_OVERLAY).read()
    tb = re.sub(r"\n\s*res-cpi\s*=\s*<\d+>;", "", tb)
    if s.get("cpi"):
        cpi = round(int(s["cpi"]) / CPI_STEP) * CPI_STEP        # 50刻みに丸め
        cpi = max(CPI_MIN, min(CPI_MAX, cpi))                   # 有効域(650〜4800)にclamp
        tb = tb.replace("spi-max-frequency = <2000000>;", f"spi-max-frequency = <2000000>;\n        res-cpi = <{cpi}>;")
    open(TRACKBALL_OVERLAY, "w").write(tb)

# ===== ビルド =====

BUILD = {"running": False, "log": "", "ok": None, "artifact": None}


def run_build(artifact):
    BUILD.update(running=True, log=f"==> {artifact} のビルドを開始...\n", ok=None, artifact=artifact)
    try:
        p = subprocess.Popen([os.path.join(REPO, "local.sh"), artifact],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=REPO)
        for line in p.stdout:
            BUILD["log"] += line
        p.wait()
        BUILD["ok"] = p.returncode == 0
        BUILD["log"] += "\n==> " + ("完了 ✓" if BUILD["ok"] else f"失敗 (exit {p.returncode})") + "\n"
    except Exception as e:
        BUILD["ok"] = False
        BUILD["log"] += f"\nエラー: {e}\n"
    finally:
        BUILD["running"] = False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  {self.command} {self.path} {args[1] if len(args) > 1 else ''}", flush=True)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def do_GET(self):
        try:
            if self.path in ("/", "/index.html"):
                body = open(os.path.join(os.path.dirname(__file__), "gui.html"), "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/keymap":
                self._json({"layers": parse_layers(), "geom": geometry(), "names": load_names()})
            elif self.path == "/extras":
                self._json({"combos": parse_combos(), "settings": parse_settings()})
            elif self.path == "/build-status":
                self._json(BUILD)
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        try:
            if self.path == "/quit":
                self._json({"ok": True})
                threading.Timer(0.3, lambda: os._exit(0)).start()
            elif self.path == "/apply":
                self._apply()
            elif self.path == "/extras":
                data = self._body()
                if "combos" in data:
                    for c in data["combos"]:
                        if not re.fullmatch(r"[a-z_][a-z0-9_]*", c["name"]):
                            return self._json({"error": f"コンボ名が不正: {c['name']} (小文字英数字と_のみ)"}, 400)
                        if len(c["positions"]) < 2:
                            return self._json({"error": f"コンボ {c['name']}: キーは2個以上"}, 400)
                    write_combos(data["combos"])
                if "settings" in data:
                    write_settings(data["settings"])
                self._json({"ok": True})
            elif self.path == "/layers/add":
                self._json({"count": add_layer()})
            elif self.path == "/layers/insert":
                b = self._body()
                self._json({"count": insert_layer(int(b["pos"]), b.get("name", "追加レイヤー"))})
            elif self.path == "/build":
                if BUILD["running"]:
                    return self._json({"error": "ビルド実行中です"}, 409)
                art = self._body().get("artifact", "torabo_tsuki_lp_right_central")
                threading.Thread(target=run_build, args=(art,), daemon=True).start()
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _apply(self):
        changes = self._body()["changes"]
        if not changes:
            return self._json({"error": "変更がありません"}, 400)
        results, p = [], None
        try:
            p = Pusher()
            dev_layers = len(p.keymap.layers)
            for ch in changes:
                li = int(ch["layer"])
                if li >= dev_layers:  # 実機にまだ無いレイヤー → ソースのみ(要ビルド)
                    results.append({"layer": li, "pos": ch["pos"], "binding": ch["binding"], "ok": False, "needsBuild": True})
                    continue
                ok = p.set_binding(li, int(ch["pos"]), ch["binding"])
                results.append({"layer": li, "pos": ch["pos"], "binding": ch["binding"], "ok": bool(ok)})
            saved = p.save() if any(r["ok"] for r in results) else False
        finally:
            if p:
                p.c.close()
        layers = parse_layers()
        changed = set()
        for r in results:
            if r["ok"] or r.get("needsBuild"):
                layers[r["layer"]][r["pos"]] = r["binding"]
                changed.add(r["layer"])
        if changed:
            write_keymap(layers, changed)
        self._json({"results": results, "saved": saved, "space": p.space})


if __name__ == "__main__":
    print(f"torabo-tsuki keymap GUI: http://localhost:{PORT}", flush=True)
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
