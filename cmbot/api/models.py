from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import List


@dataclass
class Request:
    request_id: str
    random_key: str
    title: str
    author: str
    budget: str
    request_type: str = ""
    tags: List = field(default_factory=list)
    interested_count: int = 0
    posted_time: str = ""
    url: str = ""
    description: str = ""
    already_interested: bool = False
    processed_at: str = ""

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**d)
