"""build.yaml から artifact-name に対応する shield/snippet を shell export 形式で出力する。
local.sh から呼ばれる (bash 3.2 でも動くよう、シェルのヒアドキュメントを避けるため分離)。

  python3 build_query.py <artifact-name> <build.yaml のパス>
"""
import sys, yaml, shlex

art = sys.argv[1]
path = sys.argv[2]
for e in yaml.safe_load(open(path))["include"]:
    if e.get("artifact-name") == art or (e.get("shield") == art and not e.get("artifact-name")):
        print(f'export SHIELD={shlex.quote(e.get("shield", ""))}')
        print(f'export SNIPPET={shlex.quote(e.get("snippet", ""))}')
        sys.exit(0)
print(f'''echo "artifact '{art}' が build.yaml に見つかりません" >&2; exit 1''')
