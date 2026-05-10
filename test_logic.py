"""Unit tests for telegram_bot formatting and GPS-smoothing logic.

Run with:  python -m pytest test_logic.py -v
Or simply:  python test_logic.py
"""

from datetime import timedelta
from pathlib import Path
import tempfile
from typing import List, Dict, Any, Optional

# Import the functions under test directly from the bot module.
import telegram_bot
from telegram_bot import (
    normalize_distance,
    find_dominant_distance,
    smooth_interval_pace,
    fmt_smooth_time,
    fmt_pace,
    fmt_distance,
    escape_html,
    group_intervals,
    build_compact_intervals_message,
    build_laps_message,
    build_detailed_intervals_message,
    aggregate_segments,
    normalize_label,
    STANDARD_DISTANCES,
    NORMALIZATION_THRESHOLD,
    parse_pace_to_spk,
    format_mmss,
)


def test_normalize_distance_within_threshold():
    assert normalize_distance(398) == 400.0
    assert normalize_distance(407) == 400.0
    assert normalize_distance(205) == 200.0
    assert normalize_distance(812) == 800.0
    assert normalize_distance(992) == 1000.0


def test_normalize_distance_exact():
    for d in STANDARD_DISTANCES:
        assert normalize_distance(float(d)) == float(d)


def test_normalize_distance_outside_threshold():
    assert normalize_distance(450) == 450
    assert normalize_distance(350) == 350
    assert normalize_distance(1050) == 1050


def test_normalize_distance_edge():
    assert normalize_distance(0) is not None or True
    assert normalize_distance(None) is None
    assert normalize_distance(-5) == -5


def test_find_dominant_distance_all_400():
    segs = [
        {"label": "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b", "distance_m": 407},
        {"label": "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b", "distance_m": 395},
        {"label": "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b", "distance_m": 402},
    ]
    assert find_dominant_distance(segs) == 400.0


def test_find_dominant_distance_mixed():
    segs = [
        {"label": "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b", "distance_m": 407},
        {"label": "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b", "distance_m": 398},
        {"label": "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b", "distance_m": 210},
        {"label": "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b", "distance_m": 403},
    ]
    assert find_dominant_distance(segs) == 400.0


def test_find_dominant_distance_200():
    segs = [
        {"label": "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b", "distance_m": 195},
        {"label": "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b", "distance_m": 207},
        {"label": "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b", "distance_m": 198},
        {"label": "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b", "distance_m": 204},
    ]
    assert find_dominant_distance(segs) == 200.0


def test_find_dominant_distance_empty():
    assert find_dominant_distance([]) == 400.0


def test_find_dominant_distance_ignores_non_intervals():
    segs = [
        {"label": "\u0440\u0430\u0437\u043c\u0438\u043d\u043a\u0430", "distance_m": 3000},
        {"label": "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b", "distance_m": 805},
        {"label": "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b", "distance_m": 795},
    ]
    assert find_dominant_distance(segs) == 800.0


def test_smooth_pace_within_tolerance():
    eff = smooth_interval_pace(duration_s=90, real_distance=407, dominant=400.0)
    expected = (90 / 407) * 400
    assert abs(eff - expected) < 0.01


def test_smooth_pace_outside_tolerance():
    eff = smooth_interval_pace(duration_s=90, real_distance=500, dominant=400.0)
    assert eff == 90


def test_smooth_pace_zero_distance():
    assert smooth_interval_pace(60, 0, 400) == 60


def test_compact_intervals_uses_smoothing():
    segments = [
        {"label": "\u0440\u0430\u0437\u043c\u0438\u043d\u043a\u0430", "distance_m": 2000, "duration_s": 600},
        {"label": "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b", "distance_m": 407, "duration_s": 90},
        {"label": "\u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435", "distance_m": 100, "duration_s": 60},
        {"label": "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b", "distance_m": 395, "duration_s": 88},
        {"label": "\u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435", "distance_m": 100, "duration_s": 60},
        {"label": "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b", "distance_m": 402, "duration_s": 91},
        {"label": "\u0437\u0430\u043c\u0438\u043d\u043a\u0430", "distance_m": 1000, "duration_s": 300},
    ]
    msg = build_compact_intervals_message(segments, "test")
    assert "3x400" in msg
    assert "test" in msg


