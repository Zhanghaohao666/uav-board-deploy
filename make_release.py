#!/usr/bin/env python3
import hashlib,json,tarfile
from pathlib import Path
root=Path(__file__).resolve().parent
files=[]
for path in sorted(root.rglob('*')):
    rel=path.relative_to(root)
    if not path.is_file() or any(p in ('.git','__pycache__') for p in rel.parts):continue
    if path.name in ('MANIFEST.json','board-config.json') or str(rel)=='board/config.json':continue
    if path.suffix in ('.pyc','.gz','.zip','.log'):continue
    files.append(path)
manifest={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
(root/'MANIFEST.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
archive=root.parent/'uav-board-deploy.tar.gz'
with tarfile.open(archive,'w:gz') as tar:
    for path in files+[root/'MANIFEST.json']:tar.add(path,arcname='uav-board-deploy/'+str(path.relative_to(root)),recursive=False)
print(archive)
