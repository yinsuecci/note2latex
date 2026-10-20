import json
import time

import httpx

from .config import Settings


class ProviderError(RuntimeError):
    pass


class Provider:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.transport = transport

    def chat(self, messages, *, vision=False, tools=None):
        self.settings.require_provider()
        body = {
            "model": self.settings.vision_model if vision else self.settings.agent_model,
            "messages": messages, "temperature": 0, "max_tokens": 6000,
        }
        if tools:
            body.update(tools=tools, tool_choice="auto")
        for attempt in range(3):
            try:
                with httpx.Client(timeout=self.settings.timeout, transport=self.transport) as client:
                    response = client.post(
                        self.settings.base_url + "/chat/completions",
                        headers={"Authorization": f"Bearer {self.settings.api_key}"},
                        json=body,
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                if response.status_code >= 400:
                    # Never persist provider response bodies: they may echo credentials.
                    raise ProviderError(f"Provider HTTP {response.status_code}; check model, capabilities and quota")
                result = response.json()["choices"][0]
                if result.get("finish_reason") == "length":
                    raise ProviderError("Provider output truncated; reduce page density")
                message = result["message"]
                if not isinstance(message, dict):
                    raise ValueError("Invalid message")
                return message
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise ProviderError("Provider connection failed or timed out") from None
            except (KeyError, IndexError, ValueError):
                raise ProviderError("Provider returned an invalid completion") from None
        raise ProviderError("Provider retries exhausted")


def parse_object(content):
    if not isinstance(content, str):
        raise ValueError("Missing JSON content")
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[-1].strip() != "```":
            raise ValueError("Incomplete code fence")
        text = "\n".join(lines[1:-1])
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Expected JSON object")
    return value

