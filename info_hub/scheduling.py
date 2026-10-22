from datetime import timedelta


def latest_slots(now, hours, weekly_dow='sun', weekly_hour=20):
    """Timezone-aware calendar slots, independent of the Windows host timezone."""
    day = now.replace(hour=0,minute=0,second=0,microsecond=0)
    candidates = [day + timedelta(days=d,hours=h) for d in (-1,0) for h in hours]
    daily = max(t for t in candidates if t<=now)
    dow = ['mon','tue','wed','thu','fri','sat','sun'].index(weekly_dow)
    weekly = day - timedelta(days=(now.weekday()-dow)%7) + timedelta(hours=weekly_hour)
    if weekly > now:
        weekly -= timedelta(days=7)
    return daily,weekly
