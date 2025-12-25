flowchart TD
  A[Config / Params] --> B

  subgraph DATA[Data Layer]
    B[OKX API: candles + positions + orders]
    C[Coinbase API: spot candles]
    B --> D[OKX 4H OHLCV]
    C --> E[CB 1H OHLCV]
    E --> F[Resample: 1H -> 4H (last close)]
    D --> G[Time Align / Merge]
    F --> G
  end

  subgraph FEAT[Feature Engineering]
    G --> H[CBP = CB_close / OKX_close - 1]
    H --> I[CBP_MA = SMA(CBP, N)]
    I --> J[excess = CBP - CBP_MA]
    D --> K[Williams %R on OKX (N_wr)]
    J --> L[z-score(excess, Lz) + shift(1)]
    K --> M[Gate: WR_lo < WR < WR_hi]
    J --> N[Gate: tau_lo <= |excess| <= tau_hi]
    L --> O[Signal z_out]
    M --> P[Gate OK]
    N --> P
    P --> O
  end

  subgraph RISK[Risk / Regime Overlay]
    D --> Q[Realized Vol: 1m/5m/30m/60m]
    Q --> R[Predict vol (weighted)]
    R --> S[MCMC / Monte Carlo paths (GBM)]
    S --> T[Crash Prob p_crash]
    T --> U{p_crash > threshold?}
  end

  subgraph EXEC[Execution Engine]
    O --> V[Size = floor + (1-floor)*tanh(alpha*(|z|-thr)+)]
    V --> W[Leverage / Notional Target]
    U -->|YES| X[Mode Override: SHORT_DEFENSIVE]
    U -->|NO| Y[Mode from z_out: LONG/SHORT/NEUTRAL]
    X --> Z[Grid Params: wider spread, defensive]
    Y --> Z
    R --> Z[Spread multiplier from vol]
    Z --> AA[Grid Order Levels & Prices]
    AA --> AB[Order Sizer -> contracts]
    AB --> AC[OKX Order Placement]
    AC --> AD[Position + PnL Tracking]
    AD --> AE[Logs / Metrics / CSV]
  end
