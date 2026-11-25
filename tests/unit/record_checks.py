"""DEV command recorder. Runs checks, stores fresh exit status; no shell interpolation."""
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys

root=Path(__file__).resolve().parents[2]
evidence=root/'docs/engineering/audit/AI-DEV/2026-11-25-p0.2'
evidence.mkdir(exist_ok=True,parents=True)
label=sys.argv[1];args=sys.argv[2:]
started=datetime.datetime.now(datetime.timezone.utc).isoformat()
env={**os.environ,'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}
with (evidence/(label+'.stdout.txt')).open('wb') as out:
    result=subprocess.run(args,cwd=root,env=env,stdout=out,stderr=subprocess.STDOUT)
row={'label':label,'command':args,'started_at':started,'ended_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'exit_code':result.returncode}
with (evidence/'commands.jsonl').open('a',encoding='utf-8') as out:
    out.write(json.dumps(row)+'\n')
print(json.dumps(row))
sys.exit(result.returncode)
