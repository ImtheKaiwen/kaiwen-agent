from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.models.capabilities import DataLocality


class ModelTier(StrEnum):
    CHEAP = "cheap"
    STANDARD = "standard"
    STRONG = "strong"
    EMBEDDING = "embedding"
    REALTIME = "realtime"


class ModelComplexity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ModelRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    tier: ModelTier
    capabilities: frozenset[str] = Field(min_length=1)
    complexities: frozenset[ModelComplexity] = Field(min_length=1)
    priority: int = Field(default=0, ge=-100, le=100)
    enabled: bool = True
    data_locality: DataLocality = DataLocality.CLOUD_ALLOWED
    regions: frozenset[str] = Field(default_factory=frozenset)


class ModelSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route_id: str
    provider: str
    model: str
    tier: ModelTier
    capability: str
    complexity: ModelComplexity
    data_locality: DataLocality = DataLocality.CLOUD_ALLOWED
    regions: frozenset[str] = Field(default_factory=frozenset)
