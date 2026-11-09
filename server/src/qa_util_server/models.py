from dataclasses import dataclass, field


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    model: str
    api_key: str = field(repr=False)


def normalize(text: str) -> str:
    return " ".join(text.split())
