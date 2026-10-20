import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    base_url: str = ""
    api_key: str = ""
    vision_model: str = ""
    agent_model: str = ""
    data_dir: Path = Path("data")
    timeout: float = 90
    max_rounds: int = 4
    auto_review: bool = True
    service_token: str = ""
    compile_pdf: bool = False
    tex_image: str = "note2latex-tex:local"

    @classmethod
    def load(cls):
        load_dotenv()
        return cls(
            base_url=os.getenv("API_BASE_URL", "").rstrip("/"),
            api_key=os.getenv("API_KEY", ""),
            vision_model=os.getenv("VISION_MODEL", ""),
            agent_model=os.getenv("AGENT_MODEL", "") or os.getenv("VISION_MODEL", ""),
            data_dir=Path(os.getenv("DATA_DIR", "data")).resolve(),
            timeout=float(os.getenv("API_TIMEOUT", "90")),
            max_rounds=max(1, min(8, int(os.getenv("AGENT_MAX_ROUNDS", "4")))),
            auto_review=os.getenv("AUTO_REVIEW", "true").lower() == "true",
            service_token=os.getenv("SERVICE_TOKEN", ""),
            compile_pdf=os.getenv("COMPILE_PDF", "false").lower() == "true",
            tex_image=os.getenv("TEX_IMAGE", "note2latex-tex:local"),
        )

    def require_provider(self):
        missing = [k for k, v in {
            "API_BASE_URL": self.base_url, "API_KEY": self.api_key,
            "VISION_MODEL": self.vision_model, "AGENT_MODEL": self.agent_model,
        }.items() if not v]
        if missing:
            raise ValueError("Missing configuration: " + ", ".join(missing))
        url = urlparse(self.base_url)
        local = url.hostname in {"localhost", "127.0.0.1", "::1"}
        if url.username or url.password or not url.hostname:
            raise ValueError("Invalid provider URL")
        if url.scheme != "https" and not (local and url.scheme == "http"):
            raise ValueError("Provider URL requires HTTPS (except localhost)")

