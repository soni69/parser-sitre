"""Data models for TV repair scraper."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

try:
  from pydantic import BaseModel, ConfigDict, Field
except ImportError:  # pragma: no cover
  BaseModel = None  # type: ignore[misc, assignment]


@dataclass
class TVRepairData:
  brand: str
  model_name: str
  full_title: str
  chassis: Optional[str] = None
  panel: Optional[str] = None
  backlight: Optional[str] = None
  inverter: Optional[str] = None
  tcon: Optional[str] = None
  tuner: Optional[str] = None
  mainboard: Optional[str] = None
  mainboard_ic: Optional[str] = None
  psu: Optional[str] = None
  pwm_power: Optional[str] = None
  panel_diagonal: Optional[str] = None
  panel_resolution: Optional[str] = None
  panel_active_area: Optional[str] = None
  panel_brightness: Optional[str] = None
  panel_contrast: Optional[str] = None
  panel_display_colors: Optional[str] = None
  panel_frequency: Optional[str] = None
  panel_lamp_type: Optional[str] = None
  panel_voltage: Optional[str] = None
  preview_image: Optional[str] = None
  other_parts: Dict[str, str] = field(default_factory=dict)
  year: Optional[str] = None

  def to_dict(self) -> Dict[str, Any]:
    return asdict(self)

  @classmethod
  def from_dict(cls, data: Dict[str, Any]) -> "TVRepairData":
    return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ModelRef:
  """Lightweight reference discovered on a brand listing page."""

  brand: str
  model_name: str
  url: str
  panel: Optional[str] = None
  backlight: Optional[str] = None
  mainboard: Optional[str] = None
  psu: Optional[str] = None
  source_page: Optional[str] = None


if BaseModel is not None:

  class TVRepairDataSchema(BaseModel):
    model_config = ConfigDict(extra="ignore")

    brand: str
    model_name: str
    full_title: str
    chassis: Optional[str] = None
    panel: Optional[str] = None
    backlight: Optional[str] = None
    inverter: Optional[str] = None
    tcon: Optional[str] = None
    tuner: Optional[str] = None
    mainboard: Optional[str] = None
    mainboard_ic: Optional[str] = None
    psu: Optional[str] = None
    pwm_power: Optional[str] = None
    panel_diagonal: Optional[str] = None
    panel_resolution: Optional[str] = None
    panel_active_area: Optional[str] = None
    panel_brightness: Optional[str] = None
    panel_contrast: Optional[str] = None
    panel_display_colors: Optional[str] = None
    panel_frequency: Optional[str] = None
    panel_lamp_type: Optional[str] = None
    panel_voltage: Optional[str] = None
    preview_image: Optional[str] = None
    other_parts: Dict[str, str] = Field(default_factory=dict)
    year: Optional[str] = None

    @classmethod
    def from_dataclass(cls, item: TVRepairData) -> "TVRepairDataSchema":
      return cls.model_validate(item.to_dict())

    def to_dataclass(self) -> TVRepairData:
      return TVRepairData.from_dict(self.model_dump())
