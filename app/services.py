"""核心业务逻辑：批次导入、跨批次合并、缺帧区间计算、补扫任务认领。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from . import schemas


# ---------- 纯函数（便于测试） ----------

def content_hash(roll_no: str, start: int, end: int, frames: list[int]) -> str:
    """同一胶卷下相同（预期范围 + 帧号集合）的内容指纹，用于幂等判定。"""
    payload = json.dumps(
        {"roll_no": roll_no, "start": start, "end": end, "frames": sorted(set(frames))},
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并重叠或相邻的闭区间，例如 [(1,5),(5,8)] -> [(1,8)]。"""
    if not intervals:
        return []
    merged: list[tuple[int, int]] = []
    for s, e in sorted(intervals):
        if not merged or s > merged[-1][1] + 1:
            merged.append((s, e))
        else:
            prev_s, prev_e = merged[-1]
            merged[-1] = (prev_s, max(prev_e, e))
    return merged


def missing_intervals(
    expected: list[tuple[int, int]], frames: set[int]
) -> list[tuple[int, int]]:
    """返回合并后的预期范围内、未被扫描帧覆盖的连续缺帧区间。"""
    result: list[tuple[int, int]] = []
    for s, e in merge_intervals(expected):
        gap_start: int | None = None
        for n in range(s, e + 1):
            if n in frames:
                if gap_start is not None:
                    result.append((gap_start, n - 1))
                    gap_start = None
            elif gap_start is None:
                gap_start = n
        if gap_start is not None:
            result.append((gap_start, e))
    return result


def out_of_range_frames(
    expected: list[tuple[int, int]], frames: set[int]
) -> list[int]:
    expected = merge_intervals(expected)
    return sorted(
        f for f in frames if not any(s <= f <= e for s, e in expected)
    )


