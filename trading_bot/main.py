"""
Indicators Crypto Bot - VWAP Mean Reversion Strategy v3
Main entry point for VWAP ±2σ strategy with adaptive leverage

Orchestrates:
- Data management (4H candles with caching)
- Signal generation (VWAP-based, no ML)
- Position management (TP/SL/Timeout)
- Order execution (Bybit API with adaptive 7x leverage)
- Telegram notifications
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from trading_bot.data_manager_vwap import TradingDataManagerVWAP
from trading_bot.exchange_connector import BybitConnector
from trading_bot.position_tracker import Position, PositionTracker
from trading_bot.signal_generator_vwap import SignalGeneratorVWAP
from trading_bot.telegram_command_handler import TelegramCommandHandler
from trading_bot.telegram_notifier import TelegramNotifier
from trading_bot.trade_logger import TradeLogger
from trading_bot.websocket_manager import BybitWebSocketManager

# Setup logging
log_dir = Path('logs')
log_dir.mkdir(parents=True, exist_ok=True)

# Try to write to logs, fallback to /tmp if permission denied
try:
    log_file = log_dir / 'trading_vwap.log'
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler()
        ]
    )
except PermissionError:
    log_file = Path('/tmp/trading_vwap.log')
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler()
        ]
    )
    print(f"Warning: Using /tmp for logs due to permission error")

# Suppress noisy websocket messages
logging.getLogger('websocket').setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


class TradingBotVWAP:
    """
    Main trading bot orchestrator for VWAP Mean Reversion strategy
    """

    def __init__(self, config_path: str = 'config/trading_params.json'):
        """
        Initialize trading bot

        Args:
            config_path: Path to trading configuration
        """
        logger.info("="*80)
        logger.info("INDICATORS CRYPTO BOT - VWAP MEAN REVERSION v3")
        logger.info("="*80)

        # Load configuration
        with open(config_path, 'r') as f:
            self.config = json.load(f)

        # Check demo mode from environment
        use_demo = os.getenv('BYBIT_DEMO_MODE', 'true').lower() == 'true'

        logger.info(f"📋 Configuration loaded:")
        logger.info(f"   Strategy: VWAP Mean Reversion v3")
        logger.info(f"   Symbol: {self.config['symbol']}")
        logger.info(f"   Timeframe: {self.config['timeframe']}")
        logger.info(f"   Leverage: {self.config['leverage']}x (adaptive)")
        logger.info(f"   Position Size: {self.config['position_size_pct']*100}%")
        logger.info(f"   Max Hold: {self.config['max_hold_hours']}h")
        logger.info(f"   Mode: {'🎮 DEMO (virtual money)' if use_demo else '🚀 LIVE (REAL MONEY!)'}")

        # Initialize components
        logger.info("\n🔧 Initializing components...")

        # Telegram notifier
        telegram_enabled = os.getenv('TELEGRAM_BOT_TOKEN') is not None
        self.notifier = TelegramNotifier(enabled=telegram_enabled)

        # Exchange connector
        self.exchange = BybitConnector(self.config, demo=use_demo)

        # Test connection
        logger.info("🔗 Testing Bybit connection...")
        if self.exchange.test_connection():
            logger.info("✅ Connection test passed")
        else:
            logger.warning("⚠️ Connection test failed, will retry on first request")

        # Data manager (handles caching and downloading of 4H bars)
        self.data_manager = TradingDataManagerVWAP(
            historical_csv_path='data/btcusdt_4h.csv',
            state_file_path='data/state_vwap.json',
            min_days=self.config.get('min_history_days', 7)
        )

        # Signal generator
        self.signal_generator = SignalGeneratorVWAP(self.config)

        # Position tracker
        self.position_tracker = PositionTracker()

        # Trade logger
        self.trade_logger = TradeLogger('data/trades_vwap.json')

        # WebSocket manager (for position updates and 4H kline stream)
        self.ws_manager = BybitWebSocketManager(
            symbol=self.config['symbol'],
            demo=use_demo,
            on_position_update=self._handle_websocket_position_update,
            on_kline_update=self._handle_kline_update
        )

        # Telegram command handler (already starts polling in __init__)
        if telegram_enabled:
            self.telegram_handler = TelegramCommandHandler(
                self.exchange,
                self.position_tracker,
                self.trade_logger
            )
            logger.info("✅ Telegram command handler ready")

        # Adaptive leverage state
        self.consecutive_sl = 0
        self.consecutive_wins = 0
        self.current_leverage = self.config['leverage']
        self.pause_until = None
        self.peak_capital = None
        self.initial_balance = None

        # Cached balance (pre-fetched before 4H candle close)
        self.cached_balance = None
        self.balance_prefetched = False

        # Strategy parameters
        self.base_leverage = self.config['leverage']
        self.min_leverage = self.config.get('min_leverage', 3)
        self.sl_threshold = self.config.get('consecutive_sl_threshold', 2)
        self.pause_threshold = self.config.get('pause_after_sl_streak', 3)
        self.pause_hours = self.config.get('pause_hours', 24)
        self.dd_threshold_1 = self.config.get('max_dd_threshold_1', 35.0)
        self.dd_threshold_2 = self.config.get('max_dd_threshold_2', 45.0)
        self.recovery_wins = self.config.get('recovery_win_streak', 5)
        self.recovery_dd = self.config.get('recovery_dd_threshold', 10.0)
        self.enable_adaptive = self.config.get('enable_adaptive_leverage', True)

        logger.info("\n✅ Bot initialization complete!")

    def _handle_websocket_position_update(self, position_data: Dict):
        """
        Handle WebSocket position updates (when exchange closes position via TP/SL)

        Args:
            position_data: Position update from WebSocket
        """
        try:
            if position_data.get('event') == 'closed':
                # Position closed by exchange (TP or SL hit)
                logger.info(f"📬 WebSocket: Position closed by exchange")

                if not self.position_tracker.has_position():
                    logger.warning("⚠️ Received position close but no tracked position")
                    return

                position = self.position_tracker.get_open_positions()[0]

                # Get PnL from WebSocket (more accurate than calculation)
                pnl = position_data.get('realised_pnl', 0.0)
                exit_price = position_data.get('entry_price', 0.0)  # Bybit keeps entry_price even after close
                tp_price = position_data.get('take_profit', 0.0)
                sl_price = position_data.get('stop_loss', 0.0)

                # Determine exit reason (TP or SL)
                if tp_price > 0 and abs(exit_price - tp_price) < abs(exit_price - sl_price):
                    exit_reason = 'TP'
                elif sl_price > 0:
                    exit_reason = 'SL'
                else:
                    exit_reason = 'CLOSED'

                # Update streak counters
                if exit_reason == 'SL':
                    self.consecutive_sl += 1
                    self.consecutive_wins = 0

                    # Trigger pause
                    if self.consecutive_sl >= self.pause_threshold:
                        self.pause_until = datetime.utcnow() + timedelta(hours=self.pause_hours)
                        logger.warning(f"⏸️ Trading paused until {self.pause_until} ({self.consecutive_sl} SL)")
                        self.notifier.send(
                            f"⚠️ TRADING PAUSED\n"
                            f"Consecutive SL: {self.consecutive_sl}\n"
                            f"Resume at: {self.pause_until.strftime('%Y-%m-%d %H:%M')}"
                        )
                else:
                    self.consecutive_sl = 0
                    if pnl > 0:
                        self.consecutive_wins += 1

                # Log trade
                trade_data = {
                    'timestamp': datetime.utcnow().isoformat(),
                    'action': position.action,
                    'entry_price': position.entry_price,
                    'exit_price': exit_price,
                    'tp_price': position.tp_price,
                    'sl_price': position.sl_price,
                    'size_usdt': position.size_usdt,
                    'leverage': self.current_leverage,
                    'exit_reason': exit_reason,
                    'pnl': pnl,
                    'pnl_pct': (pnl / position.size_usdt) * 100 if position.size_usdt > 0 else 0,
                    'consecutive_sl': self.consecutive_sl,
                    'consecutive_wins': self.consecutive_wins
                }

                self.trade_logger.log_trade(trade_data)

                # Clear position
                self.position_tracker.close_position(position.action, exit_price, exit_reason, self.current_leverage)

                # Fetch updated balance after position close
                balance_info = self.exchange.fetch_balance()
                new_balance = balance_info['free'] if balance_info else 0
                self.cached_balance = new_balance  # Update cache

                # Send notification
                emoji = '✅' if pnl > 0 else '❌'
                self.notifier.send(
                    f"{emoji} POSITION CLOSED: {exit_reason}\n"
                    f"Entry: ${position.entry_price:.2f}\n"
                    f"Exit: ${exit_price:.2f}\n"
                    f"PnL: ${pnl:.2f} ({trade_data['pnl_pct']:.2f}%)\n"
                    f"Leverage: {self.current_leverage}x\n"
                    f"Balance: ${new_balance:.2f}\n"
                    f"Win Streak: {self.consecutive_wins} | SL Streak: {self.consecutive_sl}"
                )

                logger.info(f"✅ Position closed via WebSocket, PnL: ${pnl:.2f}, Balance: ${new_balance:.2f}")

        except Exception as e:
            logger.error(f"❌ Error handling WebSocket position update: {e}", exc_info=True)

    def _handle_kline_update(self, candle: Dict):
        """
        Handle 4H kline updates (when new candle closes)

        Args:
            candle: Candle data from WebSocket
        """
        try:
            if not candle.get('confirm'):
                return  # Only process closed candles

            logger.info(f"🕯️ New 4H candle closed: ${candle['close']:.2f}")

            # Update cached data with new candle
            self.data_manager.update_historical_data(self.exchange)

            # Check for new signal (only if no open position)
            if not self.position_tracker.has_position():
                logger.info("🔍 Checking for signal after 4H candle close...")
                # Use pre-fetched balance if available
                self.check_and_execute_signal(use_cached_balance=True)

            # Reset balance prefetch flag after candle close
            self.balance_prefetched = False

        except Exception as e:
            logger.error(f"❌ Error handling kline update: {e}", exc_info=True)

    def _prefetch_balance(self):
        """
        Pre-fetch balance ~3 minutes before 4H candle close
        This allows instant trade execution when signal arrives
        """
        try:
            balance_info = self.exchange.fetch_balance()
            if balance_info:
                self.cached_balance = balance_info['free']
                self.balance_prefetched = True
                logger.info(f"💰 Balance pre-fetched: ${self.cached_balance:.2f}")
        except Exception as e:
            logger.error(f"❌ Error pre-fetching balance: {e}")

    def update_adaptive_leverage(self, balance: float, open_position_exists: bool):
        """
        Update leverage based on adaptive risk management logic

        Args:
            balance: Current account balance in USDT
            open_position_exists: Whether there's an open position
        """
        if not self.enable_adaptive:
            self.current_leverage = self.base_leverage
            return

        # Initialize peak capital on first call
        if self.peak_capital is None:
            self.peak_capital = balance
            self.initial_balance = balance

        # Update peak
        if balance > self.peak_capital:
            self.peak_capital = balance

        # Calculate drawdown
        current_dd = (self.peak_capital - balance) / self.peak_capital * 100 if self.peak_capital > 0 else 0

        # Adaptive logic
        prev_leverage = self.current_leverage

        # 1. Check circuit breaker (DD > 45%)
        if current_dd > self.dd_threshold_2:
            logger.critical(f"🚨 CIRCUIT BREAKER: DD {current_dd:.1f}% > {self.dd_threshold_2}% - STOPPING TRADING!")
            self.notifier.send(f"🚨 CIRCUIT BREAKER TRIGGERED!\nDD: {current_dd:.1f}%\n⛔ Trading stopped!")
            # Don't change leverage, just stop trading
            return

        # 2. Consecutive SL protection
        if self.consecutive_sl >= self.sl_threshold:
            self.current_leverage = self.min_leverage
            if prev_leverage != self.current_leverage:
                logger.warning(f"⚠️ Leverage reduced to {self.current_leverage}x (consecutive SL: {self.consecutive_sl})")

        # 3. High DD protection
        elif current_dd > self.dd_threshold_1:
            self.current_leverage = (self.base_leverage + self.min_leverage) // 2
            if prev_leverage != self.current_leverage:
                logger.warning(f"⚠️ Leverage reduced to {self.current_leverage}x (DD: {current_dd:.1f}%)")

        # 4. Recovery
        elif self.consecutive_wins >= self.recovery_wins and current_dd < self.recovery_dd:
            self.current_leverage = self.base_leverage
            if prev_leverage != self.current_leverage:
                logger.info(f"✅ Leverage restored to {self.current_leverage}x (wins: {self.consecutive_wins}, DD: {current_dd:.1f}%)")

    def check_and_execute_signal(self, use_cached_balance: bool = False):
        """
        Check for trading signals and execute if found

        Args:
            use_cached_balance: If True, use pre-fetched balance instead of fetching new
        """
        try:
            # Check if trading is paused
            if self.pause_until is not None:
                if datetime.utcnow() < self.pause_until:
                    remaining = (self.pause_until - datetime.utcnow()).total_seconds() / 3600
                    logger.info(f"⏸️ Trading paused, resume in {remaining:.1f}h")
                    return
                else:
                    logger.info("▶️ Trading pause ended")
                    self.pause_until = None

            # Check if there's already an open position
            if self.position_tracker.has_position():
                logger.debug("Position already open, skipping signal check")
                return

            # Get 4H data
            df_4h = self.data_manager.get_4h_data()
            if df_4h is None or len(df_4h) < 42:
                logger.warning("Insufficient data for signal generation")
                return

            # Get balance (use cached if available and requested)
            if use_cached_balance and self.cached_balance is not None:
                balance = self.cached_balance
                logger.debug(f"Using pre-fetched balance: ${balance:.2f}")
            else:
                balance_info = self.exchange.fetch_balance()
                if balance_info is None:
                    logger.error("Failed to get balance")
                    return
                balance = balance_info['free']
                logger.info(f"💰 Balance fetched: ${balance:.2f}")

            # Update adaptive leverage
            self.update_adaptive_leverage(balance, False)

            # Check circuit breaker
            if self.peak_capital and balance < self.peak_capital:
                current_dd = (self.peak_capital - balance) / self.peak_capital * 100
                if current_dd > self.dd_threshold_2:
                    logger.critical("🚨 CIRCUIT BREAKER ACTIVE - NOT TRADING")
                    return

            # Generate signal
            signal = self.signal_generator.generate_signal(df_4h)

            if signal is None:
                logger.debug("No signal generated")
                return

            # Execute order
            logger.info(f"\n{'='*60}")
            logger.info(f"🎯 Executing {signal['action']} order")
            logger.info(f"   Leverage: {self.current_leverage}x")
            logger.info(f"   Entry: ${signal['entry_price']:.2f}")
            logger.info(f"   TP: ${signal['tp_price']:.2f} ({signal['tp_pct']:.2f}%)")
            logger.info(f"   SL: ${signal['sl_price']:.2f} ({signal['sl_pct']:.2f}%)")
            logger.info(f"{'='*60}\n")

            # Calculate position size
            position_size_pct = self.config['position_size_pct']
            size_usdt = balance * position_size_pct

            # Place order
            order_result = self.exchange.place_market_order(
                side='Buy' if signal['action'] == 'LONG' else 'Sell',
                qty=None,  # Will calculate from size_usdt
                tp_price=signal['tp_price'],
                sl_price=signal['sl_price'],
                leverage=self.current_leverage,
                size_usdt=size_usdt
            )

            if order_result:
                # Get real executed price from exchange
                real_entry_price = order_result.get('price', signal['entry_price'])
                expected_entry = signal['entry_price']

                # Calculate slippage
                if signal['action'] == 'LONG':
                    slippage = real_entry_price - expected_entry
                    slippage_pct = (slippage / expected_entry) * 100
                else:  # SHORT
                    slippage = expected_entry - real_entry_price
                    slippage_pct = (slippage / expected_entry) * 100

                # Log slippage
                slippage_emoji = '⚠️' if abs(slippage_pct) > 0.1 else '✅'
                logger.info(
                    f"{slippage_emoji} Slippage: ${slippage:+.2f} ({slippage_pct:+.3f}%) | "
                    f"Expected: ${expected_entry:.2f} → Real: ${real_entry_price:.2f}"
                )

                # Create position with REAL entry price
                position = Position(
                    action=signal['action'],
                    entry_price=real_entry_price,  # Use real executed price!
                    tp_price=signal['tp_price'],
                    sl_price=signal['sl_price'],
                    size_usdt=size_usdt,
                    max_hold_hours=self.config['max_hold_hours'],
                    entry_time=datetime.utcnow()
                )

                self.position_tracker.open_position(position)

                # Send notification
                self.notifier.send(
                    f"✅ {signal['action']} ORDER OPENED\n"
                    f"Leverage: {self.current_leverage}x\n"
                    f"Entry: ${real_entry_price:.2f} (slippage: {slippage_pct:+.3f}%)\n"
                    f"Expected: ${expected_entry:.2f}\n"
                    f"TP: ${signal['tp_price']:.2f} (+{signal['tp_pct']:.2f}%)\n"
                    f"SL: ${signal['sl_price']:.2f} (-{signal['sl_pct']:.2f}%)\n"
                    f"Size: ${size_usdt:.2f}\n"
                    f"VWAP: ${signal['vwap']:.2f} ± ${signal['vwap_std']:.2f}\n"
                    f"Distance: {signal['distance_sigma']:.2f}σ"
                )

                logger.info("✅ Order placed successfully")

        except Exception as e:
            logger.error(f"❌ Error in signal check: {e}", exc_info=True)

    def check_position_exit(self):
        """
        Check if position should be exited based on TP/SL/Timeout
        """
        try:
            if not self.position_tracker.has_position():
                return

            position = self.position_tracker.get_open_positions()[0]

            # Get current price from WebSocket
            latest_ticker = self.ws_manager.get_latest_ticker()
            if latest_ticker is None:
                logger.warning("No ticker data from WebSocket")
                return

            current_price = float(latest_ticker['lastPrice'])

            # Check exit conditions
            exit_reason = position.check_exit(current_price)

            if exit_reason:
                logger.info(f"\n{'='*60}")
                logger.info(f"🚪 Closing position: {exit_reason}")
                logger.info(f"   Entry: ${position.entry_price:.2f}")
                logger.info(f"   Exit: ${current_price:.2f}")
                logger.info(f"{'='*60}\n")

                # Close position
                close_result = self.exchange.close_position(reduce_only=True)

                if close_result:
                    # Calculate PnL
                    pnl_info = position.calculate_pnl(current_price, self.current_leverage)

                    # Update streak counters
                    if exit_reason == 'SL':
                        self.consecutive_sl += 1
                        self.consecutive_wins = 0

                        # Trigger pause
                        if self.consecutive_sl >= self.pause_threshold:
                            self.pause_until = datetime.utcnow() + timedelta(hours=self.pause_hours)
                            logger.warning(f"⏸️ Trading paused until {self.pause_until} ({self.consecutive_sl} SL)")
                            self.notifier.send(
                                f"⚠️ TRADING PAUSED\n"
                                f"Consecutive SL: {self.consecutive_sl}\n"
                                f"Resume at: {self.pause_until.strftime('%Y-%m-%d %H:%M')}"
                            )
                    else:
                        self.consecutive_sl = 0
                        if pnl_info['pnl'] > 0:
                            self.consecutive_wins += 1

                    # Log trade
                    trade_data = {
                        'timestamp': datetime.utcnow().isoformat(),
                        'action': position.action,
                        'entry_price': position.entry_price,
                        'exit_price': current_price,
                        'tp_price': position.tp_price,
                        'sl_price': position.sl_price,
                        'size_usdt': position.size_usdt,
                        'leverage': self.current_leverage,
                        'exit_reason': exit_reason,
                        'pnl': pnl_info['pnl'],
                        'pnl_pct': pnl_info['pnl_pct'],
                        'consecutive_sl': self.consecutive_sl,
                        'consecutive_wins': self.consecutive_wins
                    }

                    self.trade_logger.log_trade(trade_data)

                    # Clear position
                    self.position_tracker.close_position(position.action, exit_price, exit_reason, self.current_leverage)

                    # Fetch updated balance after position close
                    balance_info = self.exchange.fetch_balance()
                    new_balance = balance_info['free'] if balance_info else 0
                    self.cached_balance = new_balance  # Update cache

                    # Send notification
                    emoji = '✅' if pnl_info['pnl'] > 0 else '❌'
                    self.notifier.send(
                        f"{emoji} POSITION CLOSED: {exit_reason}\n"
                        f"Entry: ${position.entry_price:.2f}\n"
                        f"Exit: ${current_price:.2f}\n"
                        f"PnL: ${pnl_info['pnl']:.2f} ({pnl_info['pnl_pct']:.2f}%)\n"
                        f"Leverage: {self.current_leverage}x\n"
                        f"Balance: ${new_balance:.2f}\n"
                        f"Win Streak: {self.consecutive_wins} | SL Streak: {self.consecutive_sl}"
                    )

                    logger.info(f"✅ Position closed, PnL: ${pnl_info['pnl']:.2f}, Balance: ${new_balance:.2f}")

        except Exception as e:
            logger.error(f"❌ Error checking position exit: {e}", exc_info=True)

    def run(self):
        """
        Main bot loop
        """
        logger.info("\n" + "="*80)
        logger.info("🚀 Starting VWAP bot main loop")
        logger.info("="*80 + "\n")

        # Initial data download
        logger.info("📥 Updating historical data...")
        if not self.data_manager.update_historical_data(self.exchange):
            logger.error("❌ Failed to download initial data")
            return

        # Start WebSocket
        logger.info("🌐 Starting WebSocket connection...")
        self.ws_manager.start()
        time.sleep(3)  # Wait for WebSocket to connect

        # Send startup notification
        self.notifier.send(
            "🤖 VWAP Bot Started\n"
            f"Strategy: VWAP ±2σ Mean Reversion v3\n"
            f"Leverage: {self.base_leverage}x (adaptive)\n"
            f"Mode: {'Demo' if os.getenv('BYBIT_DEMO_MODE', 'true').lower() == 'true' else 'LIVE'}"
        )

        # Intervals for main loop
        data_update_interval = 900  # 15 minutes - keep data fresh
        position_check_interval = 30  # 30 seconds - check position timeout

        last_data_update = 0
        last_position_check = 0

        # Signal checking is done via WebSocket callback (on 4H candle close)
        # Balance is pre-fetched 3 minutes before candle close

        try:
            while True:
                current_time = time.time()
                now = datetime.utcnow()

                # Pre-fetch balance ~3 minutes before 4H candle close
                # 4H candles close at 00:00, 04:00, 08:00, 12:00, 16:00, 20:00 UTC
                minutes = now.minute
                hours = now.hour
                is_near_4h_close = (hours % 4 == 3) and (minutes >= 57)  # XX:57-XX:59

                if is_near_4h_close and not self.balance_prefetched:
                    if not self.position_tracker.has_position():
                        self._prefetch_balance()

                # Update historical data periodically (backup, main updates via WebSocket)
                if current_time - last_data_update >= data_update_interval:
                    logger.debug("📊 Periodic data update...")
                    self.data_manager.update_historical_data(self.exchange)
                    last_data_update = current_time

                # Check position exit timeout (WebSocket handles TP/SL, this handles max_hold)
                if current_time - last_position_check >= position_check_interval:
                    if self.position_tracker.has_position():
                        self.check_position_exit()
                    last_position_check = current_time

                time.sleep(1)

        except KeyboardInterrupt:
            logger.info("\n⚠️ Shutdown requested by user")
        except Exception as e:
            logger.error(f"❌ Fatal error in main loop: {e}", exc_info=True)
        finally:
            logger.info("🛑 Stopping bot...")
            self.ws_manager.stop()
            self.notifier.send("🛑 VWAP Bot Stopped")
            logger.info("👋 Bot stopped")


if __name__ == "__main__":
    bot = TradingBotVWAP()
    bot.run()
