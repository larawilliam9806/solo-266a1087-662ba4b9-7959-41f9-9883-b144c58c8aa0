"""核心业务规则。

计量模型
--------
每个计量点的读数按时刻排序，相邻两条读数构成一个区间 (t1, t2]：

* 若区间 (t1, t2] 内发生过换表（换表时刻 s 满足 t1 < s <= t2，即含后次读数时刻），
  该区间标记为缺口，不推算增量；
* **未配置**归零参数的计量点沿用原规则：增量 = 后次读数 - 前次读数，必须非负
  （倒退则整批拒绝），增量计入后次读数所属的 UTC 日；
* **已配置**归零电量 R（kWh）与最大功率 P（kW）的计量点：
  - 表盘读数须满足 0 <= v < R，否则读数越界；
  - 设区间小时数为 h、归零次数为非负整数 n，则折算增量
        delta = v2 - v1 + n * R
    且 0 <= delta <= P * h（含两端）；
  - 满足条件的 n 唯一时，按后次读数所属 UTC 日计入用量；
  - 存在多个可行 n 时，区间列为“待核”，不计入用量，并给出 n 的取值范围；
  - 不存在任何可行 n 时，区间“无解”，同样列为待核并拒绝引入该状态的写操作。

晚到读数插入后自然拆分旧区间；补录换表后原区间变为缺口，已计入的增量自动撤销。
日用量不做冗余物化，查询时在单个读事务内由原始记录与当前配置现算，
因此“配置、原始记录、受影响日结果”永远在同一事务/同一版本下一起生效。
"""

import bisect
import csv
import io
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, getcontext

# 定宽规范化 UTC 时刻：字典序 == 时间序
TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
MAX_POINT_LEN = 200
MAX_RANGE_DAYS = 400
# 区间换算涉及小时除法，提高 Decimal 精度保证 ceil/floor 取整可靠
getcontext().prec = 40


class CsvError(Exception):
    """CSV 格式或内容非法。"""


class BatchRejected(Exception):
    """批次/配置与既有数据冲突，整批拒绝（调用方回滚事务）。"""


# ---------------------------------------------------------------- 基础工具

def canon_ts(dt):
    return dt.astimezone(timezone.utc).strftime(TS_FORMAT)


def now_ts():
    return canon_ts(datetime.now(timezone.utc))


def parse_ts(raw, where=""):
    """解析 ISO 8601 UTC 时刻，返回规范化字符串。必须显式带 UTC 时区。"""
    s = (raw or "").strip()
    if not s:
        raise CsvError(f"{where}时刻为空")
    t = s[:-1] + "+00:00" if s[-1] in ("Z", "z") else s
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        raise CsvError(
            f"{where}无法解析的时刻 {s!r}（需 ISO 8601，如 2026-01-02T03:04:05Z）"
        )
    if dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        raise CsvError(f"{where}时刻 {s!r} 必须显式使用 UTC（后缀 Z 或 +00:00）")
    return canon_ts(dt)


def parse_value(raw, where=""):
    """解析非负十进制千瓦时。"""
    s = (raw or "").strip()
    if not s:
        raise CsvError(f"{where}读数为空")
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise CsvError(f"{where}非法的十进制读数 {s!r}")
    if not d.is_finite():
        raise CsvError(f"{where}读数须为有限十进制数：{s!r}")
    if d < 0:
        raise CsvError(f"{where}读数不能为负：{s!r}")
    return d


def parse_positive(raw, label):
    """解析网页表单中的正十进制数（归零电量/最大功率）。"""
    s = (raw or "").strip()
    if not s:
        raise ValueError(f"{label}不能为空")
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"{label}须为十进制数：{s!r}")
    if not d.is_finite():
        raise ValueError(f"{label}须为有限十进制数：{s!r}")
    if d <= 0:
        raise ValueError(f"{label}须为正数（大于 0）：{s!r}")
    return d


def value_str(d):
    """Decimal 的规范文本形式（去尾零、不用科学计数法）。"""
    return format(d.normalize(), "f")


