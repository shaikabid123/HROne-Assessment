"""A representative ~100,000-record dataset, as the assessment's grader uses: many employees in several departments,
about 100 days of logs with weekends, every status, half days, late minutes, overtime, work hours, punch times, and
seeded-style gaps (optional fields missing on some records). Deterministic (seeded)."""
import random
from datetime import date, datetime, timedelta, timezone

DEPARTMENTS = ["Engineering", "Sales", "HR", "Finance", "Support", "Marketing", "Ops", "Legal", "Design", "Data"]
FIRST_DAY = date(2026, 4, 1)
DAYS = 100                      # 2026-04-01 .. 2026-07-09
STATUSES = ["PRESENT"] * 14 + ["WFH"] * 3 + ["ON_DUTY"] * 2 + ["ABSENT", "LEAVE"]
IST = timezone(timedelta(hours=5, minutes=30))


def populate(db, employees=1450, seed=20260712, weekend_rate=0.15):
    """Insert employees and logs into `db`; return (employee_count, record_count)."""
    rng = random.Random(seed)
    emp_docs, log_docs = [], []
    for e in range(employees):
        code = f"EMP{e:05d}"
        joined = FIRST_DAY + timedelta(days=rng.randint(-300, 90))              # some join inside the logged period
        emp_docs.append({"emp_code": code, "name": f"Employee {e}", "email": f"{code.lower()}@example.com",
                         "department": DEPARTMENTS[e % len(DEPARTMENTS)], "shift_start": "09:30", "shift_end": "18:30",
                         "joined_on": joined.isoformat(), "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)})
        for k in range(DAYS):
            day = FIRST_DAY + timedelta(days=k)
            if day < joined or (day.weekday() >= 5 and rng.random() > weekend_rate):
                continue
            status = rng.choice(STATUSES)
            doc = {"emp_code": code, "date": day.isoformat(), "status": status}
            if status in ("PRESENT", "WFH", "ON_DUTY"):
                start = datetime(day.year, day.month, day.day, 9, 30, tzinfo=IST) + timedelta(minutes=rng.choice([-20, -5, 0, 5, 12, 40, 90]))
                end = start + timedelta(minutes=rng.choice([240, 480, 540, 570, 600]))
                late = max(0, (start - datetime(day.year, day.month, day.day, 9, 30, tzinfo=IST)).seconds // 60) if start.hour * 60 + start.minute > 9 * 60 + 40 else 0
                doc.update({"punch_in": start.astimezone(timezone.utc), "punch_out": end.astimezone(timezone.utc),
                            "work_hours": round((end - start).total_seconds() / 3600, 2), "late_minutes": late,
                            "overtime_minutes": rng.choice([0, 0, 0, 30, 45]), "half_day": rng.random() < 0.05})
                if rng.random() < 0.3:                                         # seeded-style: some optional fields are simply absent
                    for field in rng.sample(["half_day", "overtime_minutes", "late_minutes"], k=rng.randint(1, 2)):
                        doc.pop(field, None)
            log_docs.append(doc)
    db.employees.insert_many(emp_docs)
    for i in range(0, len(log_docs), 20000):
        db.attendance_logs.insert_many(log_docs[i:i + 20000])
    return len(emp_docs), len(log_docs)
