"""Render the same closed candles used by indicators for Gemini vision."""
from io import BytesIO


def render_chart(snapshot) -> bytes:
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.patches import Rectangle

    fig = Figure(figsize=(11, 7), dpi=100, layout='constrained')
    FigureCanvasAgg(fig)
    axes = fig.subplots(2, 2, gridspec_kw={'height_ratios': [3, 1]})
    for column, (frame, label) in enumerate(((snapshot.df15, 'Trend'), (snapshot.df1, 'Entry'))):
        df = frame.tail(60)
        ax, rsi = axes[0, column], axes[1, column]
        for i, row in enumerate(df.itertuples()):
            color = '#169c70' if row.close >= row.open else '#d34959'
            ax.vlines(i, row.low, row.high, color=color, linewidth=0.8)
            ax.add_patch(Rectangle((i-.3, min(row.open, row.close)), .6, max(abs(row.close-row.open), 0.01), facecolor=color))
        ax.plot(df.ema_fast.to_numpy(), label='EMA fast', linewidth=1)
        ax.plot(df.ema_slow.to_numpy(), label='EMA slow', linewidth=1)
        for zone in snapshot.supports + snapshot.resistances:
            ax.axhline(zone.price, alpha=.2, color='blue', linestyle='--')
        ax.set_title(f'{label}: closed candles | last {df.ts.iloc[-1]:%m-%d %H:%M} UTC')
        ax.legend(fontsize=7)
        rsi.plot(df.rsi.to_numpy(), color='#7053b5')
        rsi.axhline(70, color='gray', linestyle='--')
        rsi.axhline(30, color='gray', linestyle='--')
        rsi.set_ylim(0, 100)
        rsi.set_ylabel('RSI')
    fig.suptitle('GMX oracle candles — volume unavailable; horizontal lines = detected S/R')
    buffer = BytesIO()
    fig.savefig(buffer, format='png')
    return buffer.getvalue()
