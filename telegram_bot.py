#!/usr/bin/env python3
import os
import sys
import json
import asyncio
import logging
import statistics as stats_mod
from statistics import median
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from garminconnect import Garmin, GarminConnectAuthenticationError
from telegram.constants import ParseMode

# --------------- Configuration ---------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

TOKEN_DIR = Path(os.environ.get("GARTH_TOKEN_DIR", ".garth_tokens"))

# Session storage per chat
chat_sessions: Dict[int, Dict[str, Any]] = {}


def _session_keys(sess: Dict[str, Any]) -> List[str]:
    return sorted(sess.keys())


def _message_text_category(text: Optional[str], state: Optional[str]) -> str:
    if state == "waiting_password":
        return "password_redacted"
    if text is None:
        return "none"
    stripped = text.strip()
    if not stripped:
        return "empty"
    if "@" in stripped:
        return "email_like"
    return f"text_len_{len(stripped)}"


# --------------- Token persistence (garth) ---------------

def _token_path(chat_id: int) -> Path:
    return TOKEN_DIR / str(chat_id)


def save_garth_tokens(chat_id: int, api: Garmin) -> None:
    """Persist garth OAuth tokens to disk so sessions survive restarts."""
    try:
        token_dir = _token_path(chat_id)
        token_dir.mkdir(parents=True, exist_ok=True)
        api.garth.dump(str(token_dir))
        logger.info("Saved garth tokens for chat %s", chat_id)
    except Exception as e:
        logger.warning("Could not save garth tokens for chat %s: %s", chat_id, e)


def try_restore_session(chat_id: int) -> Optional[Garmin]:
    """Try to restore a Garmin session from persisted garth tokens.

    Loads tokens, calls login, and verifies with a lightweight API call.
    On any failure (expired, network, etc.) clears partial data and returns None.
    """
    token_dir = _token_path(chat_id)
    if not token_dir.exists():
        return None
    try:
        logger.info("Attempting restore from garth tokens for chat %s", chat_id)
        api = Garmin()
        api.garth.load(str(token_dir))
        api.login()
        # lightweight verification call
        api.get_user_profile()
        if hasattr(api.garth, "profile") and api.garth.profile:
            api.display_name = api.garth.profile.get("displayName")
        logger.info("Successfully restored and verified garth session for chat %s", chat_id)
        return api
    except Exception as e:
        logger.warning("Garth restore failed for chat %s: %s — clearing tokens", chat_id, e)
        try:
            import shutil
            shutil.rmtree(token_dir, ignore_errors=True)
        except Exception:
            pass
        return None


# --------------- Garmin helpers ---------------

def ensure_session(chat_id: int) -> Dict[str, Any]:
    sess = chat_sessions.get(chat_id)
    if not sess:
        sess = {}
        chat_sessions[chat_id] = sess
        logger.info("ensure_session created chat_id=%s keys=%s", chat_id, _session_keys(sess))
    else:
        logger.info(
            "ensure_session returned chat_id=%s state=%s keys=%s",
            chat_id,
            sess.get("state"),
            _session_keys(sess),
        )
    return sess


def garmin_login(sess: Dict[str, Any], email: str, password: str, chat_id: int) -> bool:
    try:
        api = Garmin(email, password)
        api.login()
        sess["email"] = email
        sess["password"] = password
        sess["api"] = api
        save_garth_tokens(chat_id, api)
        return True
    except GarminConnectAuthenticationError as e:
        logger.error("Authentication failed: %s", e)
        return False
    except Exception as e:
        logger.error("Login error: %s", e)
        return False


def ensure_api(chat_id: int) -> Optional[Garmin]:
    """Return an active Garmin API, restoring from tokens if needed."""
    sess = ensure_session(chat_id)
    api = sess.get("api")
    if api:
        return api
    api = try_restore_session(chat_id)
    if api:
        sess["api"] = api
        return api
    return None


def get_last_activities(sess: Dict[str, Any], count: int = 10) -> List[Dict[str, Any]]:
    api: Optional[Garmin] = sess.get("api")
    if not api:
        return []
    try:
        acts = api.get_activities(0, max(count, 10))

        def parse_dt(s: Optional[str]) -> datetime:
            if not s:
                return datetime.min
            try:
                if "T" in s:
                    return datetime.fromisoformat(s.replace("Z", "+00:00"))
                return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
            except Exception:
                return datetime.min

        acts.sort(
            key=lambda a: parse_dt(
                a.get("startTimeLocal") or a.get("startTimeGMT") or a.get("startTime")
            ),
            reverse=True,
        )
        return acts[:count]
    except Exception as e:
        logger.error("Failed to fetch recent activities: %s", e)
        return []


def get_activity_splits(api: Garmin, activity_id: int) -> Dict[str, Any]:
    data: Dict[str, Any] = {"typed": None, "splits": None, "summaries": None, "details": None}
    try:
        data["typed"] = api.get_activity_typed_splits(activity_id)
    except Exception:
        pass
    try:
        data["splits"] = api.get_activity_splits(activity_id)
    except Exception:
        pass
    try:
        data["summaries"] = api.get_activity_split_summaries(activity_id)
    except Exception:
        pass
    try:
        data["details"] = api.get_activity_details(activity_id)
    except Exception:
        pass
    return data


def pick_num(d: Dict[str, Any], keys: List[str]) -> Optional[float]:
    for k in keys:
        v = d.get(k)
        try:
            if v is not None:
                return float(v)
        except Exception:
            continue
    return None


