import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    model_base_url: str
    model_name: str
    data_dir: Path = ROOT / 'var'
    host: str = '127.0.0.1'
    port: int = 8787
    origin: str = 'http://127.0.0.1:8787'
    api_key: str = ''
    context_tokens: int = 8192
    output_tokens: int = 1024
    connect_timeout: float = 5
    read_timeout: float = 60
    total_timeout: float = 180

    def __post_init__(self):
        u = urlsplit(self.model_base_url)
        if (self.host != '127.0.0.1' or not 1 <= self.port <= 65535 or self.port == 8765
                or self.origin != f'http://127.0.0.1:{self.port}'
                or u.scheme not in ('http','https') or not u.hostname or u.username or u.password
                or u.query or u.fragment or not u.path.endswith('/v1') or not self.model_name
                or not 0 < self.output_tokens <= 1024 or not self.output_tokens + 512 < self.context_tokens <= 8192):
            raise ValueError('CONFIG_INVALID')
        self.data_dir = Path(self.data_dir).resolve()
        if self.data_dir == ROOT or any(base==self.data_dir or base in self.data_dir.parents for base in (ROOT/'server', ROOT/'public', ROOT/'vendor')):
            raise ValueError('CONFIG_INVALID')
        self.data_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def load(cls):
        load_dotenv(ROOT / '.env', override=False)
        return cls(model_base_url=os.environ.get('MODEL_BASE_URL',''), model_name=os.environ.get('MODEL_NAME',''),
                   data_dir=Path(os.environ.get('DATA_DIR',ROOT / 'var')),host=os.environ.get('APP_HOST','127.0.0.1'),
                   port=int(os.environ.get('APP_PORT','8787')),origin=os.environ.get('APP_ORIGIN','http://127.0.0.1:8787'),
                   api_key=os.environ.get('MODEL_API_KEY',''), context_tokens=int(os.environ.get('MODEL_CONTEXT_TOKENS','8192')),
                   output_tokens=int(os.environ.get('MAX_OUTPUT_TOKENS','1024')))
