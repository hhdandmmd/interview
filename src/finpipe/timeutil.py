"""统一UTC格式与自然年季度，不依赖运行主机时区。"""

from datetime import date, datetime, timedelta, timezone

UTC = timezone.utc
SHANGHAI = timezone(timedelta(hours=8))


def utc_now() -> str:
    """返回实际时刻，固定六位微秒供SQLite排序。"""
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    """日期参数表示上海当日结束；精确时间必须带时区。"""
    if len(value) == 10:
        day = date.fromisoformat(value)
        return datetime.combine(day + timedelta(days=1), datetime.min.time(), SHANGHAI).astimezone(
            UTC
        ) - timedelta(microseconds=1)
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("精确时间必须包含时区")
    return result.astimezone(UTC)


def iso_time(value: str) -> str:
    """规范时点字符串，避免混合精度及偏移影响比较。"""
    return parse_time(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def quarter_end(value: str) -> str:
    """接受季度简写或准确季末日期，拒绝任意月末。"""
    if len(value) == 6 and value[4].upper() == "Q":
        year, quarter = int(value[:4]), int(value[-1])
        if quarter not in (1, 2, 3, 4):
            raise ValueError("季度须在1至4之间")
        return f"{year:04d}-" + ("03-31", "06-30", "09-30", "12-31")[quarter - 1]
    day = date.fromisoformat(value)
    if (day.month, day.day) not in ((3, 31), (6, 30), (9, 30), (12, 31)):
        raise ValueError("报告期必须为自然年季度末")
    return day.isoformat()


def quarters(start: str, end: str) -> tuple[str, ...]:
    """生成连续准确季度，先验证范围再建立期望集合。"""
    start, end = quarter_end(start), quarter_end(end)
    if start > end:
        raise ValueError("起始季度晚于终止季度")
    result = []
    year = int(start[:4])
    while year <= int(end[:4]):
        for suffix in ("03-31", "06-30", "09-30", "12-31"):
            value = f"{year:04d}-{suffix}"
            if start <= value <= end:
                result.append(value)
        year += 1
    return tuple(result)