def nested_summary(obj: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("summaryDTO", "splitSummaryDTO", "summary", "intervalSummaryDTO"):
        inner = obj.get(key)
        if isinstance(inner, dict):
            return inner
    return obj


def extract_rows_from_container(container: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if isinstance(container, list):
        items = container
    elif isinstance(container, dict):
        for v in container.values():
            if isinstance(v, list) and v:
                items = v
                break
    rows: List[Dict[str, Any]] = []
    for idx, s in enumerate(items, start=1):
        if not isinstance(s, dict):
            continue
        base = nested_summary(s)
        label = (
            s.get("type") or s.get("splitType") or s.get("intensity")
            or s.get("intensityType") or s.get("lapType") or s.get("stepType")
            or base.get("type") or base.get("splitType") or base.get("intensity")
            or base.get("intensityType") or base.get("lapType") or base.get("stepType")
            or "-"
        )
        distance_m = pick_num(base, ["distance", "distanceInMeters", "totalDistance", "totalDistanceMeters", "sumDistance", "lengthInMeters"])
        duration_s = pick_num(base, ["duration", "durationInSeconds", "totalElapsedDuration", "elapsedDuration", "movingDuration", "sumDuration"])
        avg_speed = pick_num(base, ["averageSpeed", "avgSpeed", "meanSpeed"])
        avg_hr = pick_num(base, ["averageHR", "avgHR", "averageHeartRate"])
        med_hr = pick_num(base, ["medianHR", "medianHeartRate"]) or avg_hr
        max_hr = pick_num(base, ["maxHR", "maxHeartRate", "maximumHR"])
        lap_no = s.get("splitNumber") or s.get("lapNumber") or s.get("lapIndex") or s.get("number") or idx
        rows.append({
            "lap": lap_no,
            "type": str(label),
            "distance_m": distance_m,
            "duration_s": duration_s,
            "avg_speed": avg_speed,
            "median_hr": med_hr,
            "max_hr": max_hr,
        })
    return rows


def collect_rows_for_activity(api: Garmin, activity_id: int) -> List[Dict[str, Any]]:
    payload = get_activity_splits(api, activity_id)
    for key in ("typed", "splits", "summaries"):
        rows = extract_rows_from_container(payload.get(key))
        if rows:
            return rows
    details = payload.get("details") or {}
    for path in (
        ["laps"], ["lapDTOs"],
        ["activityDetailDTO", "lapDTOs"],
        ["activityDetail", "lapDTOs"],
        ["splitSummaries"],
    ):
        obj: Any = details
        for k in path:
            obj = obj.get(k) if isinstance(obj, dict) else None
            if obj is None:
                break
        rows = extract_rows_from_container(obj)
        if rows:
            return rows
    return []


def normalize_label(t: str) -> str:
    t = (t or "").upper()
    if t == "INTERVAL_ACTIVE":
        return "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b"
    if t == "INTERVAL_WARMUP":
        return "\u0440\u0430\u0437\u043c\u0438\u043d\u043a\u0430"
    if t == "INTERVAL_RECOVERY":
        return "\u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435"
    if t == "INTERVAL_COOLDOWN":
        return "\u0437\u0430\u043c\u0438\u043d\u043a\u0430"
    if t.startswith("RWD_RUN") or t.startswith("RWD_WALK") or t.startswith("RWD_STAND"):
        return "\u0431\u0435\u0433"
    if t in ("LAP", "SPLIT", "DISTANCE") or t.strip() == "-":
        return "\u0431\u0435\u0433"
    return t.lower()


def aggregate_segments(rows: List[Dict[str, Any]], include_sub_laps: bool = False) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    last_interval: Optional[Dict[str, Any]] = None

    for r in rows:
        label = normalize_label(r.get("type", ""))
        raw_type = (r.get("type") or "").upper()
        distance = r.get("distance_m") or 0
        duration = r.get("duration_s") or 0
        med_hr = r.get("median_hr")
        speed = r.get("avg_speed")

        is_lap = raw_type in ("LAP", "SPLIT", "DISTANCE") or raw_type.strip() == "-"

        if include_sub_laps and is_lap and last_interval is not None:
            sub_lap_pace = fmt_pace(speed, distance, duration)
            last_interval.setdefault("sub_laps", []).append({
                "distance_m": distance,
                "duration_s": duration,
                "pace": sub_lap_pace,
            })
            last_interval["distance_m"] = (last_interval.get("distance_m") or 0) + distance
            last_interval["duration_s"] = (last_interval.get("duration_s") or 0) + duration
            continue

        if label == "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b":
            if current:
                if (current.get("distance_m") or 0) > 0 and (current.get("duration_s") or 0) > 0:
                    current["avg_speed"] = current["distance_m"] / current["duration_s"]
                result.append(current)
                current = None

            if last_interval and last_interval.get("sub_laps"):
                d = last_interval.get("distance_m") or 0
                t = last_interval.get("duration_s") or 0
                if d > 0 and t > 0:
                    last_interval["avg_speed"] = d / t

            new_interval = {
                "label": label,
                "distance_m": distance,
                "duration_s": duration,
                "avg_speed": speed if speed else (distance / duration if distance and duration else None),
                "hrs": [med_hr] if med_hr is not None else [],
            }
            result.append(new_interval)
            last_interval = new_interval
            continue

        if last_interval and last_interval.get("sub_laps"):
            d = last_interval.get("distance_m") or 0
            t = last_interval.get("duration_s") or 0
            if d > 0 and t > 0:
                last_interval["avg_speed"] = d / t
        last_interval = None

        if current and current.get("label") == label:
            current["distance_m"] = (current.get("distance_m") or 0) + distance
            current["duration_s"] = (current.get("duration_s") or 0) + duration
            if med_hr is not None:
                current.setdefault("hrs", []).append(med_hr)
        else:
            if current:
                if (current.get("distance_m") or 0) > 0 and (current.get("duration_s") or 0) > 0:
                    current["avg_speed"] = current["distance_m"] / current["duration_s"]
                result.append(current)
            current = {
                "label": label,
                "distance_m": distance,
                "duration_s": duration,
                "avg_speed": speed if speed else (distance / duration if distance and duration else None),
                "hrs": [med_hr] if med_hr is not None else [],
            }

    if current:
        if (current.get("distance_m") or 0) > 0 and (current.get("duration_s") or 0) > 0:
            current["avg_speed"] = current["distance_m"] / current["duration_s"]
        result.append(current)

    if last_interval and last_interval.get("sub_laps"):
        d = last_interval.get("distance_m") or 0
        t = last_interval.get("duration_s") or 0
        if d > 0 and t > 0:
            last_interval["avg_speed"] = d / t

    for seg in result:
        hr_list = seg.get("hrs") or []
        seg["median_hr"] = int(median(hr_list)) if hr_list else None
    return result


# --------------- GPS smoothing (advanced) ---------------

STANDARD_DISTANCES = [100, 200, 400, 800, 1000]
NORMALIZATION_THRESHOLD = 15  # meters


def normalize_distance(meters: Optional[float]) -> Optional[float]:
    """Snap GPS distance to nearest standard track distance if within threshold."""
    if not meters or meters <= 0:
        return meters
    for std_dist in STANDARD_DISTANCES:
        if abs(meters - std_dist) <= NORMALIZATION_THRESHOLD:
            return float(std_dist)
    return meters


def find_dominant_distance(segments: List[Dict[str, Any]]) -> float:
    """Auto-detect the dominant interval distance (mode of rounded values).

    Uses 50 m rounding so 390-410 m all bucket to 400, etc.
    Falls back to median, then 400 m.
    """
    distances = [
        seg.get("distance_m") or 0
        for seg in segments
        if seg.get("label") == "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b" and (seg.get("distance_m") or 0) > 0
    ]
    if not distances:
        return 400.0
    rounded = [round(d / 50) * 50 for d in distances]
    try:
        return float(stats_mod.mode(rounded))
    except stats_mod.StatisticsError:
        return float(stats_mod.median(rounded))


def smooth_interval_pace(duration_s: float, real_distance: float, dominant: float) -> float:
    """Proportionally rescale time to the dominant distance.

    effective_time = (duration / real_distance) * dominant_distance
    Only applied when real_distance is within 10 % of dominant.
    """
    if real_distance <= 0 or duration_s <= 0:
        return duration_s
    if abs(real_distance - dominant) / dominant <= 0.10:
        return (duration_s / real_distance) * dominant
    return duration_s


def fmt_smooth_time(seconds: float) -> str:
    """Format seconds to m:ss for diary compact output."""
    total = int(round(seconds))
    m = total // 60
    s = total % 60
    return f"{m}:{s:02d}"


def compact_interval_pace_seconds(duration_s: float, display_distance: float) -> float:
    """Return compact interval pace in seconds per kilometer.

    Compact output labels intervals by the same normalized distance shown in
    detailed output, so its pace should use that normalized distance too.
    """
    if display_distance <= 0 or duration_s <= 0:
        return duration_s
    return duration_s / (display_distance / 1000.0)


# --------------- Formatting helpers ---------------

def fmt_pace(avg_speed: Optional[float], distance: Optional[float], duration: Optional[float]) -> str:
    if avg_speed and avg_speed > 0:
        spk = 1000.0 / avg_speed
    elif distance and duration and distance > 0:
        spk = duration * 1000.0 / distance
    else:
        return "-"
    m = int(spk // 60)
    s = int(round(spk % 60))
    if s == 60:
        m += 1
        s = 0
    return f"{m}:{s:02d}"


def fmt_distance(meters: Optional[float]) -> str:
    if not meters or meters <= 0:
        return "-"
    return f"{meters / 1000:.2f} \u043a\u043c" if meters >= 1000 else f"{int(round(meters))} \u043c"


def escape_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def get_activity_header(sess: Dict[str, Any], activity_id: int) -> str:
    api: Optional[Garmin] = sess.get("api")
    start = None
    for a in sess.get("last_activities", []) or []:
        aid = a.get("activityId") or a.get("id") or a.get("activityIdLong")
        if str(aid) == str(activity_id):
            start = a.get("startTimeLocal") or a.get("startTimeGMT") or a.get("startTime")
            break
    if not start and api:
        try:
            act = api.get_activity(activity_id)
            start = act.get("startTimeLocal") or act.get("startTimeGMT") or act.get("startTime")
        except Exception:
            start = None
    try:
        if start and "T" in start:
            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        elif start:
            dt = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
        else:
            return "\u041f\u043e\u0434\u0440\u043e\u0431\u043d\u044b\u0439 \u0430\u043d\u0430\u043b\u0438\u0437 \u043f\u043e \u043a\u0440\u0443\u0433\u0430\u043c:"
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return "\u041f\u043e\u0434\u0440\u043e\u0431\u043d\u044b\u0439 \u0430\u043d\u0430\u043b\u0438\u0437 \u043f\u043e \u043a\u0440\u0443\u0433\u0430\u043c:"


def get_activity_summary(sess: Dict[str, Any], activity_id: int) -> Dict[str, Optional[float]]:
    api: Optional[Garmin] = sess.get("api")
    if not api:
        return {}

    summary: Dict[str, Optional[float]] = {}

    for a in sess.get("last_activities", []) or []:
        aid = a.get("activityId") or a.get("id") or a.get("activityIdLong")
        if str(aid) == str(activity_id):
            summary = {
                "distance_m": pick_num(a, ["distance", "distanceInMeters"]),
                "avg_speed": pick_num(a, ["averageSpeed", "avgSpeed"]),
                "avg_hr": pick_num(a, ["averageHR", "avgHR", "averageHeartRate"]),
                "max_hr": pick_num(a, ["maxHR", "maxHeartRate", "maximumHR"]),
                "duration_s": pick_num(a, ["duration", "durationInSeconds", "elapsedDuration"]),
            }
            if all(v is not None for v in [summary["distance_m"], (summary["avg_speed"] or summary["duration_s"]), summary["avg_hr"]]):
                return summary
            break

    try:
        act = api.get_activity(activity_id)
        summary = {
            "distance_m": pick_num(act, ["distance", "distanceInMeters"]),
            "avg_speed": pick_num(act, ["averageSpeed", "avgSpeed"]),
            "avg_hr": pick_num(act, ["averageHR", "avgHR", "averageHeartRate"]),
            "max_hr": pick_num(act, ["maxHR", "maxHeartRate", "maximumHR"]),
            "duration_s": pick_num(act, ["duration", "durationInSeconds", "elapsedDuration"]),
        }
        if summary["distance_m"] is None or summary["avg_hr"] is None:
            try:
                details = api.get_activity_details(activity_id)
                summary_dto = details.get("summaryDTO") or details.get("summary") or {}
                for field, keys in [
                    ("distance_m", ["distance", "distanceInMeters"]),
                    ("avg_speed", ["averageSpeed", "avgSpeed"]),
                    ("avg_hr", ["averageHR", "avgHR", "averageHeartRate"]),
                    ("max_hr", ["maxHR", "maxHeartRate", "maximumHR"]),
                    ("duration_s", ["duration", "durationInSeconds", "elapsedDuration"]),
                ]:
                    if summary.get(field) is None:
                        summary[field] = pick_num(summary_dto, keys)
            except Exception:
                pass
    except Exception:
        pass

    return summary


# --------------- Message builders ---------------

def group_intervals(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: List[Dict[str, Any]] = []
    i = 0
    while i < len(segments):
        seg = segments[i]
        if seg.get("label") == "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b":
            if i + 1 < len(segments) and segments[i + 1].get("label") == "\u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435":
                grouped.append({"type": "interval_pair", "interval": seg, "recovery": segments[i + 1]})
                i += 2
            else:
                grouped.append(seg)
                i += 1
        else:
            grouped.append(seg)
            i += 1
    return grouped


def build_compact_intervals_message(segments: List[Dict[str, Any]], header: Optional[str] = None) -> str:
    """Diary-ready compact format using normalized interval distances.

    Output example: ``10x400: 3:48, 3:50, 3:42, 3:20``
    """
    title = header or "\u0434\u043b\u044f \u0434\u043d\u0435\u0432\u043d\u0438\u043a\u0430:"

    groups: List[Tuple[int, List[str]]] = []

    for seg in segments:
        if seg.get("label") != "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b":
            continue
        raw_dist = seg.get("distance_m") or 0
        dur = seg.get("duration_s") or 0
        if raw_dist <= 0 or dur <= 0:
            continue

        norm_dist = normalize_distance(raw_dist)
        dist_key = int(round(norm_dist)) if norm_dist else int(round(raw_dist))
        pace_seconds = compact_interval_pace_seconds(dur, float(dist_key))
        pace_str = fmt_smooth_time(pace_seconds)

        if groups and groups[-1][0] == dist_key:
            groups[-1][1].append(pace_str)
        else:
            groups.append((dist_key, [pace_str]))

    lines: List[str] = [f"{escape_html(title)}\n"]
    for dist, paces in groups:
        count = len(paces)
        paces_str = ", ".join(paces)
        line = f"{count}x{dist}: {paces_str}"
        lines.append(escape_html(line))

    return "\n".join(lines)


def build_detailed_intervals_message(segments: List[Dict[str, Any]], header: Optional[str] = None) -> str:
    title = header or "\u041f\u043e\u0434\u0440\u043e\u0431\u043d\u044b\u0439 \u0430\u043d\u0430\u043b\u0438\u0437 \u0441 \u0440\u0430\u0437\u0431\u0438\u0432\u043a\u043e\u0439:"
    lines: List[str] = [f"{escape_html(title)}\n"]

    grouped_segments = group_intervals(segments)
    interval_count = 0

    for seg in grouped_segments:
        if isinstance(seg, dict) and seg.get("type") == "interval_pair":
            interval_count += 1
            interval = seg["interval"]
            recovery = seg["recovery"]

            raw_int_dist = interval.get("distance_m")
            norm_int_dist = normalize_distance(raw_int_dist)
            int_dist = fmt_distance(norm_int_dist)
            int_pace = fmt_pace(None, norm_int_dist, interval.get("duration_s"))

            sub_laps = interval.get("sub_laps") or []
            if sub_laps:
                sub_paces = ", ".join(sl.get("pace", "-") for sl in sub_laps)
                int_part = f"{int_dist} - {int_pace} ({sub_paces})"
            else:
                int_part = f"{int_dist} - {int_pace}"

            rec_dist = fmt_distance(recovery.get("distance_m"))
            rec_pace = fmt_pace(recovery.get("avg_speed"), recovery.get("distance_m"), recovery.get("duration_s"))
            rec_dur = str(timedelta(seconds=int(recovery.get("duration_s") or 0))) if recovery.get("duration_s") else "-"

            line = f"{interval_count}) {int_part} / {rec_dist} - {rec_pace} ({rec_dur})"
            lines.append(f"<b>{escape_html(line)}</b>")

        elif isinstance(seg, dict) and seg.get("label") == "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b":
            interval_count += 1
            raw_dist = seg.get("distance_m")
            norm_dist = normalize_distance(raw_dist)
            dist = fmt_distance(norm_dist)
            pace = fmt_pace(None, norm_dist, seg.get("duration_s"))

            sub_laps = seg.get("sub_laps") or []
            if sub_laps:
                sub_paces = ", ".join(sl.get("pace", "-") for sl in sub_laps)
                line = f"{interval_count}) {dist} - {pace} ({sub_paces})"
            else:
                line = f"{interval_count}) {dist} - {pace}"

            lines.append(f"<b>{escape_html(line)}</b>")

        else:
            raw_label = seg.get("label", "-")
            label = escape_html(raw_label)
            dist = escape_html(fmt_distance(seg.get("distance_m")))
            pace = escape_html(fmt_pace(seg.get("avg_speed"), seg.get("distance_m"), seg.get("duration_s")))
            dur_val = str(timedelta(seconds=int(seg.get("duration_s") or 0))) if seg.get("duration_s") else "-"
            dur = escape_html(dur_val)
            hr = escape_html(f"{seg.get('median_hr')} \u0443\u0434/\u043c\u0438\u043d" if seg.get("median_hr") else "-")
            line = f"{label} ({dist}, {pace}, {dur}, \u0427\u0421\u0421: {hr})"
            lines.append(line)

    return "\n".join(lines)


def build_laps_message(
    segments: List[Dict[str, Any]],
    header: Optional[str] = None,
    summary: Optional[Dict[str, Optional[float]]] = None,
) -> str:
    title = header or "\u041f\u043e\u0434\u0440\u043e\u0431\u043d\u044b\u0439 \u0430\u043d\u0430\u043b\u0438\u0437 \u043f\u043e \u043a\u0440\u0443\u0433\u0430\u043c:"
    lines: List[str] = [f"{escape_html(title)}\n"]

    has_intervals = any(seg.get("label") == "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b" for seg in segments)

    if has_intervals:
        grouped_segments = group_intervals(segments)
        interval_count = 0

        for seg in grouped_segments:
            if isinstance(seg, dict) and seg.get("type") == "interval_pair":
                interval_count += 1
                interval = seg["interval"]
                recovery = seg["recovery"]

                raw_int_dist = interval.get("distance_m")
                norm_int_dist = normalize_distance(raw_int_dist)
                int_dist = fmt_distance(norm_int_dist)
                int_pace = fmt_pace(None, norm_int_dist, interval.get("duration_s"))

                rec_dist = fmt_distance(recovery.get("distance_m"))
                rec_pace = fmt_pace(recovery.get("avg_speed"), recovery.get("distance_m"), recovery.get("duration_s"))
                rec_dur = str(timedelta(seconds=int(recovery.get("duration_s") or 0))) if recovery.get("duration_s") else "-"

                line = f"{interval_count}) {int_dist} - {int_pace} / {rec_dist} - {rec_pace} ({rec_dur})"
                lines.append(f"<b>{escape_html(line)}</b>")

            elif isinstance(seg, dict) and seg.get("label") == "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b":
                interval_count += 1
                raw_dist = seg.get("distance_m")
                norm_dist = normalize_distance(raw_dist)
                dist = fmt_distance(norm_dist)
                pace = fmt_pace(None, norm_dist, seg.get("duration_s"))

                line = f"{interval_count}) {dist} - {pace}"
                lines.append(f"<b>{escape_html(line)}</b>")
    else:
        if summary:
            distance_m = summary.get("distance_m")
            avg_speed = summary.get("avg_speed")
            duration_s = summary.get("duration_s")
            avg_hr = summary.get("avg_hr")
            max_hr = summary.get("max_hr")

            dist_str = fmt_distance(distance_m)
            pace_str = fmt_pace(avg_speed, distance_m, duration_s)

            hr_parts: List[str] = []
            if avg_hr:
                hr_parts.append(str(int(round(avg_hr))))
            if max_hr:
                hr_parts.append(str(int(round(max_hr))))

            line_parts = [dist_str, pace_str]
            if hr_parts:
                if len(hr_parts) == 2:
                    line_parts.append(f"\u0427\u0421\u0421 {hr_parts[0]} / {hr_parts[1]}")
                elif len(hr_parts) == 1:
                    line_parts.append(f"\u0427\u0421\u0421 {hr_parts[0]}")

            lines.append(escape_html(", ".join(line_parts)))
        else:
            for idx, seg in enumerate(segments, start=1):
                raw_label = seg.get("label", "-")
                label = escape_html(raw_label)
                dist = escape_html(fmt_distance(seg.get("distance_m")))
                pace = escape_html(fmt_pace(seg.get("avg_speed"), seg.get("distance_m"), seg.get("duration_s")))
                avg_hr = seg.get("median_hr")
                max_hr_val = seg.get("max_hr")

                if raw_label == "\u0431\u0435\u0433":
                    if avg_hr and max_hr_val:
                        line = f"{idx}. {dist}, {pace}, \u0427\u0421\u0421 {avg_hr} (\u043c\u0430\u043a\u0441 {max_hr_val})"
                    elif avg_hr:
                        line = f"{idx}. {dist}, {pace}, \u0427\u0421\u0421 {avg_hr}"
                    else:
                        line = f"{idx}. {dist}, {pace}"
                else:
                    dur_val = str(timedelta(seconds=int(seg.get("duration_s") or 0))) if seg.get("duration_s") else "-"
                    dur = escape_html(dur_val)
                    hr = escape_html(f"{avg_hr} \u0443\u0434/\u043c\u0438\u043d" if avg_hr else "-")
                    line = f"{idx}. {label} ({dist}, {pace}, {dur}, \u0427\u0421\u0421: {hr})"

                if raw_label == "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b":
                    line = f"<b>{line}</b>"
                lines.append(line)

    return "\n".join(lines)


def derive_km_rows(api: Garmin, activity_id: int) -> List[Dict[str, Any]]:
    try:
        details = api.get_activity_details(activity_id)
    except Exception:
        details = {}
    candidates: List[Dict[str, Any]] = []
    splits = details.get("splitSummaries")
    if isinstance(splits, list):
        for item in splits:
            if isinstance(item, dict) and (
                str(item.get("unitKey", "")).lower() in {"kilometer", "kilometers", "km"}
                or str(item.get("unit", "")).lower() in {"kilometer", "kilometers", "km"}
                or str(item.get("unitName", "")).lower() in {"kilometer", "kilometers", "km"}
            ):
                inner = item.get("summaries") or item.get("summaryDTOs") or item.get("splits")
                if isinstance(inner, list):
                    candidates.extend(inner)
                else:
                    candidates.append(item)
            elif isinstance(item, dict):
                candidates.append(item)
    if not candidates:
        payload = get_activity_splits(api, activity_id)
        rows: List[Dict[str, Any]] = []
        for key in ("splits", "summaries", "typed"):
            cont = payload.get(key)
            if cont:
                rows = extract_rows_from_container(cont)
                break
    else:
        rows = extract_rows_from_container(candidates)
    filtered: List[Dict[str, Any]] = []
    for r in rows:
        d = r.get("distance_m") or 0
        if d <= 0:
            continue
        if d < 1200:
            filtered.append(r)
    return filtered or rows


def rows_to_segments_no_agg(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    segments: List[Dict[str, Any]] = []
    for r in rows:
        dist = r.get("distance_m")
        dur = r.get("duration_s")
        speed = r.get("avg_speed") or (dist / dur if dist and dur else None)
        segments.append({
            "label": "\u0431\u0435\u0433",
            "distance_m": dist,
            "duration_s": dur,
            "avg_speed": speed,
            "median_hr": r.get("median_hr"),
            "max_hr": r.get("max_hr"),
        })
    return segments


# --------------- Shared UI helpers ---------------

def _format_activity_for_display(
    sess: Dict[str, Any], activity_id: int
) -> Tuple[str, InlineKeyboardMarkup, bool]:
    """Fetch data and build the default message + keyboard for an activity.

    Returns (message_html, keyboard, is_interval).
    """
    rows = collect_rows_for_activity(sess["api"], activity_id)
    if not rows:
        return "\u274c \u0414\u0430\u043d\u043d\u044b\u0435 \u043f\u043e \u043a\u0440\u0443\u0433\u0430\u043c \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u044b.", InlineKeyboardMarkup([]), False

    normalized_types = [normalize_label(r.get("type", "")) for r in rows]
    count_interval = sum(1 for t in normalized_types if t == "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b")
    has_other = any(t in {"\u0440\u0430\u0437\u043c\u0438\u043d\u043a\u0430", "\u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435", "\u0437\u0430\u043c\u0438\u043d\u043a\u0430"} for t in normalized_types)
    manual_intervals = has_other or count_interval > 1
    header = get_activity_header(sess, activity_id)

    if manual_intervals:
        keep = {"\u0440\u0430\u0437\u043c\u0438\u043d\u043a\u0430", "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b", "\u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435", "\u0437\u0430\u043c\u0438\u043d\u043a\u0430", "\u0431\u0435\u0433"}
        filtered_rows = [r for r in rows if normalize_label(r.get("type", "")) in keep]
        segments = aggregate_segments(filtered_rows)
        segments_with_sublaps = aggregate_segments(filtered_rows, include_sub_laps=True)
        sess["cached_segments"] = segments
        sess["cached_segments_sublaps"] = segments_with_sublaps
        sess["cached_header"] = header
        sess["cached_activity_id"] = activity_id
        message = build_compact_intervals_message(segments, header)
        nav = InlineKeyboardMarkup([
            [InlineKeyboardButton("Подробный формат", callback_data=f"full_{activity_id}")],
            [InlineKeyboardButton("\u2b05\ufe0f \u041d\u0430\u0437\u0430\u0434 \u043a \u0441\u043f\u0438\u0441\u043a\u0443", callback_data="back_to_list")],
            [InlineKeyboardButton("На главную", callback_data="go_home")],
        ])
        return message, nav, True
    else:
        summary = get_activity_summary(sess, activity_id)
        message = build_laps_message([], header, summary=summary)
        nav = InlineKeyboardMarkup([
            [InlineKeyboardButton("\u2b05\ufe0f \u041d\u0430\u0437\u0430\u0434 \u043a \u0441\u043f\u0438\u0441\u043a\u0443", callback_data="back_to_list")],
            [InlineKeyboardButton("На главную", callback_data="go_home")],
        ])
        return message, nav, False


def _build_activity_list_keyboard(acts: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    keyboard = []
    for a in acts:
        title = a.get("activityName") or a.get("activityType", {}).get("typeKey", "\u0422\u0440\u0435\u043d\u0438\u0440\u043e\u0432\u043a\u0430")
        start = a.get("startTimeLocal") or a.get("startTimeGMT") or a.get("startTime")
        try:
            dt = (
                datetime.fromisoformat(start.replace("Z", "+00:00"))
                if start and "T" in start
                else datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
            )
            title_dt = dt.strftime("%d.%m %H:%M")
        except Exception:
            title_dt = start or ""
        aid = a.get("activityId") or a.get("id") or a.get("activityIdLong")
        if aid:
            keyboard.append([InlineKeyboardButton(f"{title_dt} \u2014 {title}", callback_data=f"activity_{aid}")])
    return InlineKeyboardMarkup(keyboard)


def _append_keyboard_rows(
    markup: InlineKeyboardMarkup, rows: List[List[InlineKeyboardButton]]
) -> InlineKeyboardMarkup:
    """Return a new keyboard with additional rows.

    python-telegram-bot v20 exposes inline_keyboard as an immutable tuple.
    """
    return InlineKeyboardMarkup([*markup.inline_keyboard, *rows])


# --------------- Telegram handlers ---------------

def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Garmin Extractor", callback_data="garmin_extractor"),
            InlineKeyboardButton("\u23f1 Pace Calculator", callback_data="pace_calculator"),
        ]
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Добро пожаловать в BegovayaKuznitsa_Bot!\n\nВыберите действие:",
        reply_markup=build_main_menu_keyboard(),
    )


async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear persisted Garmin tokens and session for this chat."""
    chat_id = update.effective_chat.id
    token_dir = _token_path(chat_id)
    try:
        import shutil
        shutil.rmtree(token_dir, ignore_errors=True)
    except Exception:
        pass
    chat_sessions.pop(chat_id, None)
    await update.message.reply_text(
        "Вы вышли из аккаунта Garmin. Нажмите /start чтобы войти заново."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "<b>BegovayaKuznitsa_Bot</b>\n\n"
        "<b>\u041a\u043e\u043c\u0430\u043d\u0434\u044b:</b>\n"
        "/start \u2014 \u0433\u043b\u0430\u0432\u043d\u043e\u0435 \u043c\u0435\u043d\u044e\n"
        "/last \u2014 \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u044f\u044f \u0431\u0435\u0433\u043e\u0432\u0430\u044f \u0442\u0440\u0435\u043d\u0438\u0440\u043e\u0432\u043a\u0430 (\u0431\u044b\u0441\u0442\u0440\u044b\u0439 \u0444\u043e\u0440\u043c\u0430\u0442 \u0434\u043b\u044f \u0434\u043d\u0435\u0432\u043d\u0438\u043a\u0430)\n"
        "/format &lt;activity_id&gt; \u2014 \u0444\u043e\u0440\u043c\u0430\u0442\u0438\u0440\u043e\u0432\u0430\u0442\u044c \u043a\u043e\u043d\u043a\u0440\u0435\u0442\u043d\u0443\u044e \u0442\u0440\u0435\u043d\u0438\u0440\u043e\u0432\u043a\u0443\n"
        "/logout \u2014 \u0432\u044b\u0445\u043e\u0434 \u0438\u0437 \u0430\u043a\u043a\u0430\u0443\u043d\u0442\u0430 Garmin (\u0441\u0431\u0440\u043e\u0441 \u0441\u043e\u0445\u0440\u0430\u043d\u0451\u043d\u043d\u044b\u0445 \u0434\u0430\u043d\u043d\u044b\u0445)\n"
        "/help \u2014 \u044d\u0442\u0430 \u0441\u043f\u0440\u0430\u0432\u043a\u0430\n\n"
        "<b>\u041f\u043e\u0441\u0442\u043e\u044f\u043d\u043d\u0430\u044f \u0430\u0432\u0442\u043e\u0440\u0438\u0437\u0430\u0446\u0438\u044f:</b>\n"
        "\u041f\u043e\u0441\u043b\u0435 \u043f\u0435\u0440\u0432\u043e\u0433\u043e \u0443\u0441\u043f\u0435\u0448\u043d\u043e\u0433\u043e \u0432\u0445\u043e\u0434\u0430 \u0442\u043e\u043a\u0435\u043d\u044b \u0441\u043e\u0445\u0440\u0430\u043d\u044f\u044e\u0442\u0441\u044f \u0438 \u0432\u044b \u043d\u0435 \u0431\u0443\u0434\u0435\u0442\u0435 \u0432\u0432\u043e\u0434\u0438\u0442\u044c email/\u043f\u0430\u0440\u043e\u043b\u044c \u043f\u043e\u0441\u043b\u0435 \u043f\u0435\u0440\u0435\u0437\u0430\u043f\u0443\u0441\u043a\u0430 \u0431\u043e\u0442\u0430.\n\n"
        "<b>\u041a\u0430\u043a \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u044c\u0441\u044f:</b>\n"
        "1. /start \u2192 Garmin Extractor \u2014 \u043f\u0435\u0440\u0432\u044b\u0439 \u0440\u0430\u0437 \u0432\u0432\u0435\u0434\u0438\u0442\u0435 email/\u043f\u0430\u0440\u043e\u043b\u044c, \u0434\u0430\u043b\u0435\u0435 \u0430\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u0435\u0441\u043a\u0438\n"
        "2. \u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0442\u0440\u0435\u043d\u0438\u0440\u043e\u0432\u043a\u0443 \u0438\u0437 \u0441\u043f\u0438\u0441\u043a\u0430\n"
        "3. \u0411\u043e\u0442 \u043f\u043e\u043a\u0430\u0436\u0435\u0442 \u043a\u043e\u043c\u043f\u0430\u043a\u0442\u043d\u044b\u0439 \u0444\u043e\u0440\u043c\u0430\u0442 \u0434\u043b\u044f \u0434\u043d\u0435\u0432\u043d\u0438\u043a\u0430\n"
        "4. \u041a\u043d\u043e\u043f\u043a\u0430\u043c\u0438 \u043f\u0435\u0440\u0435\u043a\u043b\u044e\u0447\u0430\u0439\u0442\u0435 \u0444\u043e\u0440\u043c\u0430\u0442 (\u043f\u043e\u0434\u0440\u043e\u0431\u043d\u044b\u0439 / \u043a\u043e\u043c\u043f\u0430\u043a\u0442\u043d\u044b\u0439)\n\n"
        "<b>\u041f\u0440\u0438\u043c\u0435\u0440\u044b:</b>\n"
        "<code>/last</code> \u2014 \u0431\u044b\u0441\u0442\u0440\u044b\u0439 \u0432\u044b\u0432\u043e\u0434 \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u0435\u0439 \u043f\u0440\u043e\u0431\u0435\u0436\u043a\u0438\n"
        "<code>/format 123456789</code> \u2014 \u0444\u043e\u0440\u043c\u0430\u0442 \u043a\u043e\u043d\u043a\u0440\u0435\u0442\u043d\u043e\u0439 \u0442\u0440\u0435\u043d\u0438\u0440\u043e\u0432\u043a\u0438"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def last_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetch the most recent running activity and show diary-ready format."""
    chat_id = update.effective_chat.id
    api = ensure_api(chat_id)
    if not api:
        await update.message.reply_text(
            "\u274c \u0421\u043d\u0430\u0447\u0430\u043b\u0430 \u0430\u0432\u0442\u043e\u0440\u0438\u0437\u0443\u0439\u0442\u0435\u0441\u044c: /start \u2192 Garmin Extractor"
        )
        return

    sess = ensure_session(chat_id)
    await update.message.reply_text("\u23f3 \u041f\u043e\u043b\u0443\u0447\u0430\u044e \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u044e\u044e \u0442\u0440\u0435\u043d\u0438\u0440\u043e\u0432\u043a\u0443...")

    acts = get_last_activities(sess, 10)
    if not acts:
        await update.message.reply_text("\u274c \u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043f\u043e\u043b\u0443\u0447\u0438\u0442\u044c \u0441\u043f\u0438\u0441\u043e\u043a \u0442\u0440\u0435\u043d\u0438\u0440\u043e\u0432\u043e\u043a.")
        return

    sess["last_activities"] = acts
    running_act = None
    for a in acts:
        atype = a.get("activityType", {})
        type_key = (atype.get("typeKey") or "").lower() if isinstance(atype, dict) else ""
        parent_key = (atype.get("parentTypeId") if isinstance(atype, dict) else None)
        if "run" in type_key or parent_key == 1:
            running_act = a
            break
    if not running_act:
        running_act = acts[0]

    aid = running_act.get("activityId") or running_act.get("id") or running_act.get("activityIdLong")
    if not aid:
        await update.message.reply_text("\u274c \u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u043f\u0440\u0435\u0434\u0435\u043b\u0438\u0442\u044c ID \u0442\u0440\u0435\u043d\u0438\u0440\u043e\u0432\u043a\u0438.")
        return

    try:
        message, nav, _ = _format_activity_for_display(sess, int(aid))
        await update.message.reply_text(
            message, reply_markup=nav, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )
    except Exception as e:
        logger.error("Error in /last: %s", e)
        await update.message.reply_text("\u274c \u041e\u0448\u0438\u0431\u043a\u0430 \u043f\u0440\u0438 \u043f\u043e\u043b\u0443\u0447\u0435\u043d\u0438\u0438 \u0434\u0430\u043d\u043d\u044b\u0445. \u041f\u043e\u043f\u0440\u043e\u0431\u0443\u0439\u0442\u0435 \u0435\u0449\u0435 \u0440\u0430\u0437.")


async def format_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Format a specific activity by ID: /format <activity_id>"""
    chat_id = update.effective_chat.id
    api = ensure_api(chat_id)
    if not api:
        await update.message.reply_text(
            "\u274c \u0421\u043d\u0430\u0447\u0430\u043b\u0430 \u0430\u0432\u0442\u043e\u0440\u0438\u0437\u0443\u0439\u0442\u0435\u0441\u044c: /start \u2192 Garmin Extractor"
        )
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "\u0418\u0441\u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u043d\u0438\u0435: <code>/format &lt;activity_id&gt;</code>\n\n"
            "\u041f\u0440\u0438\u043c\u0435\u0440: <code>/format 123456789</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        activity_id = int(args[0])
    except ValueError:
        await update.message.reply_text("\u274c activity_id \u0434\u043e\u043b\u0436\u0435\u043d \u0431\u044b\u0442\u044c \u0447\u0438\u0441\u043b\u043e\u043c.")
        return

    sess = ensure_session(chat_id)
    await update.message.reply_text("\u23f3 \u041f\u043e\u043b\u0443\u0447\u0430\u044e \u0434\u0430\u043d\u043d\u044b\u0435...")

    try:
        message, nav, _ = _format_activity_for_display(sess, activity_id)
        await update.message.reply_text(
            message, reply_markup=nav, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )
    except Exception as e:
        logger.error("Error in /format: %s", e)
        await update.message.reply_text("\u274c \u041e\u0448\u0438\u0431\u043a\u0430 \u043f\u0440\u0438 \u043f\u043e\u043b\u0443\u0447\u0435\u043d\u0438\u0438 \u0434\u0430\u043d\u043d\u044b\u0445. \u041f\u043e\u043f\u0440\u043e\u0431\u0443\u0439\u0442\u0435 \u0435\u0449\u0435 \u0440\u0430\u0437.")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    sess = ensure_session(chat_id)
    state_before = sess.get("state")
    logger.info(
        "button_handler chat_id=%s data=%s state_before=%s keys=%s",
        chat_id,
        query.data,
        state_before,
        _session_keys(sess),
    )

    api = ensure_api(chat_id)
    if api:
        sess["api"] = api
        acts = get_last_activities(sess, 10)
        if acts:
            sess["last_activities"] = acts
            keyboard = _build_activity_list_keyboard(acts)
            await query.edit_message_text("\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0442\u0440\u0435\u043d\u0438\u0440\u043e\u0432\u043a\u0443:", reply_markup=keyboard)
            return
        await query.edit_message_text(
            "\u274c \u0421\u0435\u0441\u0441\u0438\u044f \u0430\u043a\u0442\u0438\u0432\u043d\u0430, \u043d\u043e \u0442\u0440\u0435\u043d\u0438\u0440\u043e\u0432\u043a\u0438 \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u044b. \u041f\u043e\u043f\u0440\u043e\u0431\u0443\u0439\u0442\u0435 \u043f\u043e\u0437\u0436\u0435.",
            reply_markup=build_main_menu_keyboard(),
        )
        return

    sess = ensure_session(chat_id)
    sess["state"] = "waiting_email"
    logger.info(
        "button_handler chat_id=%s data=%s state_after=%s keys=%s",
        chat_id,
        query.data,
        sess.get("state"),
        _session_keys(sess),
    )
    await query.message.reply_text("Введите ваш email для Garmin Connect:")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors caused by updates."""
    logger.error("Exception while handling an update:", exc_info=context.error)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    sess = ensure_session(chat_id)
    state = sess.get("state")
    logger.info(
        "handle_message chat_id=%s text_category=%s state=%s keys=%s",
        chat_id,
        _message_text_category(update.message.text, state),
        state,
        _session_keys(sess),
    )

    if state == "waiting_email":
        email = update.message.text.strip()
        sess["email"] = email
        sess["state"] = "waiting_password"
        await update.message.reply_text("Теперь введите ваш пароль для Garmin Connect:")
        return

    if state == "waiting_password":
        email = sess.get("email")
        password = update.message.text.strip()
        if not email or not password:
            await update.message.reply_text("\u274c \u0423\u043a\u0430\u0436\u0438\u0442\u0435 email \u0438 \u043f\u0430\u0440\u043e\u043b\u044c.")
            return
        if not garmin_login(sess, email, password, chat_id):
            sess["state"] = "waiting_email"
            await update.message.reply_text("\u274c \u041e\u0448\u0438\u0431\u043a\u0430 \u0430\u0432\u0442\u043e\u0440\u0438\u0437\u0430\u0446\u0438\u0438. \u0412\u0432\u0435\u0434\u0438\u0442\u0435 email \u0437\u0430\u043d\u043e\u0432\u043e:")
            return
        acts = get_last_activities(sess, 10)
        if not acts:
            await update.message.reply_text("\u274c \u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043f\u043e\u043b\u0443\u0447\u0438\u0442\u044c \u0441\u043f\u0438\u0441\u043e\u043a \u0442\u0440\u0435\u043d\u0438\u0440\u043e\u0432\u043e\u043a.")
            return
        sess["last_activities"] = acts
        keyboard = _build_activity_list_keyboard(acts)
        await update.message.reply_text(
            "\u2705 \u0423\u0441\u043f\u0435\u0448\u043d\u0430\u044f \u0430\u0432\u0442\u043e\u0440\u0438\u0437\u0430\u0446\u0438\u044f!\n\n\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0442\u0440\u0435\u043d\u0438\u0440\u043e\u0432\u043a\u0443:", reply_markup=keyboard
        )
        sess.pop("state", None)
        return

    if state == "waiting_pace":
        text = update.message.text.strip()
        spk = parse_pace_to_spk(text)
        if spk is None:
            await update.message.reply_text(
                "\u274c \u041d\u0435\u0432\u0435\u0440\u043d\u044b\u0439 \u0444\u043e\u0440\u043c\u0430\u0442. \u0412\u0432\u0435\u0434\u0438 \u0442\u0435\u043c\u043f \u043a\u0430\u043a \u043c\u0438\u043d:\u0441\u0435\u043a, \u043d\u0430\u043f\u0440\u0438\u043c\u0435\u0440 3:50",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("На главную", callback_data="go_home")]]
                ),
            )
            return
        t100 = spk * 0.1
        t200 = spk * 0.2
        t400 = spk * 0.4
        msg = (
            f"\u041f\u0440\u0438 \u0442\u0435\u043c\u043f\u0435 {text} \u043c\u0438\u043d/\u043a\u043c \u0442\u044b \u0434\u043e\u043b\u0436\u0435\u043d \u043f\u0440\u043e\u0431\u0435\u0436\u0430\u0442\u044c:\n\n"
            f"100\u043c \u0437\u0430 {format_mmss(t100)}\n"
            f"200\u043c \u0437\u0430 {format_mmss(t200)}\n"
            f"400\u043c \u0437\u0430 {format_mmss(t400)}\n\n"
            f"\u0412\u0432\u0435\u0434\u0438 \u043d\u043e\u0432\u044b\u0439 \u0442\u0435\u043c\u043f \u0438\u043b\u0438 \u043d\u0430\u0436\u043c\u0438 \u043a\u043d\u043e\u043f\u043a\u0443 \u043d\u0438\u0436\u0435."
        )
        await update.message.reply_text(
            msg,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("На главную", callback_data="go_home")]]
            ),
        )
        return

    await update.message.reply_text("\u041d\u0430\u0436\u043c\u0438\u0442\u0435 /start \u0438\u043b\u0438 \u043a\u043d\u043e\u043f\u043a\u0443 Garmin Extractor \u0434\u043b\u044f \u043d\u0430\u0447\u0430\u043b\u0430 \u0440\u0430\u0431\u043e\u0442\u044b.")


