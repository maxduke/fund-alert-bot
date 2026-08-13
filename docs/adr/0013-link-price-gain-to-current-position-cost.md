# Link price-gain reminders to current position cost

`/add_profit` accepts `auto` in place of a numeric cost for an Investment Feeder
Fund. At evaluation time, that mode reads the current positive-unit Position
Snapshot or Position Estimate and compares its average unit cost with the latest
dated unit NAV, explicitly labeling estimated positions while existing
numeric-cost rules remain unchanged. Thresholds stay user-defined per fund and alert once per
continuous positive Position Cycle, so routine DCA cost changes do not re-arm
them; only an exact `0, 0` close followed by a new positive position starts a new
cycle, and no reminder chooses or performs a redemption.
