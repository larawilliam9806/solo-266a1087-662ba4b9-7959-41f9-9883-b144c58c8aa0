"""归零电量 / 最大功率配置相关规则测试。"""
import io
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import create_app, logic  # noqa: E402
from decimal import Decimal  # noqa: E402


@pytest.fixture()
def app(tmp_path):
    return create_app(
        {"TESTING": True, "DATABASE": str(tmp_path / "test.db"), "SECRET_KEY": "t"}
    )


@pytest.fixture()
def client(app):
    return app.test_client()


def upload(client, kind, csv_text, filename="data.csv"):
    data = {"file": (io.BytesIO(csv_text.encode("utf-8")), filename)}
    return client.post(
        f"/upload/{kind}", data=data, content_type="multipart/form-data",
        follow_redirects=True,
    )


def set_config(client, point, rollover, max_power):
    return client.post(
        "/config",
        data={"point": point, "rollover": str(rollover), "max_power": str(max_power)},
        follow_redirects=True,
    )


def usage(client, point, start, end):
    resp = client.get(f"/api/usage?point={point}&start={start}&end={end}")
    assert resp.status_code == 200
    return resp.get_json()


def day_kwh(result, day):
    return next(d["kwh"] for d in result["days"] if d["day"] == day)


# ---------------------------------------------------------------- 配置基本校验

def test_config_requires_positive_numbers(client):
    for bad in ("0", "-1", "abc", ""):
        resp = set_config(client, "A", bad, "10")
        assert "配置被拒绝" in resp.get_data(as_text=True)
    resp = set_config(client, "A", "1000", "0")
    assert "配置被拒绝" in resp.get_data(as_text=True)
    # 没有任何成功配置：配置页可正常打开，版本不变
    assert client.get("/config").status_code == 200


def test_config_page_lists_existing_config(client):
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-02T00:00:00Z,150\n")
    set_config(client, "A", "1000", "10")
    body = client.get("/config").get_data(as_text=True)
    assert "1000" in body and "10" in body and "A" in body


# ---------------------------------------------------------------- 唯一归零次数

def test_unique_rollover_count_counted(client):
    # R=1000, P=12.5kW；900 -> 200，24 小时（cap=300）：n=1 时 delta=300（恰好上限），n=0 为负
    set_config(client, "A", "1000", "12.5")
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,900\n"
           "A,2026-01-02T00:00:00Z,200\n")
    r = usage(client, "A", "2026-01-01", "2026-01-02")
    assert r["config"] == {"rollover_kwh": "1000", "max_power_kw": "12.5"}
    assert day_kwh(r, "2026-01-02") == "300"
    assert r["pending"] == []
    assert r["gaps"] == []


def test_no_rollover_when_value_rises(client):
    # 100 -> 300，n=0 时 delta=200 落在 [0,240]，唯一解 n=0
    set_config(client, "A", "1000", "10")
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-02T00:00:00Z,300\n")
    r = usage(client, "A", "2026-01-01", "2026-01-02")
    assert day_kwh(r, "2026-01-02") == "200"


def test_two_rollovers_in_one_interval_unique(client):
    # R=1000, 48h, P=10 => cap 480；950 -> 50，d=-900：n=1 => 100（唯一可行）
    set_config(client, "A", "1000", "10")
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,950\n"
           "A,2026-01-03T00:00:00Z,50\n")
    r = usage(client, "A", "2026-01-01", "2026-01-03")
    assert day_kwh(r, "2026-01-03") == "100"
    assert r["pending"] == []


def test_power_bound_inclusive(client):
    # delta 恰好等于 P*h（端点包含）应可核算：R=1000, P=5, 24h => cap=120；100 -> 220 => 120
    set_config(client, "A", "1000", "5")
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-02T00:00:00Z,220\n")
    r = usage(client, "A", "2026-01-01", "2026-01-02")
    assert day_kwh(r, "2026-01-02") == "120"
    # 第二段超功率 -> 无解（n=0 时 delta=120.001 > 120，n>=1 更大），整批拒绝
    resp = upload(client, "readings",
                  "point,ts,value\nA,2026-01-03T00:00:00Z,340.001\n")
    assert "无解" in resp.get_data(as_text=True)
    r = usage(client, "A", "2026-01-01", "2026-01-03")
    assert r["total_kwh"] == "120"                    # 被拒批次未生效


# ---------------------------------------------------------------- 待核区间

