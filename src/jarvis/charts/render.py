from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from jarvis.models import Candle


def render_chart(
    candles: list[Candle], report: dict, destination: Path, *, annotated: bool
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    from matplotlib.ticker import FuncFormatter, MaxNLocator

    colors = {"bg": "#101820", "fg": "#dce5ed", "up": "#40c9a2", "down": "#ef7f87"}
    with plt.rc_context(
        {
            "text.color": colors["fg"],
            "axes.labelcolor": colors["fg"],
            "xtick.color": colors["fg"],
            "ytick.color": colors["fg"],
            "font.size": 10,
        }
    ):
        fig, (ax, volume) = plt.subplots(
            2,
            1,
            figsize=(15, 8),
            sharex=True,
            gridspec_kw={"height_ratios": [5, 1]},
            facecolor=colors["bg"],
        )
        try:
            for axis in (ax, volume):
                axis.set_facecolor(colors["bg"])
                axis.grid(alpha=0.12, color=colors["fg"])
                for spine in axis.spines.values():
                    spine.set_color("#334451")
            for i, candle in enumerate(candles):
                color = colors["up"] if candle.close >= candle.open else colors["down"]
                ax.vlines(i, candle.low, candle.high, color=color, linewidth=0.8)
                height = abs(candle.close - candle.open)
                if height:
                    ax.add_patch(
                        Rectangle(
                            (i - 0.32, min(candle.open, candle.close)), 0.64, height, color=color
                        )
                    )
                else:
                    ax.hlines(candle.close, i - 0.32, i + 0.32, color=color)
                volume.bar(i, candle.volume, width=0.65, color=color, alpha=0.65)
            labels = []
            if annotated:
                for key, color, prefix in (
                    ("supports", "#60d6b0", "S"),
                    ("resistances", "#ffab80", "R"),
                ):
                    for i, level in enumerate(report[key], 1):
                        ax.axhspan(level["zone_low"], level["zone_high"], color=color, alpha=0.09)
                        ax.axhline(level["price"], color=color, linewidth=1.1, linestyle="--")
                        labels.append(
                            (level["price"], f"{prefix}{i}  {level['price']:,.8g}", color)
                        )
            ax.axhline(report["current_price"], color="#b4c9db", alpha=0.6, linewidth=0.8)
            if labels:
                lower, upper = ax.get_ylim()
                ordered = sorted(labels)
                positions = []
                for price, _, _ in ordered:
                    desired = max(0.025, min(0.975, (price - lower) / (upper - lower)))
                    positions.append(max(desired, positions[-1] + 0.035) if positions else desired)
                shift = max(0, positions[-1] - 0.975)
                for (price, label, color), position in zip(ordered, positions):
                    # Only label text is displaced; the leader anchors to the exact price line.
                    ax.annotate(
                        label,
                        xy=(1, price),
                        xycoords=("axes fraction", "data"),
                        xytext=(1.035, position - shift),
                        textcoords="axes fraction",
                        va="center",
                        fontsize=9,
                        color=color,
                        annotation_clip=False,
                        arrowprops={"arrowstyle": "-", "color": color, "lw": 0.7},
                    )
            ax.set_ylabel(report["symbol"].split("/")[1], labelpad=12)
            ax.ticklabel_format(axis="y", style="plain", useOffset=False)
            volume.set_ylabel("Volume")
            volume.yaxis.set_major_locator(MaxNLocator(3))
            volume.xaxis.set_major_locator(MaxNLocator(8, integer=True))

            def date_label(x, _):
                index = int(round(x))
                if 0 <= index < len(candles):
                    date = datetime.fromtimestamp(candles[index].timestamp / 1000, timezone.utc)
                    return date.strftime("%m-%d\n%H:%M")
                return ""

            volume.xaxis.set_major_formatter(FuncFormatter(date_label))
            ax.set_xlim(-1, len(candles))
            label = "SIMULATION | " if report["simulation"] else ""
            ax.set_title(
                f"{label}{report['symbol']}  /  {report['exchange'].upper()} SPOT  /  {report['timeframe']}\n"
                f"{len(candles)} completed candles | observed {report['observed_at']}",
                loc="left",
                pad=16,
            )
            fig.text(
                0.06,
                0.025,
                "Jarvis | UTC | Linear price scale | "
                + (
                    "Confirmed pivot zones; no forecast" if annotated else "Unannotated OHLCV chart"
                ),
                color="#92a6b6",
                fontsize=9,
            )
            fig.subplots_adjust(left=0.06, right=0.82, top=0.88, bottom=0.1, hspace=0.08)
            fig.savefig(
                destination,
                dpi=140,
                facecolor=fig.get_facecolor(),
                bbox_inches="tight",
                pad_inches=0.2,
            )
        finally:
            plt.close(fig)
