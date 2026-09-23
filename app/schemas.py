"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class BatchIn(BaseModel):
    roll_no: str = Field(..., min_length=1, description="胶卷编号")
    expected_start: int = Field(..., ge=0, description="预期帧号起始（含）")
    expected_end: int = Field(..., ge=0, description="预期帧号结束（含）")
    frames: list[int] = Field(..., min_length=1, description="本批扫描帧号列表")
    batch_id: str | None = Field(
        None, description="客户端批次号（可选）；留空则按内容生成"
    )

    @field_validator("roll_no", "batch_id")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("expected_end")
    @classmethod
    def _check_range(cls, v: int, info) -> int:
        start = info.data.get("expected_start")
        if start is not None and v < start:
            raise ValueError("expected_end 不能小于 expected_start")
        return v


class BatchResult(BaseModel):
    batch_id: str
    roll_no: str
    repeated: bool = Field(..., description="True=内容相同的批次已导入过，本次未做任何更改")
    added_frames: list[int]
    duplicates: list[int] = Field(..., description="重复帧（批内重复 + 胶卷已有），只计一次")
    merged_frames: list[int] = Field(..., description="导入后该胶卷的全部帧号（去重排序）")
    expected_ranges: list[list[int]]
    missing_ranges: list[list[int]] = Field(..., description="预期范围内的连续缺帧区间")
    out_of_range_frames: list[int] = Field(..., description="落在全部预期范围之外的帧")
    created_at: str | None = None


class RollState(BaseModel):
    roll_no: str
    expected_ranges: list[list[int]]
    frames: list[int]
    missing_ranges: list[list[int]]
    out_of_range_frames: list[int]
    total_frames: int
    batch_count: int
    batches: list["BatchMeta"]


class BatchMeta(BaseModel):
    batch_id: str
    expected_start: int
    expected_end: int
    frames: list[int]
    added_frames: list[int]
    duplicates: list[int]
    created_at: str


class RollSummary(BaseModel):
    roll_no: str
    total_frames: int
    batch_count: int
    missing_ranges: list[list[int]]


class HealthOut(BaseModel):
    status: str
    db: str


RollState.model_rebuild()
