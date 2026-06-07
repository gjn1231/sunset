#!/usr/bin/env python3
"""
晚霞预测 — 双引擎日落质量预报
================================

引擎 1 (主): Sunsethue API — 专业日落质量模型，需要 API key
引擎 2 (兜底): Open-Meteo — 免费天气 API + 小时级云量评分算法

位置解析: 手动配置 > 城市名映射 > 时区兜底

用法:
    python3 predict-sunset.py                    # 默认位置（杭州）
    python3 predict-sunset.py --location 北京     # 指定城市
    python3 predict-sunset.py --lat 30.27 --lng 120.15  # 精确坐标
    python3 predict-sunset.py --date 2026-05-07  # 指定日期
    python3 predict-sunset.py --type sunrise     # 朝霞预测
    python3 predict-sunset.py --discord          # Discord 格式输出

环境变量:
    SUNSETHUE_API_KEY    — Sunsethue API key（可选）
    SUNSET_LOCATION      — 默认城市名（可选，默认 "杭州"）
"""

import json
import math
import os
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

# Fix Windows console encoding for emoji output (🌅🔥☁️ etc.)
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

# ── 城市坐标映射 ─────────────────────────────────────────────

LOCATION_MAP = {
    "杭州": (30.2741, 120.1551),
    "北京": (39.9042, 116.4074),
    "上海": (31.2304, 121.4737),
    "深圳": (22.5431, 114.0579),
    "广州": (23.1291, 113.2644),
    "成都": (30.5728, 104.0668),
    "南京": (32.0603, 118.7969),
    "苏州": (31.2990, 120.5853),
    "武汉": (30.5928, 114.3055),
    "长沙": (28.2282, 112.9388),
    "重庆": (29.4316, 106.9123),
    "开封": (34.8200, 114.3100),   # 河南大学金明校区
    "林州": (36.0830, 113.8190),
    "西安": (34.3416, 108.9398),
    "厦门": (24.4798, 118.0894),
    "青岛": (36.0671, 120.3826),
    "大连": (38.9140, 121.6147),
    "昆明": (25.0389, 102.7183),
    "拉萨": (29.6500, 91.1000),
    "丽江": (26.8721, 100.2299),
    "三亚": (18.2528, 109.5120),
    "香港": (22.3193, 114.1694),
    "台北": (25.0330, 121.5654),
}

# 杭州日落摄影佳位
HANGZHOU_SPOTS = [
    ("西湖断桥", "经典机位，日落方向正对宝石山"),
    ("宝石山蛤蟆峰", "俯拍西湖全景+保俶塔，需提前1h爬"),
    ("龙井茶园", "茶山+晚霞，层次感强，春秋季最佳"),
    ("钱塘江边", "开阔江面倒影"),
    ("小河直街", "运河+老街+晚霞，蓝调时刻最佳"),
    ("馒头山", "老杭州生活气息"),
    ("良渚古城", "广阔田野天际线低"),
    ("白塔公园", "铁轨+樱花（春）+晚霞"),
]

TZ_CST = timezone(timedelta(hours=8), "Asia/Shanghai")


# ── 工具函数 ─────────────────────────────────────────────────

