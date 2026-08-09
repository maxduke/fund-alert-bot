# Use the target ETF as the feeder fund's market reference

A Drawdown Add Plan binds one Reference ETF to one Investment Feeder Fund. The
Reference ETF supplies confirmed forward-adjusted (`qfq`) closes for the Recent
Peak, drawdown, and trend context, plus its validated realtime price for a
before-close estimate. The Investment Feeder Fund is the asset actually owned;
its published unit NAV and user-maintained Position Snapshot supply position
value, cost, and Price-Gain Reminders; V1 avoids a third official-index mapping
and supports only domestic A-share ETF feeder pairs whose calendars fit this
model.
