from __future__ import annotations

from datetime import datetime, timezone, timedelta
from io import BytesIO
from typing import Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter


MSK_TZ = timezone(timedelta(hours=3))


def _parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(MSK_TZ)
    except Exception:
        return None


def build_price_history_chart(product_name: str, history_rows: Iterable) -> BytesIO | None:
    points = []
    for row in history_rows:
        checked_at = _parse_dt(row["checked_at"])
        price = row["price"]
        if checked_at is None or price is None:
            continue
        points.append((checked_at, float(price)))

    if not points:
        return None

    dates = [item[0] for item in points]
    prices = [item[1] for item in points]
    date_span = dates[-1] - dates[0] if len(dates) > 1 else timedelta(0)

    fig, ax = plt.subplots(figsize=(10, 5), dpi=140)
    markevery = max(1, len(points) // 35)
    ax.plot(
        dates,
        prices,
        color="#005BFF",
        linewidth=1.8,
        marker="o",
        markersize=2.2,
        markevery=markevery,
    )
    ax.fill_between(dates, prices, min(prices), color="#005BFF", alpha=0.10)

    title = product_name.strip() if product_name else "История цены"
    if len(title) > 80:
        title = title[:77] + "..."

    ax.set_title(title, fontsize=12, pad=12)
    ax.set_ylabel("Цена, ₽")
    ax.grid(True, linestyle="--", alpha=0.3)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    if len(points) > 1:
        if date_span.days >= 10:
            locator = mdates.DayLocator(interval=2)
            formatter = mdates.DateFormatter("%d.%m", tz=MSK_TZ)
        elif date_span.days >= 4:
            locator = mdates.DayLocator(interval=1)
            formatter = mdates.DateFormatter("%d.%m", tz=MSK_TZ)
        elif date_span.days >= 1:
            locator = mdates.HourLocator(interval=12)
            formatter = mdates.DateFormatter("%d.%m\n%H:%M", tz=MSK_TZ)
        else:
            locator = mdates.HourLocator(interval=4)
            formatter = mdates.DateFormatter("%d.%m\n%H:%M", tz=MSK_TZ)
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(formatter)
    else:
        ax.set_xticks(dates)
        ax.set_xticklabels([dates[0].strftime("%d.%m\n%H:%M")])

    ax.tick_params(axis="x", labelsize=9)

    ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:,.0f}".replace(",", " ")))

    fig.tight_layout()

    buffer = BytesIO()
    fig.savefig(buffer, format="png", bbox_inches="tight")
    plt.close(fig)
    buffer.seek(0)
    buffer.name = "price_history.png"
    return buffer
