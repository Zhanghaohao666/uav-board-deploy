#!/bin/bash
# Public GitHub download entry. Private repositories: git clone using your own
# credentials, then run sudo bash install.sh in the checkout.
set -euo pipefail
repo=""
ref="main"
role="main"
while (($#)); do
    case "$1" in
        --repo) repo="${2:?--repo needs OWNER/REPOSITORY}"; shift 2 ;;
        --ref) ref="${2:?--ref needs a tag or commit}"; shift 2 ;;
        --role) role="${2:?--role needs main or algorithm}"; shift 2 ;;
        *) break ;;
    esac
done
case "$role" in
    main) installer=install.sh ;;
    algorithm) installer=algorithm/install.sh ;;
    *) echo '角色必须为 main 或 algorithm' >&2; exit 2 ;;
esac
if [[ ! "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || [[ ! "$ref" =~ ^[A-Za-z0-9_.-]+$ ]]; then
    echo '用法：bootstrap.sh --repo OWNER/REPOSITORY [--ref COMMIT_OR_TAG] [安装参数]' >&2
    exit 2
fi
bundle_tmp="$(mktemp -d)"
trap 'rm -rf -- "$bundle_tmp"' EXIT
curl --fail --location --proto '=https' --tlsv1.2 --retry 3 \
    "https://codeload.github.com/$repo/tar.gz/$ref" -o "$bundle_tmp/bundle.tar.gz"
python3 - "$bundle_tmp" <<'PY'
import pathlib, sys, tarfile
root=pathlib.Path(sys.argv[1]);dest=root/'unpacked';dest.mkdir()
with tarfile.open(root/'bundle.tar.gz','r:gz') as archive:
    members=archive.getmembers()
    total=0
    for member in members:
        target=(dest/member.name).resolve()
        if dest.resolve() not in target.parents or not (member.isfile() or member.isdir()):
            raise SystemExit('拒绝包含越界路径、链接或特殊设备的安装包')
        total+=member.size
        if total>100*1024*1024:raise SystemExit('安装包超出大小限制')
    archive.extractall(dest,members=members)
roots=list(dest.iterdir())
if len(roots)!=1 or not (roots[0]/'install.sh').is_file():raise SystemExit('安装包结构不正确')
(root/'bundle-path').write_text(str(roots[0]))
PY
bundle_path="$(cat "$bundle_tmp/bundle-path")"
# Preserve the terminal for mode/interface selection. Do not pipe this script to bash.
bash "$bundle_path/$installer" "$@"
