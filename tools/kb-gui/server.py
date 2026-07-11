"""キーマップGUIのローカルサーバ

  GET  /         GUI (gui.html)
  GET  /keymap   現在のkeymap (config/keymap.keymapをパース) + Sレイアウト座標
  POST /apply    変更を実機へRPC反映し、config/keymap.keymap にも書き込む
                 body: {"changes": [{"layer":0, "pos":33, "binding":"&kp K"}, ...]}
"""
import json, os, re, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "zmkrpc"))
from push_keymap import Pusher  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
KEYMAP = os.path.join(REPO, "config/keymap.keymap")
LAYOUTS = os.path.join(REPO, "boards/shields/torabo_tsuki_lp/torabo_tsuki_lp_layouts.dtsi")
ROWS = [12, 12, 14, 14, 14]
PORT = 8756

BIND_RE = re.compile(r"&\S+(?:\s+[^&\s][^&]*?)?(?=\s*&|\s*$)")


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
    pm = re.search(r"position_map_s_1\s*\{.*?positions\s*=\s*<(.*?)>;", dtsi, re.S).group(1)
    map_s = [int(t) for t in pm.split()]
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
    """layer_N の bindings ブロックだけ置換し、コンボ等はそのまま保つ"""
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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  {self.command} {self.path} {args[1] if len(args) > 1 else ''}")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = open(os.path.join(os.path.dirname(__file__), "gui.html"), "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/keymap":
            try:
                self._json({"layers": parse_layers(), "geom": geometry()})
            except Exception as e:
                self._json({"error": str(e)}, 500)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path != "/apply":
            return self._json({"error": "not found"}, 404)
        try:
            n = int(self.headers.get("Content-Length", 0))
            changes = json.loads(self.rfile.read(n))["changes"]
            if not changes:
                return self._json({"error": "変更がありません"}, 400)
            results, p = [], None
            try:
                p = Pusher()
                for ch in changes:
                    ok = p.set_binding(int(ch["layer"]), int(ch["pos"]), ch["binding"])
                    results.append({"layer": ch["layer"], "pos": ch["pos"], "binding": ch["binding"], "ok": bool(ok)})
                saved = p.save() if any(r["ok"] for r in results) else False
            finally:
                if p:
                    p.c.close()
            # 成功した変更を keymap.keymap にも反映 (ソース整合)
            layers = parse_layers()
            changed_layers = set()
            for r in results:
                if r["ok"]:
                    layers[r["layer"]][r["pos"]] = r["binding"]
                    changed_layers.add(r["layer"])
            if changed_layers:
                write_keymap(layers, changed_layers)
            self._json({"results": results, "saved": saved, "space": p.space})
        except Exception as e:
            self._json({"error": str(e)}, 500)


if __name__ == "__main__":
    print(f"torabo-tsuki keymap GUI: http://localhost:{PORT}")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
