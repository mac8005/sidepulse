from __future__ import annotations

import math


ANIMATIONS = {
    "gentle": "Gentle — soft breathing",
    "flow": "Flow — a slow traveling wave",
    "kitt": "KITT — a relaxed return sweep",
    "tide": "Tide — alternating crossfade",
    "glow": "Glow — never fully dark",
    "steady": "Steady — no movement",
}
PALETTES = {
    "ocean": ("Ocean", "#4DA3FF", "#FFB020", "#39D98A"),
    "dusk": ("Dusk", "#AC8CFF", "#FFBE70", "#65D99A"),
    "ice": ("Ice", "#65CCFF", "#FFC16E", "#56D99B"),
}


def working_program(
    animation: str,
    color: str,
    finished_color: str,
    *,
    led_count: int = 8,
    show_finished: bool = False,
    lifetime_seconds: float | None = None,
) -> str:
    """Generate bounded-size programs for the two- and eight-LED firmware."""
    count = max(2, min(8, int(led_count)))
    start = count // 2 if show_finished else 0
    indexes = list(range(start, count))
    finished = [f"{i}:{finished_color}" for i in range(start)]

    def frame(colors: list[str], duration: int, easing: str = "cosine") -> str:
        # A shared duration keeps eight-LED programs below the 512-byte limit.
        assignments = " ".join(f"{i}:{c}" for i, c in zip(indexes, colors))
        return "; ".join([*finished, f"{assignments} {duration}ms {easing}"])

    dark = ["#000000"] * len(indexes)
    full = [color] * len(indexes)
    lines = [frame(dark, 400) if show_finished else "off 400ms cosine"]
    if animation == "steady":
        lines = [frame(full, 60000, "none")]
        cycle_ms = 60000
    elif animation in {"tide", "glow"}:
        low = "#" + "".join(f"{max(0, round(int(color[i:i + 2], 16) * 0.35)):02X}" for i in (1, 3, 5))
        first = [color if i % 2 == 0 else low for i in range(len(indexes))]
        second = [low if i % 2 == 0 else color for i in range(len(indexes))]
        if animation == "glow" or len(indexes) == 1:
            first, second = full, [low] * len(indexes)
        lines = [frame(second, 400), frame(first, 2200), frame(second, 2200)]
        cycle_ms = 4800
    elif animation == "gentle" or len(indexes) == 1:
        lines.append(frame(full, 2400, "pulse"))
        cycle_ms = 2800
    else:
        duration = 1000 if animation == "kitt" else 1800
        delays = [round(step * 800 / (len(indexes) - 1)) for step in range(len(indexes))]
        lines.append("; ".join(
            f"{i}:{color} {duration}ms pulse {delay}ms"
            for i, delay in zip(indexes, delays)
        ))
        cycle_ms = 400 + duration + 800
        if animation == "kitt":
            returning = indexes[-2::-1]
            lines.append("; ".join(
                f"{i}:{color} {duration}ms pulse {delay}ms"
                for i, delay in zip(returning, delays)
            ))
            cycle_ms += duration + delays[len(returning) - 1]
    if lifetime_seconds is None:
        return "\n".join([*lines, "repeat"])
    repeats = math.ceil(lifetime_seconds * 1000 / cycle_ms)
    final = "; ".join([*finished, " ".join(f"{i}:#000000" for i in indexes)])
    return "\n".join([*lines, f"repeat {repeats}", final])
