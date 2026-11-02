"""Strict provider schemas; reference integrity is handled separately."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class Character(StrictModel):
    id: Annotated[str, Field(pattern=r"^ch_[a-z0-9]+(?:_[a-z0-9]+)*$")]
    name: str
    aliases: list[str]
    role: Literal["protagonist", "antagonist", "supporting", "minor"]
    description: str
    arc: str
    entity_type: Literal["human", "animal", "creature", "group", "object", "unknown"] = "unknown"
    identity_scope: Literal["individual", "generic", "collective", "unspecified"] = "unspecified"


class Location(StrictModel):
    id: Annotated[str, Field(pattern=r"^loc_[a-z0-9]+(?:_[a-z0-9]+)*$")]
    name: str
    int_ext: Literal["INT", "EXT", "INT/EXT"]
    description: str


class Prop(StrictModel):
    id: Annotated[str, Field(pattern=r"^pr_[a-z0-9]+(?:_[a-z0-9]+)*$")]
    name: str
    importance: str


class Registry(StrictModel):
    title: str
    logline: str
    genre: str
    characters: list[Character]
    locations: list[Location]
    props: list[Prop]


class Scene(StrictModel):
    index: int
    heading: str
    location_id: str
    time_of_day: str
    character_ids: list[str]
    summary: str
    mood: str
    duration_sec: Annotated[int, Field(ge=0)]


class Breakdown(StrictModel):
    scenes: list[Scene]