async def activity_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    sess = ensure_session(chat_id)

    data = query.data
    if data == "go_home":
        sess.pop("state", None)
        await query.edit_message_text(
            "Добро пожаловать в BegovayaKuznitsa_Bot!\n\nВыберите действие:",
            reply_markup=build_main_menu_keyboard(),
        )
        return

    if not sess.get("api"):
        await query.edit_message_text("\u274c \u0421\u043d\u0430\u0447\u0430\u043b\u0430 \u0430\u0432\u0442\u043e\u0440\u0438\u0437\u0443\u0439\u0442\u0435\u0441\u044c \u0447\u0435\u0440\u0435\u0437 \u043a\u043d\u043e\u043f\u043a\u0443 Garmin Extractor.")
        return

    if data == "back_to_list":
        acts = get_last_activities(sess, 10)
        if not acts:
            await query.edit_message_text("\u274c \u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043f\u043e\u043b\u0443\u0447\u0438\u0442\u044c \u0441\u043f\u0438\u0441\u043e\u043a \u0442\u0440\u0435\u043d\u0438\u0440\u043e\u0432\u043e\u043a. \u041f\u043e\u043f\u0440\u043e\u0431\u0443\u0439\u0442\u0435 \u043f\u043e\u0437\u0436\u0435.")
            return
        sess["last_activities"] = acts
        keyboard = _append_keyboard_rows(
            _build_activity_list_keyboard(acts),
            [[InlineKeyboardButton("На главную", callback_data="go_home")]],
        )
        await query.edit_message_text("\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0442\u0440\u0435\u043d\u0438\u0440\u043e\u0432\u043a\u0443:", reply_markup=keyboard)
        return

    if not data.startswith("activity_"):
        return
    activity_id = int(data.split("_", 1)[1])

    await query.edit_message_text("\u23f3 \u041f\u043e\u043b\u0443\u0447\u0430\u044e \u0434\u0430\u043d\u043d\u044b\u0435 \u043f\u043e \u043a\u0440\u0443\u0433\u0430\u043c...")

    try:
        message, nav, _ = _format_activity_for_display(sess, activity_id)
        await query.edit_message_text(
            message, reply_markup=nav, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )
    except Exception as e:
        logger.error("Error fetching laps: %s", e)
        await query.edit_message_text("\u274c \u041e\u0448\u0438\u0431\u043a\u0430 \u043f\u0440\u0438 \u043f\u043e\u043b\u0443\u0447\u0435\u043d\u0438\u0438 \u0434\u0430\u043d\u043d\u044b\u0445. \u041f\u043e\u043f\u0440\u043e\u0431\u0443\u0439\u0442\u0435 \u0435\u0449\u0435 \u0440\u0430\u0437.")