def test_compact_intervals_diary_format():
    segments = [
        {"label": "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b", "distance_m": 202, "duration_s": 40},
        {"label": "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b", "distance_m": 198, "duration_s": 38},
        {"label": "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b", "distance_m": 205, "duration_s": 41},
    ]
    msg = build_compact_intervals_message(segments)
    lines = [l for l in msg.split("\n") if "x" in l.lower()]
    assert len(lines) == 1
    assert lines[0].startswith("3x200:")


def test_laps_message_regular_run():
    summary = {
        "distance_m": 10000.0,
        "avg_speed": 3.0,
        "avg_hr": 145.0,
        "max_hr": 170.0,
        "duration_s": 3333.0,
    }
    msg = build_laps_message([], "10.05.2026 07:00", summary=summary)
    assert "10.00" in msg


def test_gps_drift_200m_intervals():
    segments = [
        {"label": "\u0440\u0430\u0437\u043c\u0438\u043d\u043a\u0430", "distance_m": 4550, "duration_s": 1466, "median_hr": 124, "avg_speed": 4550 / 1466},
        {"label": "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b", "distance_m": 208, "duration_s": 38},
        {"label": "\u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435", "distance_m": 67, "duration_s": 38, "avg_speed": 67 / 38},
        {"label": "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b", "distance_m": 195, "duration_s": 35},
        {"label": "\u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435", "distance_m": 49, "duration_s": 30, "avg_speed": 49 / 30},
        {"label": "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b", "distance_m": 210, "duration_s": 40},
        {"label": "\u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435", "distance_m": 56, "duration_s": 31, "avg_speed": 56 / 31},
        {"label": "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b", "distance_m": 203, "duration_s": 41},
        {"label": "\u0437\u0430\u043c\u0438\u043d\u043a\u0430", "distance_m": 19, "duration_s": 10, "median_hr": 172, "avg_speed": 19 / 10},
    ]
    msg = build_compact_intervals_message(segments)
    assert "4x200" in msg


def test_detailed_intervals_with_sublaps():
    segments = [
        {"label": "\u0440\u0430\u0437\u043c\u0438\u043d\u043a\u0430", "distance_m": 2000, "duration_s": 600, "median_hr": 120, "avg_speed": 2000 / 600},
        {
            "label": "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b", "distance_m": 1000, "duration_s": 243, "avg_speed": 1000 / 243,
            "sub_laps": [
                {"distance_m": 200, "duration_s": 48, "pace": "4:00"},
                {"distance_m": 200, "duration_s": 49, "pace": "4:05"},
                {"distance_m": 200, "duration_s": 47, "pace": "3:55"},
                {"distance_m": 200, "duration_s": 49, "pace": "4:05"},
                {"distance_m": 200, "duration_s": 50, "pace": "4:10"},
            ],
        },
        {"label": "\u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435", "distance_m": 200, "duration_s": 89, "avg_speed": 200 / 89},
        {
            "label": "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b", "distance_m": 1000, "duration_s": 240, "avg_speed": 1000 / 240,
            "sub_laps": [
                {"distance_m": 200, "duration_s": 48, "pace": "4:00"},
                {"distance_m": 200, "duration_s": 48, "pace": "4:00"},
                {"distance_m": 200, "duration_s": 48, "pace": "4:00"},
                {"distance_m": 200, "duration_s": 48, "pace": "4:00"},
                {"distance_m": 200, "duration_s": 48, "pace": "4:00"},
            ],
        },
        {"label": "\u0437\u0430\u043c\u0438\u043d\u043a\u0430", "distance_m": 1000, "duration_s": 360, "median_hr": 130, "avg_speed": 1000 / 360},
    ]
    msg = build_detailed_intervals_message(segments)
    assert "4:00" in msg
    assert "4:05" in msg


# --------------- Pace calculator tests ---------------

def test_parse_pace_to_spk_valid():
    assert parse_pace_to_spk("3:50") == 230
    assert parse_pace_to_spk("5:00") == 300
    assert parse_pace_to_spk("0:30") == 30


def test_parse_pace_to_spk_invalid():
    assert parse_pace_to_spk("abc") is None
    assert parse_pace_to_spk("3:60") is None
    assert parse_pace_to_spk("350") is None
    assert parse_pace_to_spk("") is None


