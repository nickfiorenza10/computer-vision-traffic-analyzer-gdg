"""One definition of the traffic level, for the 30-minute sweep and for live analysis.

Two very different measurements feed the same word on the map — "heavy", "moderate",
"light" — so the thresholds live here once and both callers import them. The numbers are
documented constants rather than magic: Daksh signs off on them, and each response carries
the measurements that produced the label so anyone can see why.

Live analysis has tracking, so it reads three things: flow (vehicles per minute crossing a
counting line), density (mean vehicles inside the road per frame) and a speed proxy (median
track movement per second relative to box height). A sweep has eight stills and no tracking,
so it has density only, judged against that camera's own history.
"""

from __future__ import annotations

import numpy as np

HEAVY = "heavy"
MODERATE = "moderate"
LIGHT = "light"
LEARNING = "learning"
NIGHT = "night"
# A sweep the sun says is in daylight but whose road is dark: a camera problem (a lens cap, a
# dead lamp, a frozen night frame), not traffic. Stored as the schema's "unknown" level.
UNKNOWN = "unknown"

# What `lighting` answers, for choosing a detector and labelling a sweep.
DAY = "day"
DARK_IN_DAYLIGHT = "dark_in_daylight"

# Live thresholds. Density is vehicles inside the road area at once; flow is vehicles per
# minute; speed is box-heights per second, so 0.6 means a vehicle travels its own height in
# about 0.9 s of screen space.
LIVE_HEAVY_DENSITY = 8.0
LIVE_MEDIUM_DENSITY = 3.5
LIVE_SLOW_SPEED = 0.6
LIVE_STEADY_FLOW = 20.0

# Sweep: a camera is judged against itself. Until it has eight sweeps there is nothing to
# be above, so the level is "learning" rather than a guess.
SWEEP_MIN_SWEEPS = 8
SWEEP_HEAVY_QUANTILE = 0.8
SWEEP_MODERATE_QUANTILE = 0.4

# Below this road brightness (see `frame_brightness`) ordinary detection cannot see vehicles,
# so a sweep is labelled "night" and live analysis uses headlight mode. Measured: Caltrans
# D3-52, a dark camera where YOLO finds nothing, sits between 25 and 51; daytime footage sits
# above 75. 60 is the gap between them, not a guess.
NIGHT_MEAN_BRIGHTNESS = 60.0


def live_level(flow_per_min: float, density: float, speed: float) -> str:
    """heavy, moderate or light, from the three live measurements."""
    if density >= LIVE_HEAVY_DENSITY:
        return HEAVY
    if density >= LIVE_MEDIUM_DENSITY and speed < LIVE_SLOW_SPEED:
        return HEAVY
    if flow_per_min >= LIVE_STEADY_FLOW or density >= LIVE_MEDIUM_DENSITY:
        return MODERATE
    return LIGHT


def live_reason(flow_per_min: float, density: float, speed: float) -> str:
    """The same decision as a sentence, for the panel and the logs."""
    level = live_level(flow_per_min, density, speed)
    if level == HEAVY and density >= LIVE_HEAVY_DENSITY:
        return f"{density:.1f} vehicles in the road at once (heavy at {LIVE_HEAVY_DENSITY:g})"
    if level == HEAVY:
        return (
            f"{density:.1f} vehicles and slow movement ({speed:.2f} box-heights/s, "
            f"slow below {LIVE_SLOW_SPEED:g})"
        )
    if level == MODERATE:
        return f"flow {flow_per_min:.0f}/min or {density:.1f} vehicles at once"
    return f"light: {flow_per_min:.0f}/min and {density:.1f} vehicles at once"


def sweep_level(vehicles_mean: float, history: list[float]) -> str:
    """heavy, moderate, light or learning, from a sweep and that camera's own history.

    `history` is the camera's earlier sweeps (not including this one). At or above its 80th
    percentile is heavy, the 40th to 80th is moderate, below that light.
    """
    if len(history) < SWEEP_MIN_SWEEPS:
        return LEARNING
    ordered = sorted(history)
    at_or_below = sum(1 for value in ordered if value <= vehicles_mean) / len(ordered)
    if at_or_below >= SWEEP_HEAVY_QUANTILE:
        return HEAVY
    if at_or_below >= SWEEP_MODERATE_QUANTILE:
        return MODERATE
    return LIGHT


def is_night(mean_brightness: float) -> bool:
    """Whether a frame is dark enough that ordinary detection cannot see vehicles."""
    return mean_brightness < NIGHT_MEAN_BRIGHTNESS


def lighting(phase: str | None, mean_brightness: float) -> str:
    """"day", "night" or "dark_in_daylight": the sun first, the road's brightness second.

    `phase` is `daylight.phase(...)` for the camera's own coordinates ("day", "twilight",
    "night"), or None when they are unknown. The sun settles day and night outright: a lit
    interchange at 2 am is still night, and a dark frame at noon is a camera problem, not
    night. Only in civil twilight, or without coordinates, does brightness decide.
    """
    if phase == NIGHT:
        return NIGHT
    if phase == DAY:
        return DARK_IN_DAYLIGHT if is_night(mean_brightness) else DAY
    return NIGHT if is_night(mean_brightness) else DAY


def frame_brightness(frame: np.ndarray) -> float:
    """How bright the road itself is: the median pixel of the middle of the frame.

    Not the mean. On a dark camera the mean is dragged up by headlights, the bridge lamps
    and the burned-in text, and Caltrans' D3-52 - a camera where ordinary detection finds
    nothing - averages 55 while its median pixel is 38. The median asks "is most of the
    road dark", which is the question headlight mode actually answers. The top 25% and
    bottom 10% are skipped for the same reason the view check skips them: names and
    timestamps do not describe the road.
    """
    if frame is None:
        return 0.0
    gray = frame if frame.ndim == 2 else np.mean(frame, axis=2).astype(np.uint8)
    height = gray.shape[0]
    top = int(round(height * 0.25))
    bottom = int(round(height * 0.10))
    road = gray[top : height - bottom if bottom else height, :]
    if road.size == 0:
        road = gray
    return float(np.median(road))
