"""FastAPI 入口：胶片档案扫描整理 API。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Response, status
from sqlite3 import Connection

from . import database, schemas, services


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动即建库建表（幂等），依赖与数据库初始化都纳入启动流程
    database.init_db()
    yield


app = FastAPI(
    title="胶片档案扫描整理 API",
    description="按胶卷汇总扫描帧、去重、统计预期范围内的连续缺帧；批次导入幂等。",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", response_model=schemas.HealthOut, tags=["系统"])
def health():
    return {"status": "ok", "db": str(database.get_db_path())}


@app.post(
    "/batches",
    response_model=schemas.BatchResult,
    status_code=status.HTTP_201_CREATED,
    tags=["批次"],
)
def submit_batch(
    payload: schemas.BatchIn,
    response: Response,
    conn: Connection = Depends(database.get_db),
):
    """提交一个扫描批次。相同内容重复提交返回 200 且不改变任何结果。"""
    try:
        result, code = services.submit_batch(conn, payload)
    except services.BatchIdConflictError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"batch_id '{e.batch_id}' 已被其他内容占用，请更换后重试",
        )
    response.status_code = code
    return result


@app.get("/batches/{batch_id}", response_model=schemas.BatchResult, tags=["批次"])
def get_batch(batch_id: str, conn: Connection = Depends(database.get_db)):
    result = services.get_batch(conn, batch_id)
    if result is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    return result


@app.get("/rolls", response_model=list[schemas.RollSummary], tags=["胶卷"])
def list_rolls(conn: Connection = Depends(database.get_db)):
    """列出所有胶卷及其汇总状态。"""
    return services.list_rolls(conn)


@app.get("/rolls/{roll_no}", response_model=schemas.RollState, tags=["胶卷"])
def get_roll(roll_no: str, conn: Connection = Depends(database.get_db)):
    """查询单个胶卷的合并帧号、重复帧明细与连续缺帧区间。"""
    result = services.get_roll_state(conn, roll_no.strip())
    if result is None:
        raise HTTPException(status_code=404, detail="胶卷不存在（尚未导入任何批次）")
    return result


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
    )
