"""Configuration d'un cluster et filtres de recherche."""
from pydantic import BaseModel, ConfigDict, Field
from typing import Optional, Union


class Cluster(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    url: str
    user: Optional[str] = None
    password: Optional[str] = Field(default=None, repr=False, exclude=True)
    api_key: Optional[str] = Field(default=None, repr=False, exclude=True)
    ca_file: Optional[str] = None


class Filters(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    policy: Optional[str] = None
    region: Optional[str] = None  # None = EMEA + APAC + AMER
    starttime: Optional[Union[str, int, float]] = None
    endtime: Optional[Union[str, int, float]] = None
    text: Optional[str] = None  # Phrase recherchée dans le tableau content