def parse_day(s):
    try:
        return datetime.strptime((s or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"非法日期 {s!r}（需 YYYY-MM-DD）")


def fmt_num(d):
    return value_str(d if isinstance(d, Decimal) else Decimal(d))


def fmt_ts(ts):
    """把规范化时刻显示为 '2026-01-02 03:04:05 UTC'（按需保留小数秒）。"""
    body = ts[:-1].replace("T", " ")
    if "." in body:
        head, frac = body.split(".")
        frac = frac.rstrip("0")
        body = head if not frac else f"{head}.{frac}"
    return body + " UTC"


# ---------------------------------------------------------------- CSV 解析

def _iter_rows(text, required):
    try:
        reader = csv.DictReader(io.StringIO(text))
    except csv.Error as e:
        raise CsvError(f"CSV 解析失败：{e}")
    if reader.fieldnames is None:
        raise CsvError("CSV 为空或缺少表头")
    keymap = {}
    for name in reader.fieldnames:
        keymap.setdefault((name or "").strip().lower(), name)
    missing = [c for c in required if c not in keymap]
    if missing:
        raise CsvError(
            f"CSV 缺少必需列：{', '.join(missing)}（需要表头：{', '.join(required)}）"
        )
    for lineno, row in enumerate(reader, start=2):
        if row is None:
            continue
        cells = {k: (row.get(keymap[k]) or "") for k in required}
        if all(not c.strip() for c in cells.values()):
            continue  # 跳过空行
        yield lineno, cells


def parse_readings_csv(text):
    """返回 ({(point, ts): Decimal}, 原始数据行数)。同点同刻不同读数 -> CsvError。"""
    out = {}
    raw_count = 0
    for lineno, cells in _iter_rows(text, ["point", "ts", "value"]):
        raw_count += 1
        where = f"第 {lineno} 行："
        point = cells["point"].strip()
        if not point:
            raise CsvError(f"{where}计量点为空")
        if len(point) > MAX_POINT_LEN:
            raise CsvError(f"{where}计量点名称过长（>{MAX_POINT_LEN} 字符）")
        ts = parse_ts(cells["ts"], where)
        val = parse_value(cells["value"], where)
        key = (point, ts)
        if key in out:
            if out[key] != val:
                raise CsvError(
                    f"{where}同一计量点同一时刻出现不同读数"
                    f"（{value_str(out[key])} 与 {value_str(val)}），整批拒绝"
                )
            continue  # 同值重复行：幂等去重
        out[key] = val
    return out, raw_count


def parse_swaps_csv(text):
    """返回 ({(point, ts)}, 原始数据行数）。"""
    out = set()
    raw_count = 0
    for lineno, cells in _iter_rows(text, ["point", "ts"]):
        raw_count += 1
        where = f"第 {lineno} 行："
        point = cells["point"].strip()
        if not point:
            raise CsvError(f"{where}计量点为空")
        if len(point) > MAX_POINT_LEN:
            raise CsvError(f"{where}计量点名称过长（>{MAX_POINT_LEN} 字符）")
        ts = parse_ts(cells["ts"], where)
        key = (point, ts)
        if key in out:
            continue  # 重复行：幂等去重
        out.add(key)
    return out, raw_count


# ---------------------------------------------------------------- 归零换算

# 1 小时的微秒数；用整数微秒做精确换算，避免浮点误差
US_PER_HOUR = 3_600 * 1_000_000


def _microseconds(t1, t2):
    """两个规范化 UTC 时刻之间的微秒数（t2 > t1，精确整数）。"""
    dt1 = datetime.strptime(t1, TS_FORMAT)
    dt2 = datetime.strptime(t2, TS_FORMAT)
    td = dt2 - dt1
    return td.days * 86_400 * 1_000_000 + td.seconds * 1_000_000 + td.microseconds


def hours_between(t1, t2):
    """区间小时数（Decimal，仅用于展示；判定走精确整数微秒换算）。"""
    return Decimal(_microseconds(t1, t2)) / Decimal(US_PER_HOUR)


def rollover_bounds(v1, v2, rollover, max_power, microseconds):
    """求满足 0 <= v2-v1+n*R <= P*h 的非负整数 n 的范围。

    用微秒整数交叉相乘保持精确：h = us / US_PER_HOUR，令 K = US_PER_HOUR，则
        n * R * K <= P * us - (v2-v1) * K
    返回 (n_min, n_max)；无解时返回 (None, None)。
    """
    k = Decimal(US_PER_HOUR)
    d = v2 - v1
    rk = rollover * k  # R > 0，故 rk > 0
    n_lo = ((-d * k) / rk).to_integral_value(rounding=ROUND_CEILING)
    n_hi = ((max_power * Decimal(microseconds) - d * k) / rk).to_integral_value(
        rounding=ROUND_FLOOR
    )
    if n_lo < 0:
        n_lo = Decimal(0)
    if n_hi < n_lo:
        return None, None
    return int(n_lo), int(n_hi)


def get_point_config(conn, point):
    """读取计量点配置；未配置返回 None。"""
    row = conn.execute(
        "SELECT rollover, max_power FROM configs WHERE point = ?", (point,)
    ).fetchone()
    if row is None:
        return None
    return {
        "rollover": Decimal(row["rollover"]),
        "max_power": Decimal(row["max_power"]),
    }


def make_interval(t1, t2, v1, v2, gap, config):
    """构造一个区间 dict。

    status 取值：
      gap        换表缺口，不推算；
      unique     归零次数唯一（未配置点固定 n=0），delta 计入用量；
      pending    多个可行归零次数，待核不计；
      infeasible 无任何可行归零次数（冲突）。
    """
    it = {
        "start": t1,
        "end": t2,
        "v1": v1,
        "v2": v2,
        "gap": gap,
        "status": "gap" if gap else "unique",
        "delta": None if gap else v2 - v1,
        "n": None if gap else 0,
        "n_min": None,
        "n_max": None,
        "min_delta": None,
        "max_delta": None,
        "hours": None,
    }
    if gap or config is None:
        return it
    us = _microseconds(t1, t2)
    hours = Decimal(us) / Decimal(US_PER_HOUR)
    it["hours"] = hours
    n_lo, n_hi = rollover_bounds(
        v1, v2, config["rollover"], config["max_power"], us
    )
    d = v2 - v1
    if n_lo is None:
        it["status"] = "infeasible"
        it["delta"] = None
        it["n"] = None
    elif n_lo == n_hi:
        it["status"] = "unique"
        it["n"] = n_lo
        it["delta"] = d + Decimal(n_lo) * config["rollover"]
    else:
        it["status"] = "pending"
        it["delta"] = None
        it["n"] = None
        it["n_min"] = n_lo
        it["n_max"] = n_hi
        it["min_delta"] = d + Decimal(n_lo) * config["rollover"]
        it["max_delta"] = d + Decimal(n_hi) * config["rollover"]
    return it


def build_intervals(readings, swaps, config):
    """readings: [(ts, Decimal)] 有序；swaps: [ts] 有序。"""
    intervals = []
    prev = None
    for ts, val in readings:
        if prev is not None:
            t1, v1 = prev
            i = bisect.bisect_right(swaps, t1)
            gap = i < len(swaps) and swaps[i] <= ts  # 换表落在 (t1, t2]，含后次时刻
            intervals.append(make_interval(t1, ts, v1, val, gap, config))
        prev = (ts, val)
    return intervals


def point_state(conn, point):
    """读取一个计量点的全部读数/换表/配置，计算相邻读数区间。

    返回 (readings, swaps, config, intervals)。
    """
    readings = conn.execute(
        "SELECT ts, value FROM readings WHERE point = ? ORDER BY ts", (point,)
    ).fetchall()
    swaps = [
        r["ts"]
        for r in conn.execute(
            "SELECT ts FROM swaps WHERE point = ? ORDER BY ts", (point,)
        ).fetchall()
    ]
    config = get_point_config(conn, point)
    intervals = build_intervals(
        [(r["ts"], Decimal(r["value"])) for r in readings], swaps, config
    )
    return readings, swaps, config, intervals


# ---------------------------------------------------------------- 冲突校验

def _reading_out_of_bound(point, ts, val, config):
    if val >= config["rollover"]:
        return (
            f"计量点 {point}：{fmt_ts(ts)} 读数 {value_str(val)} 不小于归零电量 "
            f"{value_str(config['rollover'])} kWh（表盘读数须小于归零电量）"
        )
    return None


def describe_infeasible(point, it, config):
    cap = config["max_power"] * it["hours"]
    return (
        f"计量点 {point}：区间 {fmt_ts(it['start'])} ~ {fmt_ts(it['end'])} 无解"
        f"（读数 {value_str(it['v1'])}→{value_str(it['v2'])}，间隔 "
        f"{value_str(it['hours'])} 小时，增量上限 {value_str(config['max_power'])}×"
        f"{value_str(it['hours'])}={value_str(cap)} kWh，任何归零次数均不满足）"
    )


def validate_point_full(conn, point, config=None):
    """全量校验一个已配置计量点：读数越界或区间无解 -> 冲突描述列表。

    配置变更时使用：配置对历史和新读数都生效，需扫描该点全部明细。
    """
    config = config or get_point_config(conn, point)
    readings = conn.execute(
        "SELECT ts, value FROM readings WHERE point = ? ORDER BY ts", (point,)
    ).fetchall()
    errors = []
    for r in readings:
        msg = _reading_out_of_bound(point, r["ts"], Decimal(r["value"]), config)
        if msg:
            errors.append(msg)
    if errors:
        return errors  # 越界已足以拒绝，无需再算区间
    swaps = [
        r["ts"]
        for r in conn.execute(
            "SELECT ts FROM swaps WHERE point = ? ORDER BY ts", (point,)
        ).fetchall()
    ]
    intervals = build_intervals(
        [(r["ts"], Decimal(r["value"])) for r in readings], swaps, config
    )
    return [
        describe_infeasible(point, it, config)
        for it in intervals
        if it["status"] == "infeasible"
    ]


def validate_affected(conn, point, anchors, config):
    """补传读数后只校验受影响区间所需的历史明细。

    anchors 为本批次新插入的读数时刻；每个新读数至多影响它参与的两个相邻区间，
    因此只查询每个锚点的前驱/后继读数，不扫描该点全部历史。
    """
    errors = []
    pairs = {}  # (t1, t2) -> (v1, v2)，仅收集受影响区间
    for ts in anchors:
        if config is not None:
            row = conn.execute(
                "SELECT value FROM readings WHERE point = ? AND ts = ?",
                (point, ts),
            ).fetchone()
            msg = _reading_out_of_bound(point, ts, Decimal(row["value"]), config)
            if msg:
                errors.append(msg)
        prev = conn.execute(
            "SELECT ts, value FROM readings WHERE point = ? AND ts < ?"
            " ORDER BY ts DESC LIMIT 1",
            (point, ts),
        ).fetchone()
        nxt = conn.execute(
            "SELECT ts, value FROM readings WHERE point = ? AND ts > ?"
            " ORDER BY ts ASC LIMIT 1",
            (point, ts),
        ).fetchone()
        if prev is not None:
            pairs[(prev["ts"], ts)] = (Decimal(prev["value"]), None)
        if nxt is not None:
            pairs[(ts, nxt["ts"])] = (None, Decimal(nxt["value"]))
    if errors:
        return errors  # 读数越界直接拒绝

    def _value(ts):
        return Decimal(
            conn.execute(
                "SELECT value FROM readings WHERE point = ? AND ts = ?",
                (point, ts),
            ).fetchone()["value"]
        )

    for (t1, t2), (maybe_v1, maybe_v2) in sorted(pairs.items()):
        v1 = maybe_v1 if maybe_v1 is not None else _value(t1)
        v2 = maybe_v2 if maybe_v2 is not None else _value(t2)
        gap = conn.execute(
            "SELECT 1 FROM swaps WHERE point = ? AND ts > ? AND ts <= ? LIMIT 1",
            (point, t1, t2),
        ).fetchone()
        if gap is not None:
            continue  # 换表缺口不推算增量
        if config is None:
            if v2 < v1:  # 未配置点沿用原规则：读数倒退即冲突
                errors.append(
                    f"计量点 {point}：{fmt_ts(t1)} 读数 {value_str(v1)} 大于 "
                    f"{fmt_ts(t2)} 读数 {value_str(v2)}（读数倒退 {value_str(v1 - v2)} "
                    f"kWh），整批拒绝"
                )
            continue
        it = make_interval(t1, t2, v1, v2, False, config)
        if it["status"] == "infeasible":
            errors.append(describe_infeasible(point, it, config))
    return errors


# ---------------------------------------------------------------- 用量查询

def usage_range(conn, point, start_day, end_day):
    """计算 [start_day, end_day]（YYYY-MM-DD，含两端）逐日用量、缺口与待核区间。

    返回 (days, gaps, pending, total, config)：
      days    — [{'day', 'kwh', 'gap', 'pending'}]，范围内每天一行；
      gaps    — 与范围相交的换表缺口 [{'start', 'end'}]；
      pending — 与范围相交的待核/无解区间（不计入用量）；
      total   — 范围内已核算用量合计（Decimal，待核区间不计）；
      config  — 该计量点当前归零配置（未配置为 None）。
    """
    _, _, config, intervals = point_state(conn, point)
    lo = start_day + "T00:00:00.000000Z"
    hi = (parse_day(end_day) + timedelta(days=1)).strftime(
        "%Y-%m-%dT00:00:00.000000Z"
    )
    daily = defaultdict(Decimal)
    gaps = []
    pending = []
    pending_days = set()
    for it in intervals:
        intersects = it["end"] >= lo and it["start"] < hi
        if it["status"] == "gap":
            if intersects:
                gaps.append({"start": it["start"], "end": it["end"]})
        elif it["status"] == "unique":
            day = it["end"][:10]  # 增量计入后次读数所属 UTC 日
            if start_day <= day <= end_day:
                daily[day] += it["delta"]
        elif intersects:  # pending / infeasible：待核，不计入用量
            entry = {
                "start": it["start"],
                "end": it["end"],
                "v1": it["v1"],
                "v2": it["v2"],
                "resolution": it["status"],
                "n_min": it["n_min"],
                "n_max": it["n_max"],
                "min_delta": it["min_delta"],
                "max_delta": it["max_delta"],
            }
            pending.append(entry)
            pending_days.add(it["end"][:10])

    def gap_on(day):
        d_lo = day + "T00:00:00.000000Z"
        d_hi = (parse_day(day) + timedelta(days=1)).strftime(
            "%Y-%m-%dT00:00:00.000000Z"
        )
        return any(g["end"] >= d_lo and g["start"] < d_hi for g in gaps)

    days = []
    d = parse_day(start_day)
    last = parse_day(end_day)
    while d <= last:
        key = d.strftime("%Y-%m-%d")
        days.append(
            {
                "day": key,
                "kwh": daily.get(key, Decimal(0)),
                "gap": gap_on(key),
                "pending": key in pending_days,
            }
        )
        d += timedelta(days=1)
    total = sum((x["kwh"] for x in days), Decimal(0))
    return days, gaps, pending, total, config


# ---------------------------------------------------------------- 批量导入
# 以下函数在调用方开启的事务内执行；抛出 BatchRejected 时由调用方回滚。

def apply_readings(conn, upload_id, mapping):
    """写入读数。返回 (新增条数, 幂等跳过条数)。

    - 同点同刻同值：幂等跳过，不重复计数；
    - 同点同刻不同值：BatchRejected，整批回滚；
    - 未配置点：受影响非缺口区间读数倒退 -> 整批拒绝（原规则）；
    - 已配置点：新读数越界，或受影响区间归零次数无解 -> 整批拒绝；
      校验只读取受影响区间所需的前驱/后继历史明细。
    """
    applied = skipped = 0
    affected = {}  # point -> {新插入时刻}
    for (point, ts), val in mapping.items():
        row = conn.execute(
            "SELECT value FROM readings WHERE point = ? AND ts = ?", (point, ts)
        ).fetchone()
        if row is not None:
            if Decimal(row["value"]) == val:
                skipped += 1
                continue
            raise BatchRejected(
                f"计量点 {point} 在 {fmt_ts(ts)} 已存在不同读数"
                f"（现有 {row['value']}，本次 {value_str(val)}），整批拒绝"
            )
        conn.execute(
            "INSERT INTO readings(point, ts, value, upload_id) VALUES (?,?,?,?)",
            (point, ts, value_str(val), upload_id),
        )
        applied += 1
        affected.setdefault(point, set()).add(ts)

    errors = []
    for point in sorted(affected):
        config = get_point_config(conn, point)
        errors.extend(
            validate_affected(conn, point, affected[point], config)
        )
    if errors:
        raise BatchRejected("整批拒绝：" + "；".join(errors[:5]))
    return applied, skipped


def apply_swaps(conn, upload_id, keys):
    """写入换表记录。返回 (新增条数, 幂等跳过条数)。

    换表只会把区间标记为缺口（撤销已计入增量），不会产生倒退或无解，无需额外校验。
    """
    applied = skipped = 0
    for point, ts in sorted(keys):
        row = conn.execute(
            "SELECT 1 FROM swaps WHERE point = ? AND ts = ?", (point, ts)
        ).fetchone()
        if row is not None:
            skipped += 1
            continue
        conn.execute(
            "INSERT INTO swaps(point, ts, upload_id) VALUES (?,?,?)",
            (point, ts, upload_id),
        )
        applied += 1
    return applied, skipped


def apply_config(conn, upload_id, point, rollover, max_power):
    """设置/修改计量点归零配置（upsert）。

    配置对历史和新读数都生效：先按提案配置全量校验该点既有明细，
    任一读数越界或区间无解即 BatchRejected（由调用方回滚，保留原配置与明细）。
    """
    config = {"rollover": rollover, "max_power": max_power}
    errors = validate_point_full(conn, point, config)
    if errors:
        raise BatchRejected(
            "配置冲突，拒绝本次配置变更：" + "；".join(errors[:5])
        )
    conn.execute(
        "INSERT INTO configs(point, rollover, max_power, upload_id, updated_at)"
        " VALUES (?,?,?,?,?)"
        " ON CONFLICT(point) DO UPDATE SET"
        " rollover = excluded.rollover, max_power = excluded.max_power,"
        " upload_id = excluded.upload_id, updated_at = excluded.updated_at",
        (point, value_str(rollover), value_str(max_power), upload_id, now_ts()),
    )