def test_ambiguous_rollover_pending_not_counted(client):
    # R=1000, P=100kW（cap=2400）；900 -> 200，d=-700：n=1=>300、n=2=>1300、n=3=>2300 均 <=2400
    set_config(client, "A", "1000", "100")
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,900\n"
           "A,2026-01-02T00:00:00Z,200\n")
    r = usage(client, "A", "2026-01-01", "2026-01-02")
    assert day_kwh(r, "2026-01-02") == "0"        # 待核不计入
    assert r["total_kwh"] == "0"
    assert len(r["pending"]) == 1
    p = r["pending"][0]
    assert p["resolution"] == "pending"
    assert (p["n_min"], p["n_max"]) == (1, 3)
    assert (p["min_kwh"], p["max_kwh"]) == ("300", "2300")
    day = next(d for d in r["days"] if d["day"] == "2026-01-02")
    assert day["pending"] is True
    # 网页也展示待核
    body = client.get("/usage?point=A&start=2026-01-01&end=2026-01-02").get_data(
        as_text=True
    )
    assert "待核" in body and "1 ~ 3 次" in body


def test_pending_resolved_by_late_reading(client):
    # 同一待核场景，补传中间读数后两个子区间均唯一（12h cap=1200）
    set_config(client, "A", "1000", "100")
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,900\n"
           "A,2026-01-02T00:00:00Z,200\n")
    r = usage(client, "A", "2026-01-01", "2026-01-02")
    assert len(r["pending"]) == 1
    # 补传：中间读数 500。900->500：n=1=>600 唯一；500->200：n=1=>700 唯一
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T12:00:00Z,500\n")
    r = usage(client, "A", "2026-01-01", "2026-01-02")
    assert r["pending"] == []
    assert day_kwh(r, "2026-01-01") == "600"
    assert day_kwh(r, "2026-01-02") == "700"
    assert r["total_kwh"] == "1300"


def test_swap_turns_pending_into_gap(client):
    set_config(client, "A", "1000", "100")
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,900\n"
           "A,2026-01-02T00:00:00Z,200\n")
    assert len(usage(client, "A", "2026-01-01", "2026-01-02")["pending"]) == 1
    upload(client, "swaps", "point,ts\nA,2026-01-01T12:00:00Z\n")
    r = usage(client, "A", "2026-01-01", "2026-01-02")
    assert r["pending"] == []
    assert len(r["gaps"]) == 1
    assert r["total_kwh"] == "0"


# ---------------------------------------------------------------- 越界与无解

def test_reading_out_of_bound_rejected_on_upload(client):
    set_config(client, "A", "1000", "10")
    resp = upload(client, "readings",
                  "point,ts,value\nA,2026-01-01T00:00:00Z,1000\n")  # 等于 R，越界
    body = resp.get_data(as_text=True)
    assert "不小于归零电量" in body and "整批拒绝" in body
    # 同批其它行也不生效，原配置保留
    r = usage(client, "A", "2026-01-01", "2026-01-01")
    assert r["total_kwh"] == "0" and r["config"] is not None


def test_config_change_rejected_on_history_conflict(client):
    # 历史读数 1500 超过拟设 R=1000 -> 拒绝配置变更，保留原配置/明细/版本
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,1500\n"
           "A,2026-01-02T00:00:00Z,1510\n")
    v_before = usage(client, "A", "2026-01-01", "2026-01-02")["version"]
    resp = set_config(client, "A", "1000", "10")
    assert "配置冲突" in resp.get_data(as_text=True)
    r = usage(client, "A", "2026-01-01", "2026-01-02")
    assert r["config"] is None                       # 未配置点沿用原规则
    assert r["version"] == v_before                  # 版本未变
    assert day_kwh(r, "2026-01-02") == "10"          # 明细保持


def test_config_change_rejected_on_infeasible_history(client):
    # 100 -> 900，24h；R=1000, P=5（cap 120）：n=0 => 800 超功率，n>=1 更大 -> 无解
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-02T00:00:00Z,900\n")
    resp = set_config(client, "A", "1000", "5")
    assert "无解" in resp.get_data(as_text=True)
    r = usage(client, "A", "2026-01-01", "2026-01-02")
    assert r["config"] is None and day_kwh(r, "2026-01-02") == "800"


def test_upload_rejected_when_interval_infeasible(client):
    set_config(client, "A", "1000", "5")              # cap 120/24h
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n")
    resp = upload(client, "readings",
                  "point,ts,value\nA,2026-01-02T00:00:00Z,900\n")
    assert "无解" in resp.get_data(as_text=True)
    r = usage(client, "A", "2026-01-01", "2026-01-02")
    assert r["total_kwh"] == "0"                      # 整批回滚