async def compact_format_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    sess = ensure_session(chat_id)

    segments = sess.get("cached_segments")
    header = sess.get("cached_header")
    activity_id = sess.get("cached_activity_id")

    if not segments:
        await query.edit_message_text("\u274c \u0414\u0430\u043d\u043d\u044b\u0435 \u043f\u043e \u0442\u0440\u0435\u043d\u0438\u0440\u043e\u0432\u043a\u0435 \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u044b. \u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0442\u0440\u0435\u043d\u0438\u0440\u043e\u0432\u043a\u0443 \u0437\u0430\u043d\u043e\u0432\u043e.")
        return

    message = build_compact_intervals_message(segments, header)
    nav_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Подробный формат", callback_data=f"full_{activity_id}")],
        [InlineKeyboardButton("\u2b05\ufe0f \u041d\u0430\u0437\u0430\u0434 \u043a \u0441\u043f\u0438\u0441\u043a\u0443", callback_data="back_to_list")],
        [InlineKeyboardButton("На главную", callback_data="go_home")],
    ])
    await query.edit_message_text(
        message, reply_markup=nav_markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def full_format_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    sess = ensure_session(chat_id)

    segments = sess.get("cached_segments")
    header = sess.get("cached_header")
    activity_id = sess.get("cached_activity_id")

    if not segments:
        await query.edit_message_text("\u274c \u0414\u0430\u043d\u043d\u044b\u0435 \u043f\u043e \u0442\u0440\u0435\u043d\u0438\u0440\u043e\u0432\u043a\u0435 \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u044b. \u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0442\u0440\u0435\u043d\u0438\u0440\u043e\u0432\u043a\u0443 \u0437\u0430\u043d\u043e\u0432\u043e.")
        return

    message = build_laps_message(segments, header)
    nav_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Для дневника", callback_data=f"compact_{activity_id}")],
        [InlineKeyboardButton("\u2b05\ufe0f \u041d\u0430\u0437\u0430\u0434 \u043a \u0441\u043f\u0438\u0441\u043a\u0443", callback_data="back_to_list")],
        [InlineKeyboardButton("На главную", callback_data="go_home")],
    ])
    await query.edit_message_text(
        message, reply_markup=nav_markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def sublaps_format_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    sess = ensure_session(chat_id)

    segments_sublaps = sess.get("cached_segments_sublaps")
    header = sess.get("cached_header")
    activity_id = sess.get("cached_activity_id")

    if not segments_sublaps:
        await query.edit_message_text("\u274c \u0414\u0430\u043d\u043d\u044b\u0435 \u043f\u043e \u0442\u0440\u0435\u043d\u0438\u0440\u043e\u0432\u043a\u0435 \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u044b. \u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0442\u0440\u0435\u043d\u0438\u0440\u043e\u0432\u043a\u0443 \u0437\u0430\u043d\u043e\u0432\u043e.")
        return

    message = build_detailed_intervals_message(segments_sublaps, header)
    nav_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Подробный формат", callback_data=f"full_{activity_id}")],
        [InlineKeyboardButton("Для дневника", callback_data=f"compact_{activity_id}")],
        [InlineKeyboardButton("\u2b05\ufe0f \u041d\u0430\u0437\u0430\u0434 \u043a \u0441\u043f\u0438\u0441\u043a\u0443", callback_data="back_to_list")],
        [InlineKeyboardButton("На главную", callback_data="go_home")],
    ])
    await query.edit_message_text(
        message, reply_markup=nav_markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def pace_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    sess = ensure_session(chat_id)
    sess["state"] = "waiting_pace"
    await query.edit_message_text(
        "\u0412\u0432\u0435\u0434\u0438 \u0442\u0435\u043c\u043f \u0432 \u0444\u043e\u0440\u043c\u0430\u0442\u0435 \u043c\u0438\u043d:\u0441\u0435\u043a (\u043d\u0430\u043f\u0440\u0438\u043c\u0435\u0440: 3:50)",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("На главную", callback_data="go_home")]]
        ),
    )


