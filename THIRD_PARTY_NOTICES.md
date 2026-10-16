# Third-Party Notices

This repository includes software derived from third-party projects. The
following notice applies to the files identified below.

## AlphaSift

- Project: AlphaSift
- Source: https://github.com/ZhuLinsen/alphasift
- Referenced revision: `9f522747caafd3c0b1ddb7e14d5cf44c8580b6cf`
- License: Apache License 2.0
- Included and modified files: `src/services/screening/**/*.py` and
  `src/services/screening/strategies/*.yaml`
- License copy: `src/services/screening/LICENSE`

The included code has been modified and integrated into
`daily_stock_analysis`. Per-file headers identify the source revision and
modification status.

## Optional intraday research adapter

`scripts/research/intraday_backtest.py` is a standalone GPL-3.0-or-later program
using [Backtrader](https://github.com/mementum/backtrader), pinned to 1.9.78.123.
Its license is provided in `scripts/research/COPYING`. It is installed only with
`requirements-backtest.txt` (and backend test tooling), is not imported by the
application and does not change the license of the existing application files.
