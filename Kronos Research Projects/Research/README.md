<pre>
```mermaid
flowchart TB
  %% Inputs
  x[|excess_t| (magnitude)]
  wr[WR_t (Williams%R)]
  z[z-score of CBP_excess]

  %% Gates
  subgraph Gates
    Gband[G_band(t) = 1 if τ_LOW ≤ |excess_t| ≤ τ_HIGH]
    Gwr[G_wr(t) = 1 if WR_LOW < WR_t < WR_HIGH]
    G[G(t) = G_band(t) * G_wr(t)]
  end

  %% Effective signal
  zeff[z^{eff}_t = z_t if G(t)=1 else 0]

  %% Mode selection
  subgraph ModeSelection
    long[LONG_AGGRESSIVE if z^{eff}_t > 0.5]
    neutral[NEUTRAL if |z^{eff}_t| ≤ 0.5]
    short[SHORT_DEFENSIVE if z^{eff}_t < −0.5]
  end

  %% Edges
  x --> Gband
  wr --> Gwr
  z --> zeff
  Gband --> G
  Gwr --> G
  G --> zeff
  zeff --> long
  zeff --> neutral
  zeff --> short
```
</pre>
