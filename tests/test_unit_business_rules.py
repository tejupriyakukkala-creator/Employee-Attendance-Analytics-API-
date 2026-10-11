from datetime import datetime, timezone, timedelta
import pytest
from app.main import (
    IST,
    round_half_up,
    to_epoch_ms,
    from_epoch_ms,
    truncate_datetime,
    get_attendance_date,
    compute_late_minutes,
    compute_work_hours,
    compute_overtime,
    compute_half_day,
    get_month_working_days,
    serialize_doc_record,
    serialize_employee
)


def test_r1_attendance_date_normal_shift():
    # Normal shift 09:30 - 18:30
    # Punch in 2026-07-06 09:28:00 IST -> 2026-07-06 03:58:00 UTC
    dt = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    att_date = get_attendance_date(dt, "09:30", "18:30")
    assert att_date == "2026-07-06"


def test_r1_attendance_date_overnight_shift():
    # Overnight shift 22:00 - 06:00
    # Punch in at 21:55 IST on 2026-07-06
    dt_evening = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)
    att_date_evening = get_attendance_date(dt_evening, "22:00", "06:00")
    assert att_date_evening == "2026-07-06"

    # Punch in at 01:15 IST on 2026-07-07 (earlier than shift_end 06:00)
    # Per R1: belongs to the shift that started the previous day (2026-07-06)
    dt_early_morning = datetime(2026, 7, 6, 19, 45, 0, tzinfo=timezone.utc)
    att_date_morning = get_attendance_date(dt_early_morning, "22:00", "06:00")
    assert att_date_morning == "2026-07-06"

    # Punch in at 06:00:00 IST on 2026-07-07 (not strictly earlier than shift_end)
    dt_after_shift = datetime(2026, 7, 7, 0, 30, 0, tzinfo=timezone.utc)
    att_date_next = get_attendance_date(dt_after_shift, "22:00", "06:00")
    assert att_date_next == "2026-07-07"


def test_r1_instant_truncation_to_whole_seconds():
    # Instants are truncated to whole seconds
    dt_with_micros = datetime(2026, 7, 6, 3, 58, 0, 950000, tzinfo=timezone.utc)
    truncated = truncate_datetime(dt_with_micros)
    assert truncated.microsecond == 0
    assert truncated.second == 0

    ms = 1783312500999
    dt_from_ms = from_epoch_ms(ms)
    assert dt_from_ms.microsecond == 0
    assert to_epoch_ms(dt_from_ms) == 1783312500000


def test_r2_late_minutes_boundaries():
    # Shift 09:30:
    # 09:40:00 (exactly 10 min) -> 0
    dt_10m = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)  # 09:40:00 IST
    assert compute_late_minutes(dt_10m, "09:30", "2026-07-06") == 0

    # 09:40:01 (> 10 min) -> 10
    dt_10m_1s = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)  # 09:40:01 IST
    assert compute_late_minutes(dt_10m_1s, "09:30", "2026-07-06") == 10

    # 10:15:59 -> 45
    dt_45m_59s = datetime(2026, 7, 6, 4, 45, 59, tzinfo=timezone.utc)  # 10:15:59 IST
    assert compute_late_minutes(dt_45m_59s, "09:30", "2026-07-06") == 45

    # Early punch-in (09:15:00) -> 0
    dt_early = datetime(2026, 7, 6, 3, 45, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_early, "09:30", "2026-07-06") == 0


def test_r3_overtime_minutes():
    # Shift end 18:30
    # Punch out 18:35 (5 min overtime, < 30) -> 0
    dt_out_5m = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_out_5m, "09:30", "18:30", "2026-07-06") == 0

    # Punch out 18:59:59 (29 min 59 sec < 30 min) -> 0
    dt_out_29m = datetime(2026, 7, 6, 13, 29, 59, tzinfo=timezone.utc)
    assert compute_overtime(dt_out_29m, "09:30", "18:30", "2026-07-06") == 0

    # Punch out 19:00:00 (exactly 30 min) -> 30
    dt_out_30m = datetime(2026, 7, 6, 13, 30, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_out_30m, "09:30", "18:30", "2026-07-06") == 30

    # Punch out 19:10:00 (40 min) -> 40
    dt_out_40m = datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_out_40m, "09:30", "18:30", "2026-07-06") == 40


def test_r3_overtime_overnight_shift():
    # Shift 22:00 - 06:00 on attendance date 2026-07-06
    # Shift ends at 2026-07-07 06:00:00 IST (00:30:00 UTC)
    # Punch out at 06:40:00 IST on 2026-07-07 (01:10:00 UTC) -> 40 min overtime
    dt_out = datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_out, "22:00", "06:00", "2026-07-06") == 40


def test_r4_work_hours_half_up_rounding():
    # Sample 1: 09:28 to 18:35 -> 32820 sec / 3600 = 9.11666... -> 9.12
    t_in = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    t_out = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    assert compute_work_hours(t_in, t_out) == 9.12

    # Verify half-up rounding (2.505 -> 2.51, unlike Python round which yields 2.5)
    t_in2 = datetime(2026, 7, 6, 0, 0, 0, tzinfo=timezone.utc)
    # 2.505 hours = 2.505 * 3600 = 9018 seconds
    t_out2 = t_in2 + timedelta(seconds=9018)
    assert compute_work_hours(t_in2, t_out2) == 2.51


def test_r5_half_day():
    assert compute_half_day(3.50) is True
    assert compute_half_day(4.49) is True
    assert compute_half_day(4.50) is False
    assert compute_half_day(9.12) is False
    assert compute_half_day(None) is False


def test_r7_working_days_calculation():
    # July 2026: starts Wed Jul 1, ends Fri Jul 31 -> 23 weekdays
    assert get_month_working_days("2026-01-05", "2026-07") == 23

    # Mid-month joiner: joined 2026-07-13 (Monday) -> 15 weekdays
    assert get_month_working_days("2026-07-13", "2026-07") == 15

    # Joined in future month -> 0 working days
    assert get_month_working_days("2026-08-01", "2026-07") == 0


def test_r8_rounding_helper():
    assert round_half_up(9.1166, 2) == 9.12
    assert round_half_up(0.00005, 4) == 0.0001
    assert round_half_up(None, 2) is None
