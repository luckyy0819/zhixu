"""Build an isolated immutable-by-convention candidate; refuses to overwrite it."""
import hashlib
import json
import shutil
from datetime import datetime,timezone
from pathlib import Path

root=Path(__file__).resolve().parents[2]
candidate=root/'output/p0-20261125-dev2'
candidate.mkdir(exist_ok=False)
paths=[p for p in (root/'server').rglob('*.py') if '__pycache__' not in str(p)]
paths += [root/p for p in ['app.js','client-core.js','index.html','styles.css','requirements.txt','requirements.in','.env.example','.gitignore','package.json','package-lock.json']]
paths += [p for p in (root/'vendor').glob('*') if p.is_file()]
paths += [p for p in (root/'tests/unit').glob('*') if p.suffix in ('.py','.cjs')]
files=[]
for source in sorted(paths):
    relative=source.relative_to(root).as_posix()
    target=candidate/relative;target.parent.mkdir(exist_ok=True,parents=True);shutil.copy2(source,target)
    files.append({'path':relative,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'bytes':target.stat().st_size})
source_hash=hashlib.sha256(''.join(f"{f['path']}\0{f['sha256']}\0{f['bytes']}\n" for f in files).encode()).hexdigest()
shutil.copyfile(root/'.env.example',candidate/'.env')
manifest={'task':'ZHIXU-TASK-005','version':'0.1.0-p0.2','created_at':datetime.now(timezone.utc).isoformat(),'source_root':str(root),'candidate':str(candidate),'source_hash':source_hash,'files':files,'command':'canonical .venv/Scripts/python.exe -m server, cwd=candidate','port':8787,'scope':'F01 account epoch / F02 bounded cleanup / E01 actual ASGI evidence; C01 pending PM','baseline':'BL002 requirement0.2 / BL003 design0.4','independent_acceptance':'pending'}
(candidate/'candidate-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
(root/'docs/engineering/audit/AI-DEV/2026-11-25-p0.2/candidate-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'candidate':str(candidate),'version':manifest['version'],'source_hash':source_hash,'files':len(files)}))
