from datetime import datetime, timezone, timedelta
from app.main import late_minutes, calc_derived, attendance_day

def test_late_grace_boundary():
    start = datetime(2026, 1, 5, 4, 0, tzinfo=timezone.utc) # 09:30 IST
    assert late_minutes(start + timedelta(minutes=10), "09:30") == 0
    assert late_minutes(start + timedelta(minutes=10, seconds=1), "09:30") == 10

def test_half_up_hours_and_half_day():
    emp = {"shift_start":"09:30", "shift_end":"18:30"}
    pin = datetime(2026,1,5,4,0,tzinfo=timezone.utc)
    pout = pin + timedelta(hours=4, minutes=29, seconds=59)
    d = calc_derived(pin,pout,emp,"PRESENT")
    assert d["work_hours"] == 4.5
    assert d["half_day"] is False

def test_overnight_attendance_date():
    emp = {"shift_start":"22:00", "shift_end":"06:00"}
    ts = datetime(2026,1,6,1,0,tzinfo=timezone.utc) # 06:30 IST
    assert attendance_day(ts,emp) == "2026-01-05"