# ---------------------------------------------------------------- 配置变更与版本

def test_reconfigure_rejudges_history_and_bumps_version(client):
    set_config(client, "A", "1000", "100")            # 先待核（n=1..3）
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,900\n"
           "A,2026-01-02T00:00:00Z,200\n")
    r1 = usage(client, "A", "2026-01-01", "2026-01-02")
    assert len(r1["pending"]) == 1
    # 收紧最大功率到 12.5kW（cap=300）：n=1 => 300 唯一；配置对历史读数重新生效
    set_config(client, "A", "1000", "12.5")
    r2 = usage(client, "A", "2026-01-01", "2026-01-02")
    assert r2["pending"] == []
    assert day_kwh(r2, "2026-01-02") == "300"
    assert r2["version"] > r1["version"]
    # 一次查询内配置、用量、待核、版本同口径
    assert r2["config"] == {"rollover_kwh": "1000", "max_power_kw": "12.5"}


def test_failed_config_keeps_old_config_and_version(client):
    set_config(client, "A", "1000", "10")
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-02T00:00:00Z,200\n")
    v_before = usage(client, "A", "2026-01-01", "2026-01-02")["version"]
    resp = set_config(client, "A", "50", "10")        # 历史读数 100/200 越界
    assert "拒绝" in resp.get_data(as_text=True)
    r = usage(client, "A", "2026-01-01", "2026-01-02")
    assert r["config"] == {"rollover_kwh": "1000", "max_power_kw": "10"}
    assert r["version"] == v_before
    assert day_kwh(r, "2026-01-02") == "100"


# ---------------------------------------------------------------- 补传只查受影响区间

def test_backfill_only_queries_affected_history(app, client, monkeypatch):
    import sqlite3
    set_config(client, "A", "1000", "10")
    lines = ["point,ts,value"]
    for i in range(12):
        lines.append(f"A,2026-01-{i+1:02d}T00:00:00Z,{100 + i}")
    upload(client, "readings", "\n".join(lines) + "\n")

    import app.db as dbmod
    full_scans = {"n": 0}
    real_connect = sqlite3.connect

    class CountingConnection(sqlite3.Connection):
        def execute(self, sql, params=()):
            if "FROM readings WHERE point = ? ORDER BY ts" in sql:
                full_scans["n"] += 1
            return super().execute(sql, params)

    def counting_connect(*args, **kwargs):
        kwargs["factory"] = CountingConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(dbmod.sqlite3, "connect", counting_connect)
    # 在中段补传一个读数：只应触发受影响区间的局部查询，不应全量扫描该点历史
    upload(client, "readings",
           "point,ts,value\nA,2026-01-05T12:00:00Z,104.5\n")
    assert full_scans["n"] == 0
    r = usage(client, "A", "2026-01-01", "2026-01-12")
    assert r["pending"] == []


# ---------------------------------------------------------------- 未配置点不受影响

def test_unconfigured_point_keeps_original_rules(client):
    # 未配置点的倒退读数按原规则整批拒绝
    resp = upload(client, "readings",
                  "point,ts,value\nB,2026-01-01T00:00:00Z,900\n"
                  "B,2026-01-02T00:00:00Z,200\n")
    assert "读数倒退" in resp.get_data(as_text=True)
    r = usage(client, "B", "2026-01-01", "2026-01-02")
    assert r["config"] is None
    assert r["total_kwh"] == "0"                  # 被拒批次未入库


def test_two_points_one_configured_one_not(client):
    # A 配置归零；B 不配置，两者互不干扰
    set_config(client, "A", "1000", "12.5")
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,900\n"
           "A,2026-01-02T00:00:00Z,200\n"
           "B,2026-01-01T00:00:00Z,50\n"
           "B,2026-01-02T00:00:00Z,80\n")
    ra = usage(client, "A", "2026-01-01", "2026-01-02")
    rb = usage(client, "B", "2026-01-01", "2026-01-02")
    assert day_kwh(ra, "2026-01-02") == "300" and ra["config"] is not None
    assert day_kwh(rb, "2026-01-02") == "30" and rb["config"] is None

# ---------------------------------------------------------------- 交错并发

