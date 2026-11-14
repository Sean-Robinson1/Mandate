"""Geometry for the stock charts, drawn as inline SVG by the templates (no chart library needed)."""
from __future__ import annotations

import json
import math
from datetime import date

from app.kitchen import HISTORY_DAYS, HORIZON, Incoming, Row, date_of


def nice_ticks(top: float, count: int = 4) -> list[float]:
    """Round axis values from 0 that cover `top`: 0, 250, 500, ..."""
    if top <= 0:
        return [0, 1]
    raw = top / count
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    return [round(i * step, 6) for i in range(int(math.ceil(top / step)) + 1)]


def _label(value: float) -> str:
    return f"{value:,.0f}" if value >= 100 or value == int(value) else f"{value:,.1f}"


def sparkline(row: Row, w: int = 112, h: int = 30) -> dict:
    """The last four weeks of stock, with the reorder level as a hairline."""
    values = [l.on_hand for l in row.history]
    if len(values) < 2:
        return {}
    top = max(max(values), row.supply.reorder_below) * 1.1 or 1
    pad = 3
    xs = [pad + i * (w - 2 * pad) / (len(values) - 1) for i in range(len(values))]
    y = lambda v: h - pad - v / top * (h - 2 * pad)    # noqa: E731
    pts = [(round(x, 1), round(y(v), 1)) for x, v in zip(xs, values)]
    return {"w": w, "h": h, "points": " ".join(f"{x},{yy}" for x, yy in pts), "last": pts[-1],
            "reorder": round(y(row.supply.reorder_below), 1)}


def stock_chart(row: Row, today: int, start: date, incoming: list[Incoming]) -> dict:
    """Four weeks of stock, three weeks of forecast, the levels the kitchen set, and every delivery in between."""
    W, H, L, R, T, B = 760, 260, 48, 92, 14, 30
    first, last = today - HISTORY_DAYS + 1, today + HORIZON
    s, unit = row.supply, row.unit
    hist = [(l.day, l.on_hand) for l in row.history if l.day >= first]
    fc = [(d, v) for d, v in row.forecast if d <= last]
    top = max([s.order_up_to, s.reorder_below] + [v for _, v in hist + fc]) * 1.05
    ticks = nice_ticks(top)
    top = ticks[-1]
    x = lambda d: L + (d - first) / (last - first) * (W - L - R)    # noqa: E731
    y = lambda v: T + (1 - v / top) * (H - T - B)                   # noqa: E731
    base = y(0)

    def path(points):
        return " ".join(("M" if i == 0 else "L") + f"{x(d):.1f},{y(v):.1f}" for i, (d, v) in enumerate(points))

    line = path(hist)
    area = f"{line} L{x(hist[-1][0]):.1f},{base:.1f} L{x(hist[0][0]):.1f},{base:.1f} Z" if hist else ""
    xticks = [{"x": round(x(d), 1), "label": f"{date_of(start, d):%d %b}".lstrip("0")}
              for d in range(first, last + 1) if date_of(start, d).weekday() == 0]
    marks = [{"x": round(x(i.day), 1), "expected": i.status == "EXPECTED",
              "title": f"{'Due' if i.status == 'EXPECTED' else 'Arrived'} {date_of(start, i.day):%a %d %b}: "
                       f"{i.quantity:,g} {unit} from {i.supplier}" + (" (ordered by the agent)" if i.via == "agent" else "")}
             for i in incoming if first <= i.day <= last]
    hover = [{"x": round(x(d), 1), "y": round(y(v), 1),
              "t": f"{date_of(start, d):%a %d %b}", "v": f"{_label(v)} {unit}" + (" forecast" if d > today else "")}
             for d, v in hist + fc[1:]]
    out_x = round(x(row.runs_out_day), 1) if row.runs_out_day and row.runs_out_day <= last else None
    return {
        "w": W, "h": H, "left": L, "right": W - R, "top": T, "base": round(base, 1),
        "line": line, "area": area, "forecast": path(fc),
        "yticks": [{"y": round(y(t), 1), "label": _label(t)} for t in ticks],
        "xticks": xticks, "today": round(x(today), 1),
        "reorder": {"y": round(y(s.reorder_below), 1), "label": f"Reorder {_label(s.reorder_below)}"},
        "up_to": {"y": round(y(s.order_up_to), 1), "label": f"Up to {_label(s.order_up_to)}"},
        "marks": marks, "runs_out": out_x, "hover": json.dumps(hover),
    }


def use_chart(row: Row, start: date) -> dict:
    """Daily use over four weeks, as columns: shows the kitchen's week."""
    W, H, L, R, T, B = 760, 150, 48, 92, 10, 26
    days = [(l.day, l.used) for l in row.history]
    if not days:
        return {}
    ticks = nice_ticks(max(v for _, v in days) * 1.05 or 1, 3)
    top = ticks[-1]
    slot = (W - L - R) / len(days)
    bw = min(24.0, slot * 0.62)
    y = lambda v: T + (1 - v / top) * (H - T - B)    # noqa: E731
    base = y(0)
    bars = []
    for i, (d, v) in enumerate(days):
        bx, by = L + i * slot + (slot - bw) / 2, y(v)
        r = min(4.0, bw / 2, base - by)
        shape = (f"M{bx:.1f},{base:.1f} V{by + r:.1f} Q{bx:.1f},{by:.1f} {bx + r:.1f},{by:.1f} "
                 f"H{bx + bw - r:.1f} Q{bx + bw:.1f},{by:.1f} {bx + bw:.1f},{by + r:.1f} V{base:.1f} Z")
        on = date_of(start, d)
        bars.append({"d": shape, "x": round(bx - (slot - bw) / 2, 1), "w": round(slot, 1), "cx": round(bx + bw / 2, 1),
                     "title": f"{on:%a %d %b}: {_label(v)} {row.unit} used", "weekend": on.weekday() >= 5,
                     "label": f"{on:%d %b}".lstrip("0") if on.weekday() == 0 else ""})
    return {"w": W, "h": H, "left": L, "right": W - R, "top": T, "base": round(base, 1), "bars": bars,
            "yticks": [{"y": round(y(t), 1), "label": _label(t)} for t in ticks]}
