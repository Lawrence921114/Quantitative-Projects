``` mermaid

flowchart TD
  A["Loop (~1s)"] --> B["Fetch OKX trades + 1m candles + 4h candles"]
  B --> C["Get current price + position + avg entry + UPL"]

  C --> D{"Every 30s?"}
  D -->|Yes| E["FeatureEngine: vol_1m/5m/30m/60m\nstd(diff(log price))"]
  E --> F["Predictor: weighted vol forecast"]
  F --> G["Monte Carlo (GBM): 3000 paths\n10min horizon, 30s steps"]
  G --> H["CrashProb = P(min path &lt; 0.98·P_now)"]
  D -->|No| I["Use last CrashProb/Vol"]

  C --> J["SignalEngine (cached 5min):\nCB premium excess z-score + WR gate"]
  J --> K["sizing = floor + (1-floor)*tanh(alpha*max(|z|-thr,0))"]

  H --> L{"CrashProb > 0.60?"}
  L -->|Yes| M["Force SHORT_DEFENSIVE\nspread_factor=1.2"]
  L -->|No| N["Normal regime:\nz>0.5 LONG_AGGRESSIVE\nz<-0.5 SHORT_DEFENSIVE\nelse NEUTRAL"]

  I --> O{"Pred vol > 0.005?"}
  O -->|Yes| P["spread_factor *= 1.5"]
  O -->|No| Q["spread_factor unchanged"]

  M --> R{"Every 30s: grid refresh"}
  N --> R
  P --> R
  Q --> R

  R -->|Yes| S["GridTrader.execute_grid:\ncancel_all_orders + batch place limits"]
  R -->|No| T["Skip placing"]

  S --> U["PnLTracker.update + export every 5min"]
  T --> U
  U --> A
```