def test_concurrent_config_and_uploads(app):
    """配置变更与上传交错：每个生效操作都是完整串行事务，结果始终自洽。"""
    client = app.test_client()
    upload(client, "readings",
           "point,ts,value\nA,2026-01-01T00:00:00Z,100\n"
           "A,2026-01-02T00:00:00Z,200\n")
    errors = []

    def do_config():
        c = app.test_client()
        try:
            resp = set_config(c, "A", "1000", "10")
            if resp.status_code != 200:
                errors.append(f"config status {resp.status_code}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"config {e}")

    def do_upload():
        c = app.test_client()
        try:
            resp = upload(
                c, "readings",
                "point,ts,value\nA,2026-01-03T00:00:00Z,300\n"
                "A,2026-01-04T00:00:00Z,400\n",
            )
            if resp.status_code != 200:
                errors.append(f"upload status {resp.status_code}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"upload {e}")

    def do_query():
        c = app.test_client()
        for _ in range(30):
            try:
                r = usage(c, "A", "2026-01-01", "2026-01-04")
                # 配置出现后查询必须带同一版 config；用量与待核必须自洽
                if r["config"] is not None:
                    assert r["config"]["rollover_kwh"] == "1000"
                # 400-100=300 始终是唯一解 n=0 的核算量（若全部生效）
                assert r["total_kwh"] in ("0", "100", "200", "300")
                assert isinstance(r["version"], int)
            except Exception as e:  # noqa: BLE001
                errors.append(f"query {e}")
                return

    threads = ([threading.Thread(target=do_config) for _ in range(3)]
               + [threading.Thread(target=do_upload) for _ in range(3)]
               + [threading.Thread(target=do_query) for _ in range(3)])
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors

    r = usage(app.test_client(), "A", "2026-01-01", "2026-01-04")
    assert r["config"] == {"rollover_kwh": "1000", "max_power_kw": "10"}
    assert r["total_kwh"] == "300"
    assert r["pending"] == []


# ---------------------------------------------------------------- 批次边界

def test_batch_with_adjacent_new_readings(client):
    # 同批插入多条相邻读数：锚点共享邻接区间，全部受影响区间都要校验
    set_config(client, "A", "1000", "10")
    csv_text = (
        "point,ts,value\n"
        "A,2026-01-01T00:00:00Z,100\n"
        "A,2026-01-02T00:00:00Z,110\n"
        "A,2026-01-03T00:00:00Z,120\n"
        "A,2026-01-04T00:00:00Z,130\n"
    )
    resp = upload(client, "readings", csv_text)
    assert "上传成功" in resp.get_data(as_text=True)
    r = usage(client, "A", "2026-01-01", "2026-01-04")
    assert r["total_kwh"] == "30" and r["pending"] == []
    # 幂等重传：全部跳过，用量不变
    assert "重复跳过 4 条" in upload(client, "readings", csv_text).get_data(
        as_text=True
    )
    assert usage(client, "A", "2026-01-01", "2026-01-04")["total_kwh"] == "30"


def test_batch_rolls_back_when_one_interval_infeasible(client):
    set_config(client, "B", "1000", "10")        # 24h cap 240
    resp = upload(
        client, "readings",
        "point,ts,value\nB,2026-01-01T00:00:00Z,100\n"
        "B,2026-01-02T00:00:00Z,110\n"            # 合法
        "B,2026-01-03T00:00:00Z,900\n",           # 790 > 240 无解
    )
    assert "无解" in resp.get_data(as_text=True)
    r = usage(client, "B", "2026-01-01", "2026-01-03")
    assert r["total_kwh"] == "0"                  # 同批合法行一并回滚


# ---------------------------------------------------------------- 纯函数边界
def test_rollover_bounds_math():
    R, P = Decimal("1000"), Decimal("10")
    us = 24 * 3600 * 1_000_000
    # 900 -> 200，cap=240：n=1 => 300 超功率 -> 无解
    assert logic.rollover_bounds(Decimal("900"), Decimal("200"), R, P, us) == (None, None)
    # P=12.5 cap=300：n=1 => 300 恰好端点，唯一
    assert logic.rollover_bounds(
        Decimal("900"), Decimal("200"), R, Decimal("12.5"), us
    ) == (1, 1)
    # P=100 cap 2400：n=1..3
    assert logic.rollover_bounds(
        Decimal("900"), Decimal("200"), R, Decimal("100"), us
    ) == (1, 3)
    # 100 -> 220, P=5 cap 120：n=0 恰好端点
    assert logic.rollover_bounds(
        Decimal("100"), Decimal("220"), R, Decimal("5"), us
    ) == (0, 0)
    # 非整点小时也精确：1.5h, P=10 cap 15；100->110 => 10，n=0
    us15 = int(1.5 * 3600 * 1_000_000)
    assert logic.rollover_bounds(
        Decimal("100"), Decimal("110"), R, Decimal("10"), us15
    ) == (0, 0)