def parse_pace_to_spk(pace_text: str) -> Optional[float]:
    try:
        text = pace_text.strip().replace(" ", "")
        if ":" not in text:
            return None
        mins, secs = text.split(":", 1)
        m = int(mins)
        s = int(secs)
        if m < 0 or s < 0 or s >= 60:
            return None
        return m * 60 + s
    except Exception:
        return None


def format_mmss(seconds: float) -> str:
    total = int(round(seconds))
    m = total // 60
    s = total % 60
    return f"{m}:{s:02d}"


# --------------- Health check for Render ---------------

def start_health_server_if_needed() -> bool:
    """Start a minimal HTTP health server when PORT env var is set (e.g. on Render).

    Returns True if the server was started, False otherwise.
    Render Web Services require the process to bind to $PORT; without it the
    deploy times out.  The server responds 200 OK on ``/`` and ``/health``.
    """
    port_str = os.environ.get("PORT")
    if not port_str:
        logger.info("PORT not set — skipping health server (local mode)")
        return False

    import threading
    from http.server import HTTPServer, BaseHTTPRequestHandler

    try:
        port = int(port_str)
    except ValueError:
        logger.error("PORT env var is not a valid integer: %s", port_str)
        return False

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/health"):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, fmt, *args):
            pass

    try:
        server = HTTPServer(("0.0.0.0", port), _Handler)
    except OSError as exc:
        logger.error("Failed to bind health server on port %s: %s", port, exc)
        return False

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    logger.info("Health-check server listening on 0.0.0.0:%s", port)
    return True


# --------------- Application entry point ---------------

def main() -> None:
    start_health_server_if_needed()

    if not BOT_TOKEN:
        logger.critical("TELEGRAM_BOT_TOKEN environment variable not set — exiting")
        sys.exit(1)

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("last", last_command))
    application.add_handler(CommandHandler("format", format_command))
    application.add_handler(CommandHandler("logout", logout_command))
    application.add_handler(CallbackQueryHandler(button_handler, pattern=r"^garmin_extractor$"))
    application.add_handler(CallbackQueryHandler(pace_button_handler, pattern=r"^pace_calculator$"))
    application.add_handler(CallbackQueryHandler(compact_format_handler, pattern=r"^compact_\d+$"))
    application.add_handler(CallbackQueryHandler(full_format_handler, pattern=r"^full_\d+$"))
    application.add_handler(CallbackQueryHandler(sublaps_format_handler, pattern=r"^sublaps_\d+$"))
    application.add_handler(CallbackQueryHandler(activity_handler, pattern=r"^(activity_\d+|back_to_list|go_home)$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)

    logger.info("Starting polling mode (drop_pending_updates=True)")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        logger.critical("Unhandled exception in main — exiting", exc_info=True)
        sys.exit(1)