def subtract_intervals(
    base: list[tuple[int, int]], blocked: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """从 base 的各闭区间中扣除 blocked 覆盖的部分，返回剩余闭区间。"""
    result: list[tuple[int, int]] = []
    for s, e in base:
        pieces = [(s, e)]
        for bs, be in blocked:
            next_pieces: list[tuple[int, int]] = []
            for ps, pe in pieces:
                if be < ps or bs > pe:  # 不相交
                    next_pieces.append((ps, pe))
                else:
                    if bs > ps:
                        next_pieces.append((ps, bs - 1))
                    if be < pe:
                        next_pieces.append((be + 1, pe))
            pieces = next_pieces
        result.extend(pieces)
    return merge_intervals(result)


def intersect_intervals(
    a: list[tuple[int, int]], b: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """两组闭区间的交集。"""
    result: list[tuple[int, int]] = []
    for as_, ae in a:
        for bs, be in b:
            s, e = max(as_, bs), min(ae, be)
            if s <= e:
                result.append((s, e))
    return merge_intervals(result)


# ---------- 数据访问 ----------

def _expected_ranges(conn: sqlite3.Connection, roll_no: str) -> list[tuple[int, int]]:
    rows = conn.execute(
        "SELECT start_no, end_no FROM roll_expected WHERE roll_no = ?",
        (roll_no,),
    ).fetchall()
    return merge_intervals([(r["start_no"], r["end_no"]) for r in rows])


def _all_frames(conn: sqlite3.Connection, roll_no: str) -> set[int]:
    rows = conn.execute(
        "SELECT frame_no FROM roll_frames WHERE roll_no = ?", (roll_no,)
    ).fetchall()
    return {r["frame_no"] for r in rows}


def _batch_metas(conn: sqlite3.Connection, roll_no: str) -> list[schemas.BatchMeta]:
    rows = conn.execute(
        """SELECT id, expected_start, expected_end, frames_json,
                  frames_merged_json, duplicates_json, created_at
             FROM batches WHERE roll_no = ? ORDER BY created_at, rowid""",
        (roll_no,),
    ).fetchall()
    return [
        schemas.BatchMeta(
            batch_id=r["id"],
            expected_start=r["expected_start"],
            expected_end=r["expected_end"],
            frames=json.loads(r["frames_json"]),
            added_frames=json.loads(r["frames_merged_json"]),
            duplicates=json.loads(r["duplicates_json"]),
            created_at=r["created_at"],
        )
        for r in rows
    ]


def _state_from_db(conn: sqlite3.Connection, roll_no: str) -> dict:
    expected = _expected_ranges(conn, roll_no)
    frames = _all_frames(conn, roll_no)
    return {
        "expected": expected,
        "frames": frames,
        "missing": missing_intervals(expected, frames),
        "out_of_range": out_of_range_frames(expected, frames),
    }


def get_roll_state(conn: sqlite3.Connection, roll_no: str) -> schemas.RollState | None:
    if not conn.execute(
        "SELECT 1 FROM batches WHERE roll_no = ? LIMIT 1", (roll_no,)
    ).fetchone():
        return None
    st = _state_from_db(conn, roll_no)
    metas = _batch_metas(conn, roll_no)
    frames_sorted = sorted(st["frames"])
    return schemas.RollState(
        roll_no=roll_no,
        expected_ranges=[list(x) for x in st["expected"]],
        frames=frames_sorted,
        missing_ranges=[list(x) for x in st["missing"]],
        out_of_range_frames=st["out_of_range"],
        total_frames=len(frames_sorted),
        batch_count=len(metas),
        batches=metas,
    )


def list_rolls(conn: sqlite3.Connection) -> list[schemas.RollSummary]:
    rows = conn.execute(
        """SELECT roll_no,
                  (SELECT COUNT(*) FROM roll_frames f WHERE f.roll_no = b.roll_no) AS total,
                  COUNT(*) AS batch_count
             FROM batches b GROUP BY roll_no ORDER BY roll_no"""
    ).fetchall()
    out: list[schemas.RollSummary] = []
    for r in rows:
        st = _state_from_db(conn, r["roll_no"])
        out.append(
            schemas.RollSummary(
                roll_no=r["roll_no"],
                total_frames=r["total"],
                batch_count=r["batch_count"],
                missing_ranges=[list(x) for x in st["missing"]],
            )
        )
    return out


def get_batch(conn: sqlite3.Connection, batch_id: str) -> schemas.BatchResult | None:
    r = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if r is None:
        return None
    st = _state_from_db(conn, r["roll_no"])
    return schemas.BatchResult(
        batch_id=r["id"],
        roll_no=r["roll_no"],
        repeated=True,
        added_frames=json.loads(r["frames_merged_json"]),
        duplicates=json.loads(r["duplicates_json"]),
        merged_frames=sorted(st["frames"]),
        expected_ranges=[list(x) for x in st["expected"]],
        missing_ranges=[list(x) for x in st["missing"]],
        out_of_range_frames=st["out_of_range"],
        created_at=r["created_at"],
    )


def submit_batch(conn: sqlite3.Connection, data: schemas.BatchIn) -> tuple[schemas.BatchResult, int]:
    """导入一个批次。返回 (结果, HTTP 状态码 201新建 / 200重复)。

    幂等保证：(roll_no, 内容指纹) 唯一。相同内容重复导入不写任何数据。
    """
    frames = sorted(set(data.frames))
    digest = content_hash(
        data.roll_no, data.expected_start, data.expected_end, frames
    )

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT id FROM batches WHERE roll_no = ? AND content_hash = ?",
            (data.roll_no, digest),
        ).fetchone()
        if existing is not None:
            conn.execute("ROLLBACK")
            result = get_batch(conn, existing["id"])
            assert result is not None
            return result, 200

        # 客户端自带 batch_id 若已被不同内容占用则拒绝（409 在 main 中处理）
        if data.batch_id is not None:
            clash = conn.execute(
                "SELECT roll_no, content_hash FROM batches WHERE id = ?",
                (data.batch_id,),
            ).fetchone()
            if clash is not None and (
                clash["roll_no"] != data.roll_no or clash["content_hash"] != digest
            ):
                raise BatchIdConflictError(data.batch_id)

        batch_id = data.batch_id or f"auto-{uuid.uuid4().hex}"

        existing_frames = _all_frames(conn, data.roll_no)

        within_submit: set[int] = set()
        dup_seen: set[int] = set()
        for f in data.frames:  # 未去重的原始列表，检出批内重复
            if f in dup_seen:
                within_submit.add(f)
            dup_seen.add(f)
        # 批内重复 + 该胶卷此前批次已扫到的帧
        duplicates = sorted(within_submit | (set(frames) & existing_frames))
        new_frames = sorted(set(frames) - existing_frames)

        conn.execute(
            """INSERT INTO batches
               (id, roll_no, expected_start, expected_end, frames_json,
                frames_merged_json, duplicates_json, content_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                batch_id,
                data.roll_no,
                data.expected_start,
                data.expected_end,
                json.dumps(frames),
                json.dumps(new_frames),
                json.dumps(duplicates),
                digest,
            ),
        )
        conn.execute(
            "INSERT OR IGNORE INTO roll_expected (roll_no, start_no, end_no) VALUES (?, ?, ?)",
            (data.roll_no, data.expected_start, data.expected_end),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO roll_frames (roll_no, frame_no, batch_id) VALUES (?, ?, ?)",
            [(data.roll_no, f, batch_id) for f in new_frames],
        )
        conn.execute("COMMIT")
    except BatchIdConflictError:
        conn.execute("ROLLBACK")
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise

    st = _state_from_db(conn, data.roll_no)
    result = schemas.BatchResult(
        batch_id=batch_id,
        roll_no=data.roll_no,
        repeated=False,
        added_frames=new_frames,
        duplicates=duplicates,
        merged_frames=sorted(st["frames"]),
        expected_ranges=[list(x) for x in st["expected"]],
        missing_ranges=[list(x) for x in st["missing"]],
        out_of_range_frames=st["out_of_range"],
    )
    return result, 201


class BatchIdConflictError(Exception):
    def __init__(self, batch_id: str):
        self.batch_id = batch_id
        super().__init__(f"batch_id 已被其他内容占用: {batch_id}")


# ---------- 补扫任务认领 ----------

def _now() -> tuple[int, str]:
    """返回 (Unix 秒, 带毫秒的 UTC ISO 字符串)，两者取自同一时刻。"""
    dt = datetime.now(timezone.utc)
    ts = int(dt.timestamp())
    iso = dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
    return ts, iso


def _iso_from_epoch(epoch: int) -> str:
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.000Z")
    )


def _roll_exists(conn: sqlite3.Connection, roll_no: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM batches WHERE roll_no = ? LIMIT 1", (roll_no,)
    ).fetchone() is not None


def _active_claims(
    conn: sqlite3.Connection, roll_no: str, now_ts: int
) -> list[sqlite3.Row]:
    """该胶卷所有未到期认领（到期任务的帧段视为已让出，不参与扣除）。"""
    return conn.execute(
        """SELECT id, roll_no, claimer, start_no, end_no,
                  created_at, expires_at, expire_epoch
             FROM rescan_claims
            WHERE roll_no = ? AND expire_epoch > ?
            ORDER BY start_no, created_at, rowid""",
        (roll_no, now_ts),
    ).fetchall()


def _claim_to_out(
    row: sqlite3.Row, missing_now: list[tuple[int, int]]
) -> schemas.RescanClaimOut:
    """附带该任务仍缺失的帧段（与当前最新缺帧求交，已扫到的帧自动扣除）。"""
    remaining = intersect_intervals([(row["start_no"], row["end_no"])], missing_now)
    return schemas.RescanClaimOut(
        id=row["id"],
        roll_no=row["roll_no"],
        claimer=row["claimer"],
        range=[row["start_no"], row["end_no"]],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        remaining_missing_ranges=[list(x) for x in remaining],
    )


class RollNotFoundError(Exception):
    pass


class NoClaimableFramesError(Exception):
    pass


def create_rescan_claim(
    conn: sqlite3.Connection, data: schemas.RescanClaimIn, roll_no: str
) -> schemas.RescanClaimOut:
    """原子认领首段不超过 max_frames 的连续缺帧闭区间。

    在 IMMEDIATE 事务内：当前缺帧区间扣除未到期认领后，按帧号升序取第一段
    连续区间的前 max_frames 帧。新批次已扫到的帧不在缺帧中，自然不可领取；
    到期任务不参与扣除，帧段自动让出。
    """
    roll_no = roll_no.strip()
    now_ts, now_iso = _now()
    expire_ts = now_ts + data.ttl_seconds
    expires_iso = _iso_from_epoch(expire_ts)

    conn.execute("BEGIN IMMEDIATE")
    try:
        if not _roll_exists(conn, roll_no):
            conn.execute("ROLLBACK")
            raise RollNotFoundError(roll_no)

        st = _state_from_db(conn, roll_no)
        blocked = [(r["start_no"], r["end_no"]) for r in _active_claims(conn, roll_no, now_ts)]
        available = subtract_intervals(st["missing"], blocked)
        if not available:
            conn.execute("ROLLBACK")
            raise NoClaimableFramesError(roll_no)

        seg_start, seg_end = available[0]
        claim_end = min(seg_end, seg_start + data.max_frames - 1)

        claim_id = f"claim-{uuid.uuid4().hex}"
        conn.execute(
            """INSERT INTO rescan_claims
               (id, roll_no, claimer, start_no, end_no,
                created_at, expires_at, expire_epoch)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                claim_id,
                roll_no,
                data.claimer,
                seg_start,
                claim_end,
                now_iso,
                expires_iso,
                expire_ts,
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        # RollNotFound / NoClaimable 已自行 ROLLBACK；其余异常确保回滚
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

    # 提交后只读重查，组装响应（含该任务仍缺失的帧段）
    row = conn.execute(
        "SELECT * FROM rescan_claims WHERE id = ?", (claim_id,)
    ).fetchone()
    st = _state_from_db(conn, roll_no)
    return _claim_to_out(row, st["missing"])


def list_rescan_claims(
    conn: sqlite3.Connection, roll_no: str
) -> list[schemas.RescanClaimOut] | None:
    """列出胶卷的未到期补扫任务及各自仍缺失的帧段；胶卷不存在返回 None。"""
    roll_no = roll_no.strip()
    if not _roll_exists(conn, roll_no):
        return None
    now_ts, _ = _now()
    st = _state_from_db(conn, roll_no)
    rows = _active_claims(conn, roll_no, now_ts)
    return [_claim_to_out(r, st["missing"]) for r in rows]


def release_rescan_claim(conn: sqlite3.Connection, claim_id: str) -> bool:
    """释放（删除）认领任务。返回 False 表示任务不存在（含已释放/已清理）。"""
    cur = conn.execute(
        "DELETE FROM rescan_claims WHERE id = ?", (claim_id.strip(),)
    )
    return cur.rowcount > 0
