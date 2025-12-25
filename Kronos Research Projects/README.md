# Kronos Quant Project — Cross-Venue CBP Strategy (OKX × Coinbase)

A production-style crypto trading system that combines a **Coinbase Premium (CBP) alpha signal** with a **risk/regime overlay** (vol forecasting + Monte Carlo crash probability) and executes via an **adaptive grid engine** on OKX perpetual futures.

> **Core idea:** measure cross-venue pricing pressure (Coinbase spot vs OKX perp), trade only when the premium signal is statistically significant and technically “clean”, and dynamically defend against crash regimes.

---

## Highlights

- **Cross-venue alpha**: Coinbase spot vs OKX perp premium (CBP)
- **Signal safety**: rolling de-mean + **shift(1)** to avoid lookahead
- **Timing filter**: Williams %R regime gate + band-pass thresholding
- **Risk overlay**: realized vol → weighted forecast → **GBM Monte Carlo** crash probability
- **Execution**: volatility-aware **adaptive grid**, mode switching (LONG/SHORT/NEUTRAL/DEFENSIVE)
- **Research-ready**: backtest pipeline + metrics (Sharpe / Sortino / Jensen alpha / MDD)

---

## Strategy Pipeline

```mermaid
flowchart TD
  A["Config / Params"] --> B

  subgraph DATA["Data Layer"]
    B["OKX API: candles, positions, orders"]
    C["Coinbase API: spot candles"]
    B --> D["OKX 4H OHLCV"]
    C --> E["CB 1H OHLCV"]
    E --> F["Resample 1H to 4H (last close)"]
    D --> G["Time align / merge"]
    F --> G
  end

  subgraph FEAT["Feature Engineering"]
    G --> H["CBP = CB_close / OKX_close - 1"]
    H --> I["CBP_MA = SMA(CBP, N)"]
    I --> J["excess = CBP - CBP_MA"]
    D --> K["Williams %R on OKX (N_wr)"]
    J --> L["z-score(excess, Lz) then shift(1)"]
    K --> M["Gate: WR_lo < WR < WR_hi"]
    J --> N["Gate: tau_lo <= abs(excess) <= tau_hi"]
    M --> P["Gate OK"]
    N --> P
    P --> O["Signal z_out"]
  end

  subgraph RISK["Risk / Regime Overlay"]
    D --> Q["Realized vol: 1m, 5m, 30m, 60m"]
    Q --> R["Predict vol (weighted)"]
    R --> S["Monte Carlo paths (GBM)"]
    S --> T["Crash prob p_crash"]
    T --> U{"p_crash > threshold?"}
  end

  subgraph EXEC["Execution Engine"]
    O --> V["Size = floor + (1-floor)*tanh(alpha*max(abs(z)-thr, 0))"]
    V --> W["Leverage / Notional target"]
    U -->|YES| X["Mode override: SHORT_DEFENSIVE"]
    U -->|NO| Y["Mode from z_out: LONG / SHORT / NEUTRAL"]
    X --> Z["Grid params: wider spread, defensive"]
    Y --> Z
    R --> Z["Spread multiplier from vol"]
    Z --> AA["Grid order levels & prices"]
    AA --> AB["Order sizer -> contracts"]
    AB --> AC["OKX order placement"]
    AC --> AD["Position + PnL tracking"]
    AD --> AE["Logs / metrics / CSV"]
  end