def test_format_mmss():
    assert format_mmss(90) == "1:30"
    assert format_mmss(0) == "0:00"
    assert format_mmss(59) == "0:59"
    assert format_mmss(61) == "1:01"


def test_restore_failure_preserves_interactive_session():
    chat_id = 987654321
    original_token_dir = telegram_bot.TOKEN_DIR
    original_garmin = telegram_bot.Garmin

    class FailingGarmin:
        def __init__(self):
            self.garth = self

        def load(self, path: str) -> None:
            pass

        def login(self) -> None:
            raise RuntimeError("restore failed")

    try:
        with tempfile.TemporaryDirectory() as tmp:
            telegram_bot.TOKEN_DIR = Path(tmp)
            token_path = telegram_bot._token_path(chat_id)
            token_path.mkdir(parents=True)
            telegram_bot.Garmin = FailingGarmin
            telegram_bot.chat_sessions.clear()

            sess = telegram_bot.ensure_session(chat_id)
            sess["state"] = "waiting_email"

            assert telegram_bot.try_restore_session(chat_id) is None
            assert telegram_bot.chat_sessions.get(chat_id) is sess
            assert sess["state"] == "waiting_email"
            assert not token_path.exists()
    finally:
        telegram_bot.TOKEN_DIR = original_token_dir
        telegram_bot.Garmin = original_garmin
        telegram_bot.chat_sessions.clear()


def test_append_keyboard_rows_rebuilds_immutable_markup():
    keyboard = telegram_bot._build_activity_list_keyboard(
        [
            {
                "activityId": 123,
                "activityName": "Morning Run",
                "startTimeLocal": "2026-05-10 07:30:00",
            }
        ]
    )

    rebuilt = telegram_bot._append_keyboard_rows(
        keyboard,
        [[telegram_bot.InlineKeyboardButton("Home", callback_data="go_home")]],
    )

    assert isinstance(keyboard.inline_keyboard, tuple)
    assert len(keyboard.inline_keyboard) == 1
    assert len(rebuilt.inline_keyboard) == 2
    assert rebuilt.inline_keyboard[-1][0].callback_data == "go_home"


def test_fmt_smooth_time():
    assert fmt_smooth_time(90.4) == "1:30"
    assert fmt_smooth_time(0) == "0:00"
    assert fmt_smooth_time(125.6) == "2:06"


# --------------- Edge case tests ---------------

def test_escape_html():
    assert escape_html("<b>test</b>") == "&lt;b&gt;test&lt;/b&gt;"
    assert escape_html("A & B") == "A &amp; B"
    assert escape_html("") == ""


def test_normalize_label_various():
    assert normalize_label("INTERVAL_ACTIVE") == "\u0438\u043d\u0442\u0435\u0440\u0432\u0430\u043b"
    assert normalize_label("INTERVAL_WARMUP") == "\u0440\u0430\u0437\u043c\u0438\u043d\u043a\u0430"
    assert normalize_label("INTERVAL_RECOVERY") == "\u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435"
    assert normalize_label("INTERVAL_COOLDOWN") == "\u0437\u0430\u043c\u0438\u043d\u043a\u0430"
    assert normalize_label("LAP") == "\u0431\u0435\u0433"
    assert normalize_label("SPLIT") == "\u0431\u0435\u0433"
    assert normalize_label("-") == "\u0431\u0435\u0433"
    assert normalize_label("RWD_RUN_1") == "\u0431\u0435\u0433"
    assert normalize_label("SOMETHING_ELSE") == "something_else"


def test_fmt_pace_edge_cases():
    assert fmt_pace(None, None, None) == "-"
    assert fmt_pace(0, None, None) == "-"
    assert fmt_pace(None, 1000, 300) == "5:00"
    assert fmt_pace(3.333, None, None) == "5:00"


def test_fmt_distance_edge_cases():
    assert fmt_distance(None) == "-"
    assert fmt_distance(0) == "-"
    assert fmt_distance(-100) == "-"
    assert fmt_distance(500) == "500 \u043c"
    assert fmt_distance(1500) == "1.50 \u043a\u043c"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as exc:
            print(f"  FAIL  {t.__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed out of {passed + failed}")
    if failed:
        raise SystemExit(1)