def _http_get(url, headers=None):
    """通用 HTTP GET"""
    req = urllib.request.Request(url, headers=headers or {"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"_error": str(e)}


def _hour_index_for_date(hourly_times, target_date, sunset_hour=18):
    """
    找到 target_date 当天 sunset_hour 点在 hourly 数组中的索引。
    如果 sunset_hour 不在，找最接近的下午时段（16-19 点）。
    """
    target_prefix = target_date + "T"
    candidates = []
    for i, t in enumerate(hourly_times):
        if t.startswith(target_prefix):
            h = int(t.split("T")[1].split(":")[0])
            if 16 <= h <= 19:
                candidates.append((abs(h - sunset_hour), i, h))
    if candidates:
        candidates.sort()
        return candidates[0][1]  # 最接近日落的小时索引
    return None


# ── 引擎 1：Sunsethue API ──────────────────────────────────

def predict_sunsethue(lat, lng, date_str, event_type="sunset"):
    """调用 Sunsethue API 获取日落质量预测"""
    api_key = os.environ.get("SUNSETHUE_API_KEY", "")
    if not api_key:
        return None

    params = urllib.parse.urlencode({
        "latitude": lat, "longitude": lng,
        "date": date_str, "type": event_type,
    })
    url = f"https://api.sunsethue.com/event?{params}&key={api_key}"
    data = _http_get(url)
    if "_error" in data:
        return {"error": data["_error"], "source": "sunsethue"}
    return data


# ── 黄金/蓝调时刻（太阳高度角天文算法） ─────────────────

def _julian_day(year, month, day):
    """计算儒略日，返回 UT 子夜 (0h UT) 的 JD（民用日约定，.0=子夜）"""
    if month <= 2:
        year -= 1
        month += 12
    A = year // 100
    B = 2 - A + A // 4
    # +0.5 将天文 JD (.0=正午) 转为民用 JD (.0=子夜)
    return int(365.25 * (year + 4716)) + int(30.6001 * (month + 1)) + day + B - 1525.0


def _solar_position(lat, lng, jd):
    """
    计算太阳高度角和方位角。
    基于 NOAA Solar Calculator 算法。
    返回 (elevation_deg, azimuth_deg)
    """
    # 自 J2000.0 以来的儒略世纪数
    n = jd - 2451545.0

    # 太阳平均经度
    L = (280.460 + 0.9856474 * n) % 360

    # 太阳平均近点角
    g = (357.528 + 0.9856003 * n) % 360
    g_rad = math.radians(g)

    # 黄道经度
    lam = L + 1.915 * math.sin(g_rad) + 0.020 * math.sin(2 * g_rad)

    # 黄赤交角
    epsilon = 23.439 - 0.0000004 * n

    # 赤经和赤纬
    lam_rad = math.radians(lam)
    eps_rad = math.radians(epsilon)
    alpha = math.degrees(math.atan2(
        math.cos(eps_rad) * math.sin(lam_rad),
        math.cos(lam_rad),
    ))
    delta = math.degrees(math.asin(
        math.sin(eps_rad) * math.sin(lam_rad),
    ))

    # 格林尼治恒星时
    GST = (6.6974243242 + 0.0657098283 * n + (jd - int(jd)) * 24) % 24
    # 地方恒星时
    lst = (GST + lng / 15) % 24

    # 时角（度）
    ha = (lst * 15 - alpha + 360) % 360
    if ha > 180:
        ha -= 360
    ha_rad = math.radians(ha)

    lat_rad = math.radians(lat)
    delta_rad = math.radians(delta)

    # 太阳高度角
    sin_el = (math.sin(lat_rad) * math.sin(delta_rad) +
              math.cos(lat_rad) * math.cos(delta_rad) * math.cos(ha_rad))
    elevation = math.degrees(math.asin(max(-1, min(1, sin_el))))

    # 方位角
    cos_az = ((math.sin(delta_rad) - math.sin(lat_rad) * math.sin(math.radians(elevation))) /
              (math.cos(lat_rad) * math.cos(math.radians(elevation))))
    cos_az = max(-1, min(1, cos_az))
    azimuth = math.degrees(math.acos(cos_az))
    if ha > 0:
        azimuth = 360 - azimuth

    return elevation, azimuth


def _find_time_for_elevation(lat, lng, date_str, target_elev, after_noon=True):
    """
    用二分法查找太阳到达目标高度角的精确时刻。
    返回 "HH:MM" 字符串，找不到返回空串。
    """
    try:
        parts = date_str.split("-")
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
    except (ValueError, IndexError):
        return ""

    jd = _julian_day(y, m, d)

    # 太阳正午的 JD
    # jd 是 UT 子夜 (0h UT)，太阳正午 = 子夜 + solar_noon_ut 小时
    solar_noon_ut = 12.0 - lng / 15.0
    noon_jd = jd + solar_noon_ut / 24.0

    if after_noon:
        # 下午：太阳从正午高度下降
        lo = noon_jd
        hi = noon_jd + 0.5  # 最多到午夜
    else:
        # 上午：太阳升起到正午高度
        lo = noon_jd - 0.5
        hi = noon_jd

    best_jd = None
    for _ in range(40):
        mid = (lo + hi) / 2
        el, _ = _solar_position(lat, lng, mid)

        if after_noon:
            # 下午太阳高度递减，el > target 说明还没到（太阳还太高），往后搜
            if el > target_elev:
                lo = mid
            else:
                hi = mid
        else:
            # 上午太阳高度递增，el < target 说明还没到（太阳还太低），往后搜
            if el < target_elev:
                lo = mid
            else:
                hi = mid

        best_jd = mid

    if best_jd is None:
        return ""

    # JD → 北京时间 (UTC+8)
    # 民约日 .0 = UT 子夜，直接取小数部分即为 UT 天数
    ut_hours = (best_jd - int(best_jd)) * 24  # 当天 UT 小时数
    cst_hours = (ut_hours + 8) % 24

    h = int(cst_hours)
    m = int((cst_hours - h) * 60 + 0.5)  # 四舍五入到分钟
    if m == 60:
        m = 0
        h = (h + 1) % 24
    return f"{h:02d}:{m:02d}"


def calc_golden_blue_hours(lat, lng, date_str):
    """
    精确计算黄金时段和蓝调时刻。

    定义（摄影师标准）：
      黄金时段：太阳高度角 +6° → -4°
      蓝调时刻：太阳高度角 -4° → -6°

    返回 {"golden_start": "HH:MM", "golden_end": "HH:MM",
           "blue_start": "HH:MM", "blue_end": "HH:MM"}
    """
    gold_start = _find_time_for_elevation(lat, lng, date_str, 6.0, after_noon=True)
    gold_end = _find_time_for_elevation(lat, lng, date_str, -4.0, after_noon=True)
    blue_start = gold_end  # 蓝调从黄金时段结束开始
    blue_end = _find_time_for_elevation(lat, lng, date_str, -6.0, after_noon=True)

    return {
        "golden_start": gold_start,
        "golden_end": gold_end,
        "blue_start": blue_start,
        "blue_end": blue_end,
    }


# ── AOD 气溶胶光学厚度（Open-Meteo Air Quality API） ──

def fetch_aod(lat, lng):
    """
    从 Open-Meteo Air Quality API 获取 AOD 550nm 数据。
    免费，无需 API key。数据来源：CAMS 全球大气成分预报。
    返回 dict 或 None（失败时）
    """
    params = urllib.parse.urlencode({
        "latitude": lat, "longitude": lng,
        "hourly": "aerosol_optical_depth",
        "timezone": "Asia/Shanghai",
        "forecast_days": 1,
    })
    url = f"https://air-quality-api.open-meteo.com/v1/air-quality?{params}"
    try:
        data = _http_get(url)
        if "_error" in data:
            return None
        return data
    except Exception:
        return None


def extract_aod_at_sunset(aod_data, sunset_hour=18):
    """从 AOD 数据中提取日落时段的均值"""
    hourly = aod_data.get("hourly", {}) if aod_data else {}
    times = hourly.get("time", [])
    aod_vals = hourly.get("aerosol_optical_depth", [])

    if not times or not aod_vals:
        return None

    # 找日落前后 3h 窗口的 AOD 均值
    today = datetime.now(TZ_CST).strftime("%Y-%m-%d")
    target_prefix = today + "T"
    window = []
    for t, v in zip(times, aod_vals):
        if t.startswith(target_prefix) and v is not None:
            h = int(t.split("T")[1].split(":")[0])
            if sunset_hour - 1 <= h <= sunset_hour + 2:
                window.append(v)

    if not window:
        return None
    return sum(window) / len(window)


# ── 引擎 2：Open-Meteo 免费 API ───────────────────────────

def fetch_openmeteo(lat, lng):
    """从 Open-Meteo 获取天气数据（免费，无 key）"""
    # 同时获取 daily（日落时间+降水）+ hourly（分层云量+湿度）
    params = urllib.parse.urlencode({
        "latitude": lat, "longitude": lng,
        "daily": "sunrise,sunset,precipitation_probability_mean",
        "hourly": "cloud_cover_low,cloud_cover_mid,cloud_cover_high,"
                  "cloud_cover,visibility,"
                  "relative_humidity_2m,precipitation_probability",
        "timezone": "Asia/Shanghai",
        "forecast_days": 3,
    })
    url = f"https://api.open-meteo.com/v1/forecast?{params}"
    return _http_get(url)


def compute_sunset_quality(meteo_data, day_index=0, event_type="sunset", aod_value=None):
    """
    🔬 研究驱动型晚霞评分引擎 v2.1

    基于以下研究结论：
    1. AOD(气溶胶光学厚度) = 最重要的单一特征（Henriksson 2019, Chen 2022）
       → v2.1: 接入 CAMS 真实 AOD 550nm 数据（Open-Meteo Air Quality API）
       → 无AOD时降级为能见度代理
    2. 高云（卷云/卷积云 5-13km）是晚霞最佳散射介质，非低云
    3. 总云量 15-70% 为最优区间，非30-60%
    4. 云型配置 > 单层云量，多层云=纹理加分
    5. 湿度40-60%为最佳，>80%产生雾霾使颜色发灰
    """
    daily = meteo_data.get("daily", {})
    hourly = meteo_data.get("hourly", {})
    if not daily or not hourly:
        return None

    time_key = "sunset" if event_type == "sunset" else "sunrise"
    event_time_str = daily.get(time_key, [""] * 3)[day_index] or ""
    date_str = daily.get("time", [""] * 3)[day_index] or ""

    event_hour = 18
    if event_time_str and "T" in event_time_str:
        try:
            event_hour = int(event_time_str.split("T")[1].split(":")[0])
        except (ValueError, IndexError):
            pass

    rain_prob = daily.get("precipitation_probability_mean", [0] * 3)[day_index] or 0
    hourly_times = hourly.get("time", [])
    idx = _hour_index_for_date(hourly_times, date_str, event_hour)

    if idx is None:
        return {"error": f"找不到 {date_str} 日落时段的小时数据"}

    # 取日落前后 3h 窗口
    start_idx = max(0, idx - 1)
    end_idx = min(len(hourly_times), idx + 3)

    def safe_avg(key):
        vals = [v for v in hourly.get(key, [])[start_idx:end_idx] if v is not None]
        return sum(vals) / len(vals) if vals else None

    low_cloud = safe_avg("cloud_cover_low")
    mid_cloud = safe_avg("cloud_cover_mid")
    high_cloud = safe_avg("cloud_cover_high")
    total_cloud = safe_avg("cloud_cover")
    humidity = safe_avg("relative_humidity_2m")
    visibility = safe_avg("visibility")
    vis_km = visibility / 1000 if visibility else None

    # ════════════════════════════════════════════
    # ⭐ v2.0 多因子评分模型
    # ════════════════════════════════════════════

    score = 0.0
    factors = {}  # 各因子明细，用于调试

    def _v(val, fallback=0):
        """安全取值：0 是有效值，None 才是缺失"""
        return val if val is not None else fallback

    # ── 1. 云型配置评分 (权重 ~35%) ──
    # 决定性因素：高云 > 低云，多层 > 单层
    high_is_dominant = _v(high_cloud) >= 30 and _v(low_cloud) < 40
    low_is_dominant = _v(low_cloud) >= 20 and _v(low_cloud) <= 55 and _v(high_cloud) < 40
    multi_layer = _v(high_cloud) > 15 and _v(low_cloud) > 10
    overcast = _v(total_cloud) > 80
    clear_sky = _v(total_cloud) < 10

    if high_is_dominant and not overcast:
        # 🔥 高云晚霞——最佳！卷云/卷积云散射红光最强
        score += 0.40
        factors["cloud_type"] = "high_cloud_dominant"
        factors["cloud_type_score"] = 0.40
    elif low_is_dominant and not overcast:
        # 低云晚霞——也不错，但需要30-55%
        score += 0.28
        factors["cloud_type"] = "low_cloud_dominant"
        factors["cloud_type_score"] = 0.28
    elif _v(total_cloud) >= 10 and _v(total_cloud) <= 75:
        # 混合云——适中
        score += 0.22
        factors["cloud_type"] = "mixed"
        factors["cloud_type_score"] = 0.22
    elif clear_sky:
        # 晴空——无云散射，色彩平淡
        score += 0.05
        factors["cloud_type"] = "clear"
        factors["cloud_type_score"] = 0.05
    elif overcast:
        # 阴天——光线被完全遮挡
        score -= 0.10
        factors["cloud_type"] = "overcast"
        factors["cloud_type_score"] = -0.10

    # 多层云加分（纹理丰富更出片）
    if multi_layer:
        score += 0.08
        factors["multi_layer_bonus"] = 0.08
    elif factors.get("cloud_type") == "high_cloud_dominant" and _v(high_cloud) > 40:
        # 高云单层也有丰富纹理（卷云/卷积云本身纹理漂亮）
        score += 0.04
        factors["high_cloud_texture"] = 0.04

    # ── 2. AOD/能见度评分 (权重 ~25%) ──
    # v2.1: 优先使用真实 AOD 550nm 数据（Henriksson 2019 #1 特征）
    # 中等 AOD (0.15-0.45) 最佳——气溶胶散射红光增强晚霞
    # AOD 太低 → 色彩平淡；AOD 太高 → 雾霾遮挡
    if aod_value is not None:
        factors["aod_source"] = "CAMS 550nm"
        factors["aod_value"] = round(aod_value, 3)
        if 0.15 <= aod_value <= 0.45:
            score += 0.18
            factors["aod_score"] = 0.18
            factors["aod_note"] = "optimal"
        elif 0.45 < aod_value <= 0.70:
            score += 0.10
            factors["aod_score"] = 0.10
            factors["aod_note"] = "good"
        elif 0.70 < aod_value <= 0.90:
            score += 0.03
            factors["aod_score"] = 0.03
            factors["aod_note"] = "hazy"
        elif aod_value > 0.90:
            score -= 0.10
            factors["aod_score"] = -0.10
            factors["aod_note"] = "too_hazy"
        else:  # < 0.15, very clean air
            score += 0.05
            factors["aod_score"] = 0.05
            factors["aod_note"] = "too_clean"
    else:
        # 降级：能见度代理
        if vis_km is None:
            factors["aod_score"] = 0
        elif vis_km >= 20:
            score += 0.18
            factors["aod_score"] = 0.18
            factors["aod_note"] = "vis_excellent"
        elif vis_km >= 12:
            score += 0.12
            factors["aod_score"] = 0.12
            factors["aod_note"] = "vis_good"
        elif vis_km >= 6:
            score += 0.05
            factors["aod_score"] = 0.05
            factors["aod_note"] = "vis_moderate"
        else:
            score -= 0.08
            factors["aod_score"] = -0.08
            factors["aod_note"] = "vis_poor"

    # ── 3. 湿度评分 (权重 ~15%) ──
    # 40-60% 最佳；>80% 雾蒙蒙；<30% 太干
    if humidity is None:
        factors["humidity_score"] = 0
    elif 40 <= humidity <= 60:
        score += 0.15
        factors["humidity_score"] = 0.15
        factors["humidity_note"] = "optimal"
    elif 30 <= humidity < 40 or 60 < humidity <= 75:
        score += 0.08
        factors["humidity_score"] = 0.08
        factors["humidity_note"] = "good"
    elif humidity > 85:
        score -= 0.10
        factors["humidity_score"] = -0.10
        factors["humidity_note"] = "too_wet"
    else:  # 75-85
        score += 0.04
        factors["humidity_score"] = 0.04
        factors["humidity_note"] = "ok"

    # ── 4. 降水惩罚 (权重 ~10%) ──
    if rain_prob > 50:
        score -= 0.15
        factors["rain_penalty"] = -0.15
    elif rain_prob > 25:
        score -= 0.08
        factors["rain_penalty"] = -0.08
    else:
        factors["rain_penalty"] = 0

    # ── 5. 总云量修正 (权重 ~15%) ──
    # 超过总云量最优区间后的额外扣分
    tc = _v(total_cloud, 50)
    if tc > 75:
        penalty = -0.08 * ((tc - 75) / 25)  # 75%→-0, 100%→-0.08
        score += penalty
        factors["total_cloud_penalty"] = round(penalty, 3)
    elif tc < 10:
        factors["total_cloud_penalty"] = 0
    else:
        # 15-60% 最优区间
        if 15 <= tc <= 60:
            score += 0.05
            factors["total_cloud_bonus"] = 0.05
        factors["total_cloud_penalty"] = 0

    # ── Clamp ──
    score = max(0.0, min(1.0, round(score, 2)))

    # ── 置信度评估 ──
    # 数据越完整 = 置信度越高
    data_points = sum(1 for v in [low_cloud, mid_cloud, high_cloud, total_cloud, humidity, visibility] if v is not None)
    confidence = min(1.0, data_points / 6 + 0.1)

    sunset_time = daily.get("sunset", [""] * 3)[day_index] or ""
    sunrise_time = daily.get("sunrise", [""] * 3)[day_index] or ""

    return {
        "quality": score,
        "source": "open-meteo",
        "confidence": round(confidence, 2),
        "cloud_cover_low": round(low_cloud, 0) if low_cloud else None,
        "cloud_cover_mid": round(mid_cloud, 0) if mid_cloud else None,
        "cloud_cover_high": round(high_cloud, 0) if high_cloud else None,
        "total_cloud_cover": round(total_cloud, 0) if total_cloud else None,
        "visibility_km": round(vis_km, 1) if vis_km else None,
        "rain_probability": round(rain_prob, 0),
        "humidity": round(humidity, 0) if humidity else None,
        "sunset_time": sunset_time,
        "sunrise_time": sunrise_time,
        "factors": factors,
    }


# ── 位置解析 ────────────────────────────────────────────────

def resolve_location(location=None, lat=None, lng=None):
    """级联位置解析（单地点）：手动坐标 > 城市名 > 环境变量 > 默认杭州"""
    if lat is not None and lng is not None:
        return float(lat), float(lng), f"({lat},{lng})"

    if location:
        loc = LOCATION_MAP.get(location)
        if loc:
            return loc[0], loc[1], location

    env_loc = os.environ.get("SUNSET_LOCATION", "")
    if env_loc:
        loc = LOCATION_MAP.get(env_loc)
        if loc:
            return loc[0], loc[1], env_loc

    return 30.2741, 120.1551, "杭州"


def parse_locations(location=None, lat=None, lng=None):
    """
    解析多地点。支持逗号分隔。
    返回 [(lat, lng, name), ...] 列表。

    示例：
        --location 杭州,北京,上海
        --lat 30.27,39.90 --lng 120.15,116.40
        --location 杭州 --lat 34.80 --lng 114.31   (混合)
    """
    locs = []

    # ── 城市名（逗号分隔） ──
    if location:
        for name in location.split(","):
            name = name.strip()
            if name:
                loc = LOCATION_MAP.get(name)
                if loc:
                    locs.append((loc[0], loc[1], name))
                else:
                    print(f"⚠️ 未知城市: {name}，已跳过")

    # ── 经纬度（逗号分隔） ──
    if lat is not None and lng is not None:
        lats = [x.strip() for x in str(lat).split(",")]
        lngs = [x.strip() for x in str(lng).split(",")]
        if len(lats) != len(lngs):
            print(f"⚠️ lat 和 lng 数量不匹配，经纬度部分已跳过")
        else:
            for i, (la, lo) in enumerate(zip(lats, lngs)):
                try:
                    locs.append((float(la), float(lo), f"({la},{lo})"))
                except ValueError:
                    print(f"⚠️ 经纬度格式错误: {la},{lo}")

    # ── 兜底：啥也没配就用环境变量/默认杭州 ──
    if not locs:
        env_loc = os.environ.get("SUNSET_LOCATION", "")
        if env_loc:
            loc = LOCATION_MAP.get(env_loc)
            if loc:
                locs.append((loc[0], loc[1], env_loc))
        if not locs:
            locs.append((30.2741, 120.1551, "杭州"))

    return locs


# ── 格式化输出 ──────────────────────────────────────────────

def quality_emoji(score):
    if score >= 0.75: return "🔥"
    if score >= 0.50: return "🌤️"
    if score >= 0.25: return "🌥️"
    return "☁️"


def quality_label(score):
    if score >= 0.75: return "绝佳！火烧云级别 🔥"
    if score >= 0.65: return "不错，值得出工 🌤️"
    if score >= 0.50: return "还行，可拍可不拍 🌤️"
    if score >= 0.35: return "一般，大概率不出彩 🌥️"
    if score >= 0.20: return "偏弱，别抱期望 ☁️"
    return "很差，改天吧 ☁️"


def _utc_to_cst(utc_iso):
    """将 UTC ISO 时间转为 CST HH:MM，带容错"""
    if not utc_iso or "T" not in utc_iso:
        return utc_iso
    try:
        raw = utc_iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        return dt.astimezone(TZ_CST).strftime("%H:%M")
    except Exception:
        return utc_iso[:16]


def _sunset_cst(sunset_iso):
    """从 ISO 时间提取 HH:MM"""
    if not sunset_iso or "T" not in sunset_iso:
        return ""
    return sunset_iso.split("T")[1][:5]


def _est_golden_hour(sunset_time):
    """估计黄金时段（日落前后各 30min）"""
    if not sunset_time:
        return "", ""
    try:
        parts = sunset_time.split(":")
        total_min = int(parts[0]) * 60 + int(parts[1])
        start = f"{((total_min - 30)//60):02d}:{((total_min - 30)%60):02d}"
        end = sunset_time[:5]
        bh_start = f"{((total_min + 10)//60):02d}:{((total_min + 10)%60):02d}"
        bh_end = f"{((total_min + 25)//60):02d}:{((total_min + 25)%60):02d}"
        return f"{start} — {end}", f"{bh_start} — {bh_end}"
    except (ValueError, IndexError):
        return "", ""


def format_discord_message(result, location_name, date_str, event_type, gh=None):
    """生成 Discord 格式消息"""
    quality = result.get("quality", 0)
    emoji = quality_emoji(quality)
    label = quality_label(quality)
    source = result.get("source", "open-meteo")

    event_label = "晚霞" if event_type == "sunset" else "朝霞"
    icon = "🌅" if event_type == "sunset" else "🌄"

    lines = [
        f"{icon} **{location_name} {event_label}预报** · {date_str}",
        "━" * 30,
        f"{emoji} **评分：{quality:.0%}** — {label}",
    ]

    # 来源
    src_tag = "Sunsethue" if source == "sunsethue" else "Open-Meteo+云量算法"
    lines.append(f"📡 来源：{src_tag}")

    # Sunsethue 独有字段
    if source == "sunsethue":
        cloud = result.get("cloud_cover")
        if cloud is not None:
            lines.append(f"☁️ 云量：{cloud*100:.0f}%")
        if "direction" in result:
            lines.append(f"🧭 日落方向：{result['direction']:.0f}°")

        magics = result.get("magics", {})
        if magics.get("golden_hour"):
            gh = magics["golden_hour"]
            gh_start = _utc_to_cst(gh[0])
            gh_end = _utc_to_cst(gh[1])
            lines.append(f"⏰ 黄金时段：{gh_start} — {gh_end}")
        if magics.get("blue_hour"):
            bh = magics["blue_hour"]
            bh_start = _utc_to_cst(bh[0])
            bh_end = _utc_to_cst(bh[1])
            lines.append(f"⏰ 蓝色时刻：{bh_start} — {bh_end}")
        sunset_t = result.get("sunset_time_local", "")
        if sunset_t:
            lines.insert(3, f"🌇 日落：{sunset_t}")
    else:
        # Open-Meteo 数据
        sunset_t = _sunset_cst(result.get("sunset_time", ""))
        if sunset_t and event_type == "sunset":
            lines.append(f"🌇 日落：{sunset_t}")

        low_c = result.get("cloud_cover_low")
        mid_c = result.get("cloud_cover_mid")
        high_c = result.get("cloud_cover_high")
        total_c = result.get("total_cloud_cover")
        vis = result.get("visibility_km")
        conf = result.get("confidence")

        # 云型描述
        factors = result.get("factors", {})
        cloud_type = factors.get("cloud_type", "")
        if cloud_type == "high_cloud_dominant":
            lines.append("☁️ 云型：高云主导（卷云/卷积云，散射效果最佳 🔥）")
        elif cloud_type == "low_cloud_dominant":
            lines.append("☁️ 云型：低云主导（层积云，效果良好）")
        elif cloud_type == "mixed":
            lines.append("☁️ 云型：混合云层")
        elif cloud_type == "clear":
            lines.append("☁️ 云型：晴空（无云散射，色彩平淡）")
        elif cloud_type == "overcast":
            lines.append("☁️ 云型：阴天（光线被遮挡）")

        # 云量详情
        parts = []
        if low_c is not None: parts.append(f"低{low_c:.0f}%")
        if mid_c is not None: parts.append(f"中{mid_c:.0f}%")
        if high_c is not None: parts.append(f"高{high_c:.0f}%")
        if total_c is not None: parts.append(f"总{total_c:.0f}%")
        if parts:
            lines.append(f"☁️ 云量：{' · '.join(parts)}")

        # AOD / 能见度
        aod_val = result.get("aod_value") or factors.get("aod_value")
        if aod_val is not None:
            aod_note = factors.get("aod_note", "")
            if aod_note == "optimal":
                aod_icon = "🔬 AOD"
            elif aod_note in ("good", "good_enough"):
                aod_icon = "🔬 AOD"
            else:
                aod_icon = "🔬 AOD"
            lines.append(f"{aod_icon}：{aod_val:.3f} (550nm) {'✅ 最佳散射' if aod_note=='optimal' else '👍 良好' if aod_note=='good' else '⚠️ 偏雾' if aod_note=='hazy' else '❌ 严重雾霾' if aod_note=='too_hazy' else '🌫️ 过于清洁'}")
        elif vis is not None:
            lines.append(f"👁️ 能见度：{vis:.1f}km{' ✅通透' if vis >= 20 else ' ✅良好' if vis >= 12 else ' ⚠️一般' if vis >= 6 else ' ❌雾霾'}")

        # 湿度
        hu = result.get("humidity")
        if hu is not None:
            note = factors.get("humidity_note", "")
            icon = "💧" if note == "optimal" else "💧"
            lines.append(f"{icon} 湿度：{hu:.0f}%{' ✅最佳' if note=='optimal' else ' 👍良好' if note=='good' else ' ⚠️偏湿' if note=='too_wet' else ''}")

        # 降水
        rp = result.get("rain_probability", 0)
        if rp > 0:
            lines.append(f"🌧️ 降水概率：{rp:.0f}%")

        # 置信度
        if conf is not None:
            conf_stars = "🟢高" if conf >= 0.85 else "🟡中" if conf >= 0.65 else "🔴低"
            lines.append(f"🎯 置信度：{conf:.0%}（{conf_stars}）")

        # 精确黄金/蓝调时刻
        if gh and gh.get("golden_start"):
            lines.append(f"⏰ 黄金时段：{gh['golden_start']} — {gh['golden_end']} ✨精确")
            if gh.get("blue_end"):
                lines.append(f"⏰ 蓝调时刻：{gh['blue_start']} — {gh['blue_end']} ✨精确")
        else:
            gold, blue = _est_golden_hour(sunset_t or "18:40")
            if gold:
                lines.append(f"⏰ 黄金时段（估）：{gold}")
            if blue:
                lines.append(f"⏰ 蓝色时刻（估）：{blue}")

    # 拍摄建议
    lines.append("")
    lines.append("📸 **拍摄建议：**")
    if quality >= 0.75:
        lines.append("• 🔥 今晚必出！提前 1h 到场地踩光")
        lines.append("• 穿暖色系（橙/红/黄）更融晚霞")
        lines.append("• 带反光板/补光灯补面光")
        lines.append("• 三脚架必备（蓝调时刻光线暗）")
    elif quality >= 0.50:
        lines.append("• 值得出工，提前 30min 到")
        lines.append("• 日落方向找开阔地")
        lines.append("• 建议带反光板")
    else:
        lines.append("• 建议改天，或拍室内/夜景")
        lines.append("• 如果去了，调色上多拉饱和度+暖色调")

    # 杭州推荐点位
    if location_name == "杭州" and quality >= 0.25:
        lines.append("")
        lines.append("📍 **推荐机位（杭州）：**")
        limit = 3 if quality < 0.50 else 5
        for spot, desc in HANGZHOU_SPOTS[:limit]:
            lines.append(f"• {spot} — {desc}")

    lines.append("")
    lines.append(f"🕐 预报更新：{datetime.now(TZ_CST).strftime('%H:%M')}")

    return "\n".join(lines)


# ── 飞书推送 ────────────────────────────────────────────────

def send_feishu(webhook_url, result, location_name, date_str, event_type):
    """将晚霞预报推送到飞书群（自定义机器人 Webhook）—— 丰富版卡片"""
    quality = result["quality"]
    emoji = quality_emoji(quality)
    label = quality_label(quality)

    event_label = "晚霞" if event_type == "sunset" else "朝霞"
    icon = "🌅" if event_type == "sunset" else "🌄"

    # 卡片颜色
    if quality >= 0.75:
        color = "red"
    elif quality >= 0.60:
        color = "orange"
    elif quality >= 0.50:
        color = "yellow"
    elif quality >= 0.35:
        color = "wathet"
    else:
        color = "grey"

    sunset_t = _sunset_cst(result.get("sunset_time", "")) if event_type == "sunset" else ""

    factors = result.get("factors", {})
    cloud_type = factors.get("cloud_type", "")
    cloud_desc = {
        "high_cloud_dominant": "高云主导 🔥 散射最佳",
        "low_cloud_dominant": "低云主导",
        "mixed": "混合云层",
        "clear": "晴空 · 无云散射",
        "overcast": "阴天 · 光线遮挡 ⚠️",
    }.get(cloud_type, "—")

    low_c = result.get("cloud_cover_low")
    mid_c = result.get("cloud_cover_mid")
    high_c = result.get("cloud_cover_high")
    total_c = result.get("total_cloud_cover")

    cloud_parts = []
    if high_c is not None: cloud_parts.append(f"高{high_c:.0f}%")
    if mid_c is not None: cloud_parts.append(f"中{mid_c:.0f}%")
    if low_c is not None: cloud_parts.append(f"低{low_c:.0f}%")
    if total_c is not None: cloud_parts.append(f"总{total_c:.0f}%")
    cloud_detail = " · ".join(cloud_parts) if cloud_parts else "—"

    vis = result.get("visibility_km")
    if vis is not None:
        if vis >= 20:
            vis_str = f"{vis:.1f}km ✅ 通透"
        elif vis >= 12:
            vis_str = f"{vis:.1f}km ✅ 良好"
        elif vis >= 6:
            vis_str = f"{vis:.1f}km ⚠️ 一般"
        else:
            vis_str = f"{vis:.1f}km ❌ 雾霾"
    else:
        vis_str = "—"

    hu = result.get("humidity")
    if hu is not None:
        if 40 <= hu <= 60:
            hu_str = f"{hu:.0f}% ✅ 最佳"
        elif 30 <= hu < 40 or 60 < hu <= 75:
            hu_str = f"{hu:.0f}% 👍 良好"
        elif hu > 85:
            hu_str = f"{hu:.0f}% ⚠️ 偏湿"
        else:
            hu_str = f"{hu:.0f}%"
    else:
        hu_str = "—"

    rp = result.get("rain_probability", 0)

    conf = result.get("confidence")
    if conf is not None:
        if conf >= 0.85:
            conf_str = f"{conf:.0%} 🟢 高"
        elif conf >= 0.65:
            conf_str = f"{conf:.0%} 🟡 中"
        else:
            conf_str = f"{conf:.0%} 🔴 低"
    else:
        conf_str = "—"

    # v2.1: 优先使用精确天文计算
    gh = result.get("golden_hour", {}) or {}
    if gh.get("golden_start"):
        gold_str = f"{gh['golden_start']} — {gh['golden_end']}"
        blue_str = f"{gh.get('blue_start', '—')} — {gh.get('blue_end', '—')}"
    else:
        gold, blue = _est_golden_hour(sunset_t or "18:40")
        gold_str = (gold or "—") + " (估)"
        blue_str = (blue or "—") + " (估)"

    # 拍摄建议
    if quality >= 0.75:
        tips = "🔥 今晚必出！提前 1h 到场踩光\n• 三脚架必备 · 带反光板补面光\n• 穿暖色系（橙/红/黄）更融晚霞"
    elif quality >= 0.50:
        tips = "👍 值得出工\n• 提前 30min 到场 · 找开阔方向\n• 建议带反光板"
    elif quality >= 0.35:
        tips = "🤔 可拍可不拍\n• 如果出工，后期多拉饱和度+暖色调"
    else:
        tips = "🏠 建议改天\n• 拍室内、夜景或改期吧"

    # 杭州机位
    spots_text = ""
    if location_name == "杭州" and quality >= 0.25:
        limit = 3 if quality < 0.60 else 5
        spots_list = [f"• {s} — {d}" for s, d in HANGZHOU_SPOTS[:limit]]
        spots_text = "\n".join(spots_list)

    # ── 组装丰富版飞书卡片 ──
    # 用 lark_md 表格 + 分区，信息密度高且可读
    # AOD / 能见度行
    aod_val = result.get("aod_value") or factors.get("aod_value")
    if aod_val is not None:
        aod_note = factors.get("aod_note", "")
        aod_label = {"optimal": "✅ 最佳散射", "good": "👍 良好",
                      "hazy": "⚠️ 偏雾", "too_hazy": "❌ 严重雾霾",
                      "too_clean": "🌫️ 过于清洁"}.get(aod_note, "")
        aero_line = f"🔬 **AOD (550nm)**：{aod_val:.3f} {aod_label}\n"
    else:
        aero_line = f"👁️ **能见度**：{vis_str}\n"

    markdown_body = (
        f"**{emoji} 评分：{quality:.0%} — {label}**\n\n"
        f"🌇 **日落**：{sunset_t or '—'}\n"
        f"☁️ **云型**：{cloud_desc}\n"
        f"☁️ **云量**：{cloud_detail}\n"
        f"{aero_line}"
        f"💧 **湿度**：{hu_str}\n"
    )
    if rp > 0:
        markdown_body += f"🌧️ **降水概率**：{rp:.0f}%\n"
    markdown_body += (
        f"🎯 **置信度**：{conf_str}\n"
        f"⏰ **黄金时段**：{gold_str}\n"
        f"⏰ **蓝调时刻**：{blue_str}\n"
    )

    card_elements = [
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": markdown_body.strip()},
        },
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"📸 **拍摄建议**\n{tips}"},
        },
    ]

    if spots_text:
        card_elements.append({"tag": "hr"})
        card_elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"📍 **推荐机位（{location_name}）**\n{spots_text}"},
        })

    card_elements.append({"tag": "hr"})
    card_elements.append({
        "tag": "note",
        "elements": [{
            "tag": "plain_text",
            "content": f"🕐 预报更新：{datetime.now(TZ_CST).strftime('%H:%M')} CST · 数据：Open-Meteo · {result.get('source', 'open-meteo')}",
        }],
    })

    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"{icon} {location_name} {event_label}预报 · {date_str}",
                },
                "template": color,
            },
            "elements": card_elements,
        },
    }

    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp_body = json.loads(resp.read().decode())
            return resp_body
    except Exception as e:
        return {"_error": str(e)}


