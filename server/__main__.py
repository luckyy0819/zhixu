import uvicorn
from .config import Settings
from .main import create_app

if __name__=='__main__':
    cfg=Settings.load()
    uvicorn.run(create_app(cfg),host=cfg.host,port=cfg.port,workers=1,access_log=False)
