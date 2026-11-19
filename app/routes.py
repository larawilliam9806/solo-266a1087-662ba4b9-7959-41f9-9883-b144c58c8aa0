import sqlite3
from datetime import timedelta

from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import logic
from .db import get_db

bp = Blueprint("main", __name__)

KIND_NAMES = {"readings": "读数", "swaps": "换表", "config": "归零配置"}


def data_version(conn):
    """数据版本号 = 最近一个生效批次 id（含读数、换表与配置变更）。

    所有写操作都在 BEGIN IMMEDIATE 串行事务内提交，查询在单个读事务内完成，
    因此一次查询看到的用量、待核区间与版本号必然对应同一组配置及明细。
    """
    return conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS v FROM uploads WHERE status = 'applied'"
    ).fetchone()["v"]


def list_points(conn):
    return [
        r["point"]
        for r in conn.execute(
            "SELECT point FROM ("
            "  SELECT point FROM readings UNION SELECT point FROM swaps"
            "  UNION SELECT point FROM configs"
            ") ORDER BY point"
        )
    ]


def list_configs(conn):
    return conn.execute(
        "SELECT point, rollover, max_power, updated_at FROM configs ORDER BY point"
    ).fetchall()


def record_rejected(conn, kind, filename, message, rows_received):
    """被拒批次单独落一条审计记录（不影响既有数据）；审计失败不掩盖原始错误。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO uploads(kind, filename, status, message, rows_received,"
            " rows_applied, created_at) VALUES (?,?,?,?,?,0,?)",
            (kind, filename, "rejected", message, rows_received, logic.now_ts()),
        )
        conn.execute("COMMIT")
    except sqlite3.Error:
        conn.execute("ROLLBACK")


@bp.route("/")
def index():
    conn = get_db()
    conn.execute("BEGIN")  # 单个读事务：页面看到的是同一版快照
    try:
        points = list_points(conn)
        configs = list_configs(conn)
        uploads = conn.execute(
            "SELECT * FROM uploads ORDER BY id DESC LIMIT 50"
        ).fetchall()
        version = data_version(conn)
    finally:
        conn.execute("COMMIT")
    return render_template(
        "index.html",
        points=points,
        configs=configs,
        uploads=uploads,
        version=version,
        kind_names=KIND_NAMES,
    )


@bp.route("/upload/<kind>", methods=["POST"])
def upload(kind):
    if kind not in ("readings", "swaps"):
        abort(404)
    f = request.files.get("file")
    if f is None or not f.filename:
        flash("请选择要上传的 CSV 文件", "error")
        return redirect(url_for("main.index"))
    try:
        text = f.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        flash("上传被拒绝：文件编码须为 UTF-8", "error")
        return redirect(url_for("main.index"))

    conn = get_db()
    # 1) 解析与格式校验（不进事务）
    try:
        if kind == "readings":
            parsed, raw_count = logic.parse_readings_csv(text)
        else:
            parsed, raw_count = logic.parse_swaps_csv(text)
    except logic.CsvError as e:
        record_rejected(conn, kind, f.filename, str(e), 0)
        flash(f"上传被拒绝：{e}", "error")
        return redirect(url_for("main.index"))

    # 2) 单个写事务：原始记录与受影响结果一起生效；失败整体回滚，保持旧数据
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO uploads(kind, filename, status, message, rows_received,"
            " rows_applied, created_at) VALUES (?,?,?,?,?,0,?)",
            (kind, f.filename, "applied", "", raw_count, logic.now_ts()),
        )
        upload_id = cur.lastrowid
        if kind == "readings":
            applied, skipped = logic.apply_readings(conn, upload_id, parsed)
        else:
            applied, skipped = logic.apply_swaps(conn, upload_id, parsed)
        msg = f"接收 {raw_count} 行，新增 {applied} 条，重复跳过 {skipped} 条"
        conn.execute(
            "UPDATE uploads SET rows_applied = ?, message = ? WHERE id = ?",
            (applied, msg, upload_id),
        )
        conn.execute("COMMIT")
        flash(f"{KIND_NAMES[kind]}上传成功：{msg}", "success")
    except logic.BatchRejected as e:
        conn.execute("ROLLBACK")
        record_rejected(conn, kind, f.filename, str(e), raw_count)
        flash(f"上传被拒绝：{e}", "error")
    except sqlite3.OperationalError as e:
        conn.execute("ROLLBACK")
        flash(f"系统繁忙，请重试（{e}）", "error")
    return redirect(url_for("main.index"))


@bp.route("/config", methods=["GET", "POST"])
def config():
    """设置/修改计量点的归零电量（kWh）与最大功率（kW），两者均为正数。"""
    conn = get_db()
    if request.method == "POST":
        point = (request.form.get("point") or "").strip()
        rollover_raw = (request.form.get("rollover") or "").strip()
        max_power_raw = (request.form.get("max_power") or "").strip()
        if not point:
            flash("配置被拒绝：计量点不能为空", "error")
            return redirect(url_for("main.config"))
        if len(point) > logic.MAX_POINT_LEN:
            flash(f"配置被拒绝：计量点名称过长（>{logic.MAX_POINT_LEN} 字符）", "error")
            return redirect(url_for("main.config"))
        try:
            rollover = logic.parse_positive(rollover_raw, "归零电量")
            max_power = logic.parse_positive(max_power_raw, "最大功率")
        except ValueError as e:
            # 表单级错误同样留审计记录，且不触碰任何既有配置/明细
            record_rejected(conn, "config", point, str(e), 0)
            flash(f"配置被拒绝：{e}", "error")
            return redirect(url_for("main.config"))

        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "INSERT INTO uploads(kind, filename, status, message, rows_received,"
                " rows_applied, created_at) VALUES (?,?,?,?,?,0,?)",
                ("config", point, "applied", "", 1, logic.now_ts()),
            )
            upload_id = cur.lastrowid
            logic.apply_config(conn, upload_id, point, rollover, max_power)
            conn.execute("COMMIT")
            flash(
                f"计量点 {point} 归零配置已生效：归零电量 "
                f"{logic.fmt_num(rollover)} kWh，最大功率 {logic.fmt_num(max_power)} kW"
                f"（数据版本 v{upload_id}）",
                "success",
            )
        except logic.BatchRejected as e:
            conn.execute("ROLLBACK")
            record_rejected(conn, "config", point, str(e), 1)
            flash(str(e), "error")
        except sqlite3.OperationalError as e:
            conn.execute("ROLLBACK")
            flash(f"系统繁忙，请重试（{e}）", "error")
        return redirect(url_for("main.config"))

    conn.execute("BEGIN")
    try:
        points = list_points(conn)
        configs = list_configs(conn)
        version = data_version(conn)
        preselect = (request.args.get("point") or "").strip()
    finally:
        conn.execute("COMMIT")
    return render_template(
        "config.html",
        points=points,
        configs=configs,
        version=version,
        preselect=preselect,
    )


def _resolve_range(conn, point, start, end):
    """校验/补全日期范围，返回 (start_day, end_day)。缺省时取该点数据最近 31 天。"""
    if start and end:
        d0, d1 = logic.parse_day(start), logic.parse_day(end)
    else:
        row = conn.execute(
            "SELECT MIN(substr(ts,1,10)) AS lo, MAX(substr(ts,1,10)) AS hi"
            " FROM readings WHERE point = ?",
            (point,),
        ).fetchone()
        if row is None or row["lo"] is None:
            raise ValueError("该计量点暂无读数，请指定日期范围")
        d1 = logic.parse_day(end) if end else logic.parse_day(row["hi"])
        d0 = logic.parse_day(start) if start else logic.parse_day(row["lo"])
        if (d1 - d0).days > 30:
            d0 = d1 - timedelta(days=30)
    if d1 < d0:
        raise ValueError("结束日期早于开始日期")
    if (d1 - d0).days > logic.MAX_RANGE_DAYS:
        raise ValueError(f"日期范围过大（最多 {logic.MAX_RANGE_DAYS} 天）")
    return d0.strftime("%Y-%m-%d"), d1.strftime("%Y-%m-%d")


def _pending_json(items, config):
    out = []
    for p in items:
        entry = {
            "start": p["start"],
            "end": p["end"],
            "v1": logic.fmt_num(p["v1"]),
            "v2": logic.fmt_num(p["v2"]),
            "resolution": p["resolution"],  # pending | infeasible
        }
        if p["resolution"] == "pending":
            entry.update(
                n_min=p["n_min"],
                n_max=p["n_max"],
                min_kwh=logic.fmt_num(p["min_delta"]),
                max_kwh=logic.fmt_num(p["max_delta"]),
            )
        else:
            entry.update(n_min=None, n_max=None, min_kwh=None, max_kwh=None)
        out.append(entry)
    return out


@bp.route("/usage")
def usage():
    point = request.args.get("point", "").strip()
    start = request.args.get("start", "").strip()
    end = request.args.get("end", "").strip()
    conn = get_db()
    conn.execute("BEGIN")  # 单个读事务：同一版完整结果
    try:
        points = list_points(conn)
        version = data_version(conn)
        days = gaps = pending = total = cfg = None
        error = None
        if point:
            try:
                start, end = _resolve_range(conn, point, start, end)
                days, gaps, pending, total, cfg = logic.usage_range(
                    conn, point, start, end
                )
            except ValueError as e:
                error = str(e)
    finally:
        conn.execute("COMMIT")
    return render_template(
        "usage.html",
        points=points,
        point=point,
        start=start,
        end=end,
        days=days,
        gaps=gaps,
        pending=pending,
        total=total,
        cfg=cfg,
        error=error,
        version=version,
    )


@bp.route("/api/usage")
def api_usage():
    point = request.args.get("point", "").strip()
    start = request.args.get("start", "").strip()
    end = request.args.get("end", "").strip()
    if not (point and start and end):
        return jsonify(error="需要 point、start、end 参数（日期为 YYYY-MM-DD）"), 400
    conn = get_db()
    conn.execute("BEGIN")
    try:
        try:
            start, end = _resolve_range(conn, point, start, end)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        days, gaps, pending, total, cfg = logic.usage_range(
            conn, point, start, end
        )
        version = data_version(conn)
    finally:
        conn.execute("COMMIT")
    return jsonify(
        point=point,
        start=start,
        end=end,
        version=version,
        config=(
            None
            if cfg is None
            else {
                "rollover_kwh": logic.fmt_num(cfg["rollover"]),
                "max_power_kw": logic.fmt_num(cfg["max_power"]),
            }
        ),
        total_kwh=logic.fmt_num(total),
        days=[
            {
                "day": d["day"],
                "kwh": logic.fmt_num(d["kwh"]),
                "gap": d["gap"],
                "pending": d["pending"],
            }
            for d in days
        ],
        gaps=[{"start": g["start"], "end": g["end"]} for g in gaps],
        pending=_pending_json(pending, cfg),
    )