# ── 测试诊断 ────────────────────────────────────────────────

def run_test(location, lat, lng, webhook_url):
    """运行诊断测试，验证所有组件是否正常。"""
    results = {"pass": 0, "fail": 0, "checks": []}

    def check(name, ok, detail=""):
        mark = "✅" if ok else "❌"
        line = f"  {mark} {name}{' — ' + detail if detail else ''}"
        results["checks"].append(line)
        print(line)
        if ok:
            results["pass"] += 1
        else:
            results["fail"] += 1

    print("═" * 55)
    print("🔧 晚霞预报 v2.0 — 诊断测试")
    print("═" * 55)
    print()

    # ── 1. 系统环境 ──
    print("📋 系统环境")
    check("Python 版本", sys.version_info >= (3, 7),
          f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    check("操作系统", True, sys.platform)
    check("编码", sys.stdout.encoding == "utf-8" or True, sys.stdout.encoding or "unknown")
    print()

    # ── 2. 网络连接 ──
    print("🌐 网络连接")
    try:
        req = urllib.request.Request(
            "https://api.open-meteo.com/v1/forecast?latitude=30.27&longitude=120.15&daily=sunset&timezone=Asia/Shanghai&forecast_days=1",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            has_daily = "daily" in data
            sunset = data.get("daily", {}).get("sunset", ["?"])[0]
        check("Open-Meteo API", has_daily, f"日落时间: {sunset}")
    except Exception as e:
        check("Open-Meteo API", False, str(e))
    print()

    # ── 3. 位置解析 ──
    print("📍 位置解析")
    locs = parse_locations(location=location, lat=lat, lng=lng)
    check("位置解析", len(locs) > 0, f"共 {len(locs)} 个地点")
    for lat_val, lng_val, name in locs:
        print(f"     📍 {name} ({lat_val}, {lng_val})")
    print()

    # ── 4. 预测引擎 ──
    print("🧠 预测引擎")
    for lat_val, lng_val, name in locs:
        result = run_prediction(lat=lat_val, lng=lng_val, label=name)
        if "error" in result:
            check(f"预报 [{name}]", False, result["error"])
        else:
            q = result["quality"]
            s = result["short_summary"]
            check(f"预报 [{name}]", True, f"评分 {q:.0%}")
            print(f"     {s}")

            # 详细数据
            r = result
            print(f"     日落: {_sunset_cst(r.get('sunset_time','')) or '—'}")
            def v(key, fmt='{}'):
                """安全取值，None 显示为 -"""
                val = r.get(key)
                return fmt.format(val) if val is not None else '-'
            print(f"     云: 高{v('cloud_cover_high', '{:.0f}')}% · 中{v('cloud_cover_mid', '{:.0f}')}% · 低{v('cloud_cover_low', '{:.0f}')}% · 总{v('total_cloud_cover', '{:.0f}')}%")
            aod = r.get('aod_value')
            vis = r.get('visibility_km')
            if aod is not None:
                print(f"     🔬 AOD: {aod:.3f} (550nm)  湿度: {v('humidity', '{:.0f}')}%  降水: {r.get('rain_probability',0):.0f}%")
            else:
                print(f"     能见度: {f'{vis:.1f}km' if vis else '—'}  湿度: {v('humidity', '{:.0f}')}%  降水: {r.get('rain_probability',0):.0f}%")
            gh = r.get('golden_hour', {}) or {}
            if gh.get('golden_start'):
                print(f"     ⏰ 黄金: {gh['golden_start']}—{gh['golden_end']}  蓝调: {gh.get('blue_start','?')}—{gh.get('blue_end','?')} ✨精确")
            else:
                gold, blue = _est_golden_hour(_sunset_cst(r.get('sunset_time','')) or '18:40')
                print(f"     ⏰ 黄金(估): {gold or '—'}  蓝调(估): {blue or '—'}")
    print()

    # ── 5. 飞书 Webhook ──
    print("📤 飞书推送")
    if webhook_url:
        print(f"  Webhook: {webhook_url[:50]}...")
        for lat_val, lng_val, name in locs:
            result = run_prediction(lat=lat_val, lng=lng_val, label=name)
            if "error" in result:
                check(f"飞书推送 [{name}]", False, result["error"])
            else:
                fs_resp = send_feishu(webhook_url, result, name,
                                      result["date"], result["event_type"])
                if "_error" in fs_resp:
                    check(f"飞书推送 [{name}]", False,
                          f"发送失败: {fs_resp['_error']}")
                else:
                    code = fs_resp.get("code", -1)
                    msg = fs_resp.get("msg", "")
                    check(f"飞书推送 [{name}]", code == 0,
                          f"code={code} {msg}")
    else:
        print("  ⚠️ 未配置 Webhook，跳过推送测试")
        print("  设置方法：--feishu <URL> 或 FEISHU_WEBHOOK_URL 环境变量")
    print()

    # ── 6. 总结 ──
    print("═" * 55)
    total = results["pass"] + results["fail"]
    print(f"📊 测试结果: {results['pass']}/{total} 通过"
          + (f", {results['fail']} 失败" if results['fail'] else " ✅ 全部通过"))
    print("═" * 55)

    return results["fail"] == 0


# ── 守护模式 ────────────────────────────────────────────────

def serve_daemon(location, lat, lng, webhook_url, push_time_str="16:00"):
    """
    守护模式：后台常驻，每天定点推送（支持多地点）。

    参数：
        location: 城市名，逗号分隔（如 "杭州,北京,上海"）
        lat, lng: 经纬度，逗号分隔（如 "30.27,39.90"）
        webhook_url: 飞书 Webhook URL（可选）
        push_time_str: 推送时间，格式 "HH:MM"（默认 16:00）
    """
    try:
        push_hour, push_minute = map(int, push_time_str.split(":"))
    except ValueError:
        print(f"❌ 时间格式错误: {push_time_str}，应为 HH:MM")
        sys.exit(1)

    # ── 解析所有地点 ──
    locations = parse_locations(location=location, lat=lat, lng=lng)
    loc_names = [name for _, _, name in locations]

    print("═" * 55)
    print("🌅  晚霞预报服务 v2.0")
    print("═" * 55)
    print(f"   地点数量： {len(locations)}")
    for _, _, name in locations:
        print(f"     📍 {name}")
    print(f"   推送时间： 每天 {push_hour:02d}:{push_minute:02d} CST")
    print(f"   飞书推送： {'✅ 已配置' if webhook_url else '⚠️ 未配置，仅本地输出'}")
    print("═" * 55)
    print()

    # ── 对单个地点执行预测+推送 ──
    def push_one(lat_val, lng_val, name, label=""):
        prefix = f"[{name}]" if label else ""
        result = run_prediction(lat=lat_val, lng=lng_val, label=name)
        if "error" in result:
            print(f"  {prefix} ❌ {result['error']}")
            return
        print(f"  {prefix} {result['short_summary']}")
        if webhook_url:
            fs_resp = send_feishu(webhook_url, result, name,
                                  result["date"], result["event_type"])
            if "_error" in fs_resp:
                print(f"  {prefix} ⚠️ 飞书失败: {fs_resp['_error']}")
            else:
                print(f"  {prefix} ✅ 已推送")
        return result

    # ── 立即跑一次（启动时） ──
    print("🚀 启动时预报:\n")
    for lat_val, lng_val, name in locations:
        push_one(lat_val, lng_val, name)
        time.sleep(1)  # 避免请求过快
    print()

    # ── 循环等待 ──
    while True:
        now = datetime.now(TZ_CST)
        next_push = now.replace(hour=push_hour, minute=push_minute,
                                second=0, microsecond=0)
        if now >= next_push:
            next_push += timedelta(days=1)

        wait_sec = (next_push - now).total_seconds()
        wait_hours = wait_sec / 3600
        print(f"⏳ 下次推送：{next_push.strftime('%Y-%m-%d %H:%M')} CST "
              f"({wait_hours:.1f}h 后，共 {len(locations)} 个地点)")
        print("   (按 Ctrl+C 停止服务)\n")

        try:
            time.sleep(wait_sec)
        except KeyboardInterrupt:
            print("\n👋 晚霞预报服务已停止")
            break

        # ── 到点推送所有地点 ──
        print("═" * 55)
        print(f"📤 {datetime.now(TZ_CST).strftime('%Y-%m-%d %H:%M')} 定时推送:")
        print("═" * 55)
        for lat_val, lng_val, name in locations:
            push_one(lat_val, lng_val, name)
            time.sleep(1)
        print()


# ── 主流程 ──────────────────────────────────────────────────

def run_prediction(location=None, lat=None, lng=None,
                   date_str=None, event_type="sunset", label=None):
    """
    执行日落/日出质量预测。

    参数：
        location: 城市名
        lat, lng: 经纬度
        label: 显示名称（优先级最高，用于多地点时携带原始地名）
    返回 dict { quality, source, discord_message, short_summary, ... }
    """
    resolved_lat, resolved_lng, location_name = resolve_location(location, lat, lng)
    if label:
        location_name = label  # 外部传入的名称优先

    if not date_str:
        date_str = datetime.now(TZ_CST).strftime("%Y-%m-%d")

    today_str = datetime.now(TZ_CST).strftime("%Y-%m-%d")
    day_index = 0 if date_str == today_str else 1

    # ── 引擎 1：尝试 Sunsethue ──
    result = predict_sunsethue(resolved_lat, resolved_lng, date_str, event_type)
    if result and "error" not in result:
        data = result.get("data", {})
        raw = {
            "quality": data.get("quality", 0.5),
            "cloud_cover": data.get("cloud_cover"),
            "direction": data.get("direction"),
            "magics": data.get("magics", {}),
            "source": "sunsethue",
        }
        # 补全日落时间
        sunset_t_raw = data.get("time", "")
        if sunset_t_raw:
            try:
                dt = datetime.fromisoformat(sunset_t_raw.replace("Z", "+00:00"))
                raw["sunset_time_local"] = dt.astimezone(TZ_CST).strftime("%H:%M")
            except Exception:
                pass
    else:
        # ── 引擎 2：Open-Meteo 兜底 ──
        meteo = fetch_openmeteo(resolved_lat, resolved_lng)
        if "_error" in meteo:
            return {"error": f"Open-Meteo 请求失败: {meteo['_error']}", "source": "failed"}

        # v2.1: 获取 AOD 数据
        aod_val = None
        aod_data = fetch_aod(resolved_lat, resolved_lng)
        if aod_data:
            sunset_h = 18
            st = meteo.get("daily", {}).get("sunset", [""] * 3)[day_index] or ""
            if st and "T" in st:
                try:
                    sunset_h = int(st.split("T")[1].split(":")[0])
                except (ValueError, IndexError):
                    pass
            aod_val = extract_aod_at_sunset(aod_data, sunset_h)

        raw = compute_sunset_quality(meteo, day_index=day_index,
                                     event_type=event_type, aod_value=aod_val)
        if raw is None or "error" in (raw or {}):
            return {"error": f"云量解析失败: {raw}", "source": "failed"}

    # v2.1: 精确黄金/蓝调时刻
    gh = calc_golden_blue_hours(resolved_lat, resolved_lng, date_str)

    discord_msg = format_discord_message(raw, location_name, date_str, event_type, gh)
    short_summary = f"{quality_emoji(raw['quality'])} {location_name} {date_str}: {raw['quality']:.0%}"

    return {
        "quality": raw["quality"],
        "source": raw["source"],
        "location": location_name,
        "coordinates": (resolved_lat, resolved_lng),
        "date": date_str,
        "event_type": event_type,
        "short_summary": short_summary,
        "discord_message": discord_msg,
        # 原始数据透传
        "cloud_cover_low": raw.get("cloud_cover_low"),
        "cloud_cover_mid": raw.get("cloud_cover_mid"),
        "cloud_cover_high": raw.get("cloud_cover_high"),
        "total_cloud_cover": raw.get("total_cloud_cover"),
        "visibility_km": raw.get("visibility_km"),
        "humidity": raw.get("humidity"),
        "rain_probability": raw.get("rain_probability"),
        "sunset_time": raw.get("sunset_time"),
        "sunrise_time": raw.get("sunrise_time"),
        "confidence": raw.get("confidence"),
        "factors": raw.get("factors", {}),
        "golden_hour": gh,
        "aod_value": raw.get("aod_value") if "aod_value" in raw else raw.get("factors", {}).get("aod_value"),
    }


# ── CLI 入口 ────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="🌅 晚霞/朝霞预测")
    parser.add_argument("--location", type=str, default=None,
                        help="城市名，逗号分隔（如 '杭州,北京,上海'）")
    parser.add_argument("--lat", type=str, default=None,
                        help="纬度，逗号分隔（如 '30.27,39.90'）")
    parser.add_argument("--lng", type=str, default=None,
                        help="经度，逗号分隔（如 '120.15,116.40'）")
    parser.add_argument("--date", type=str, default=None)
    parser.add_argument("--type", dest="event_type", type=str,
                        default="sunset", choices=["sunset", "sunrise"])
    parser.add_argument("--discord", action="store_true")
    parser.add_argument("--feishu", type=str, default=None,
                        help="飞书 Webhook URL（也支持 FEISHU_WEBHOOK_URL 环境变量）")
    parser.add_argument("--serve", action="store_true",
                        help="守护模式：后台常驻，每天定时推送")
    parser.add_argument("--serve-time", type=str, default="16:00",
                        help="守护模式推送时间，格式 HH:MM（默认 16:00）")
    parser.add_argument("--test", action="store_true",
                        help="诊断测试：验证所有组件是否正常")
    parser.add_argument("--short", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    # ── 测试模式 ──
    if args.test:
        feishu_url = args.feishu or os.environ.get("FEISHU_WEBHOOK_URL", "")
        ok = run_test(
            location=args.location, lat=args.lat, lng=args.lng,
            webhook_url=feishu_url,
        )
        sys.exit(0 if ok else 1)

    # ── 守护模式 ──
    if args.serve:
        feishu_url = args.feishu or os.environ.get("FEISHU_WEBHOOK_URL", "")
        serve_daemon(
            location=args.location, lat=args.lat, lng=args.lng,
            webhook_url=feishu_url, push_time_str=args.serve_time,
        )
        return

    # ── 一键模式（支持多地点） ──
    locations = parse_locations(location=args.location, lat=args.lat, lng=args.lng)
    feishu_url = args.feishu or os.environ.get("FEISHU_WEBHOOK_URL", "")

    all_results = []
    for lat_val, lng_val, name in locations:
        result = run_prediction(
            lat=lat_val, lng=lng_val,
            date_str=args.date, event_type=args.event_type, label=name,
        )

        if "error" in result:
            print(f"❌ [{name}] {result['error']}")
            continue

        all_results.append(result)

        # ── 飞书推送 ──
        if feishu_url:
            fs_resp = send_feishu(feishu_url, result, name,
                                  result["date"], result["event_type"])
            if "_error" in fs_resp:
                print(f"⚠️ [{name}] 飞书推送失败: {fs_resp['_error']}")
            else:
                print(f"✅ [{name}] 已推送到飞书")

        if args.short:
            print(result["short_summary"])
        elif not args.json:
            # 纯文本输出
            if len(locations) > 1:
                print(f"\n{'─' * 50}")
                print(f"📍 {name}")
                print(f"{'─' * 50}")
            print(result["discord_message"])

    if args.json and all_results:
        if len(all_results) == 1:
            r = all_results[0]
            print(json.dumps({
                "quality": r["quality"],
                "source": r["source"],
                "location": r["location"],
                "date": r["date"],
                "short": r["short_summary"],
            }, ensure_ascii=False, indent=2))
        else:
            print(json.dumps([{
                "location": r["location"],
                "quality": r["quality"],
                "source": r["source"],
                "date": r["date"],
                "short": r["short_summary"],
            } for r in all_results], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
