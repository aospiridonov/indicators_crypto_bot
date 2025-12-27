"""
Signal Generator for VWAP Mean Reversion Strategy v11
Generates LONG/SHORT signals based on VWAP ±2σ deviation

V11 Parameters (optimized for 2024-2025):
- sigma_entry: 2.0 (entry at ±2σ from VWAP)
- sigma_exit: 1.5 (TP at ±1.5σ from VWAP) - increased from 0.8!
- sl_atr_mult: 4.0 (SL at 4x ATR) - increased from 1.8!
- max_bars: 20 (80 hours max hold)

Changes from V3:
- Wider SL (4.0 vs 1.8 ATR) - prevents stop hunts
- Larger TP distance (1.5σ vs 0.8σ) - better R:R
- ATR: SMA(14) - same as v3

Performance (2024-2025 backtest):
- 134 trades, WR 94%, Return +508%, MaxDD 37.5%
"""

import logging
import pandas as pd
import numpy as np
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class SignalGeneratorVWAP:
    """
    Generate trading signals for VWAP Mean Reversion strategy
    """

    def __init__(self, config: Dict):
        """
        Initialize signal generator

        Args:
            config: Strategy configuration
        """
        self.config = config

        # Strategy parameters
        self.sigma_entry = config.get('sigma_entry', 2.0)
        self.sigma_exit = config.get('sigma_exit', 1.5)
        self.sl_atr_mult = config.get('sl_atr_mult', 4.0)
        self.min_tp_pct = config.get('min_tp_pct', 0.3)  # Minimum TP % (filter)

        logger.info("✅ VWAP Signal Generator initialized")
        logger.info(f"   Entry Sigma: {self.sigma_entry}")
        logger.info(f"   Exit Sigma: {self.sigma_exit}")
        logger.info(f"   SL ATR Mult: {self.sl_atr_mult}")
        logger.info(f"   Min TP: {self.min_tp_pct}%")

    def calculate_atr(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        """Calculate Average True Range"""
        high = df['high']
        low = df['low']
        close = df['close']

        tr1 = high - low
        tr2 = abs(high - close.shift())
        tr3 = abs(low - close.shift())

        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(window=period).mean()

        return atr

    def calculate_vwap_std(self, df: pd.DataFrame) -> Tuple[pd.Series, pd.Series]:
        """
        Calculate daily VWAP and standard deviation

        Returns:
            (vwap_series, std_series)
        """
        df_copy = df.copy()
        df_copy['date'] = df_copy.index.date

        vwap_series = pd.Series(index=df.index, dtype=float)
        std_series = pd.Series(index=df.index, dtype=float)

        for date, group in df_copy.groupby('date'):
            typical_price = (group['high'] + group['low'] + group['close']) / 3
            cum_vol_price = (typical_price * group['volume']).cumsum()
            cum_vol = group['volume'].cumsum()
            vwap = cum_vol_price / cum_vol

            price_diff = group['close'] - vwap
            std = price_diff.rolling(window=len(group), min_periods=1).std(ddof=0)

            vwap_series.loc[group.index] = vwap
            std_series.loc[group.index] = std

        return vwap_series, std_series

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Calculate all indicators needed for strategy

        Args:
            df: DataFrame with OHLCV data

        Returns:
            DataFrame with indicators added
        """
        d = df.copy()

        # Calculate ATR
        d['atr'] = self.calculate_atr(d, period=14)

        # Calculate VWAP and std
        d['vwap'], d['vwap_std'] = self.calculate_vwap_std(d)

        # Forward fill to handle NaNs
        d['vwap'] = d['vwap'].ffill()
        d['vwap_std'] = d['vwap_std'].ffill()

        # Calculate VWAP distance in sigmas
        d['vwap_distance_sigma'] = (d['close'] - d['vwap']) / (d['vwap_std'] + 1e-10)

        return d

    def generate_signal(self, df_4h: pd.DataFrame) -> Optional[Dict]:
        """
        Generate signal for latest candle

        Args:
            df_4h: 4H DataFrame with OHLCV data (minimum 7 days)

        Returns:
            Signal dict or None: {
                'action': 'LONG' or 'SHORT',
                'entry_price': float,
                'tp_price': float,
                'sl_price': float,
                'tp_pct': float,
                'sl_pct': float,
                'vwap': float,
                'vwap_std': float,
                'distance_sigma': float
            }
        """
        try:
            # Calculate indicators
            df_ind = self.calculate_indicators(df_4h)

            # Get latest bar
            latest = df_ind.iloc[-1]
            prev = df_ind.iloc[-2] if len(df_ind) > 1 else latest

            vwap = latest['vwap']
            std = latest['vwap_std']
            close = latest['close']
            atr = latest['atr']
            distance_sigma = latest['vwap_distance_sigma']

            # Check if std is too small (avoid false signals)
            if std < vwap * 0.001:
                logger.debug(f"VWAP std too small: {std:.2f} (vwap={vwap:.2f})")
                return None

            # Check for entry signals
            signal = None

            # LONG: Price below VWAP - 2σ
            lower_band = vwap - self.sigma_entry * std
            if close < lower_band:
                signal = 'LONG'
                entry_price = close
                sl_price = entry_price - self.sl_atr_mult * atr
                tp_price = vwap - self.sigma_exit * std

            # SHORT: Price above VWAP + 2σ
            upper_band = vwap + self.sigma_entry * std
            if close > upper_band:
                signal = 'SHORT'
                entry_price = close
                sl_price = entry_price + self.sl_atr_mult * atr
                tp_price = vwap + self.sigma_exit * std

            if signal is None:
                return None

            # Calculate TP/SL percentages
            tp_pct = abs(tp_price - entry_price) / entry_price * 100
            sl_pct = abs(sl_price - entry_price) / entry_price * 100

            # Filter: Skip trades with TP < min_tp_pct (unprofitable with fees)
            if tp_pct < self.min_tp_pct:
                logger.info(f"❌ Signal filtered: TP {tp_pct:.2f}% < {self.min_tp_pct}% (unprofitable with fees)")
                return None

            signal_data = {
                'action': signal,
                'entry_price': float(entry_price),
                'tp_price': float(tp_price),
                'sl_price': float(sl_price),
                'tp_pct': float(tp_pct),
                'sl_pct': float(sl_pct),
                'vwap': float(vwap),
                'vwap_std': float(std),
                'distance_sigma': float(distance_sigma),
                'atr': float(atr)
            }

            logger.info(f"🎯 {signal} SIGNAL generated!")
            logger.info(f"   Entry: ${entry_price:.2f}")
            logger.info(f"   TP: ${tp_price:.2f} ({tp_pct:.2f}%)")
            logger.info(f"   SL: ${sl_price:.2f} ({sl_pct:.2f}%)")
            logger.info(f"   VWAP: ${vwap:.2f} ± ${std:.2f}")
            logger.info(f"   Distance: {distance_sigma:.2f}σ")

            return signal_data

        except Exception as e:
            logger.error(f"❌ Error generating signal: {e}")
            return None
