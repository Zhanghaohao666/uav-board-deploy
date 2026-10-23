#!/usr/bin/env python3
import hashlib,json,tarfile
from pathlib import Path
root=Path(__file__).resolve().parent
# Explicit roots: never sweep a maintainer's temporary exports or license keys.
allowed_dirs={'board','algorithm','common','docs','gimbal','pc','tools','tests','.github'}
allowed_files={'README.md','CHANGELOG.md','.gitignore','config.example.json','MANIFEST.json',
               'install.py','install.sh','install_recorder.py','install_record_api.py',
               'install_lidar_recorder.py','update_low_streams.py','bootstrap.sh','make_release.py',
               'deploy.py','deploy.sh'}
files=[]
for path in sorted(root.rglob('*')):
    rel=path.relative_to(root)
    if rel.parts[0] not in allowed_dirs and str(rel) not in allowed_files:continue
    if path.is_symlink():raise ValueError('发布包不允许软链接：'+str(rel))
    if not path.is_file() or any(p in ('.git','__pycache__') for p in rel.parts):continue
    if path.name in ('MANIFEST.json','board-config.json','test_unpack') or str(rel)=='board/config.json':continue
    if path.suffix in ('.pyc','.gz','.zip','.log','.o'):continue
    if path.suffix in ('.pem','.key','.nglic','.ngreq') or path.name in ('token','id_rsa','id_ed25519'):
        raise ValueError('禁止发布凭证：'+str(rel))
    files.append(path)
manifest={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
(root/'MANIFEST.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
archive=root.parent/'uav-board-deploy.tar.gz'
with tarfile.open(archive,'w:gz') as tar:
    for path in files+[root/'MANIFEST.json']:tar.add(path,arcname='uav-board-deploy/'+str(path.relative_to(root)),recursive=False)
print(archive)
