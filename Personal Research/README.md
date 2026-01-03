# Personal Research
```mermaid
flowchart TD
  A[Start] --> B[Load price data<br/>(BTC, ETH, BNB, ADA, XRP)]
  B --> C[Compute log returns]
  C --> D[Rolling window at time t]

  D --> E[Estimate expected returns μ_t<br/>(rolling mean / chosen estimator)]
  D --> F[Estimate covariance Σ_t<br/>(shrinkage / stabilization)]

  E --> G[Markowitz target weights w*_t<br/>argmin risk - λ return]
  F --> G

  G --> H[Constraints handling<br/>Long-only + fully-invested]
  H --> I[Project to simplex<br/>(w*_t feasible)]

  I --> J[LQR Rebalancing Controller]
  J --> K[State: current weights w_t]
  J --> L[Control: trade u_t = Δw_t]
  K --> M[Linear dynamics<br/>w_{t+1} = w_t + u_t (conceptually)]
  L --> M

  I --> N[Deviation signal: (w_t - w*_t)]
  N --> O[Riccati / DARE solve<br/>to get feedback gain K_t]
  O --> P[Feedback policy<br/>u_t = -K_t · (w_t - w*_t)]

  P --> Q[Apply transaction costs<br/>cost ∝ |Δw_t|]
  Q --> R[Portfolio wealth update<br/>with realized next-period returns]
  R --> S[Iterate t = t+1]
```
  S -->|until end| T[Performance evaluation]
  T --> U[Equity curve + metrics<br/>(return, volatility, Sharpe, turnover)]
  U --> V[End]
