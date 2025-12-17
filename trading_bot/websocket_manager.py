"""
WebSocket Manager for Bybit
Handles real-time position updates via WebSocket
"""

import os
import logging
import threading
from typing import Dict, Callable, Optional
from pybit.unified_trading import WebSocket

logger = logging.getLogger(__name__)


class BybitWebSocketManager:
    """
    Manages WebSocket connection to Bybit for real-time position updates
    """

    def __init__(
        self,
        symbol: str,
        demo: bool = True,
        on_position_update: Optional[Callable] = None,
        on_kline_update: Optional[Callable] = None,
        on_execution: Optional[Callable] = None
    ):
        """
        Initialize WebSocket manager

        Args:
            symbol: Trading symbol (e.g., 'BTCUSDT')
            demo: Use demo trading mode (True) or real trading (False)
            on_position_update: Callback function for position updates
            on_kline_update: Callback function for 4H kline updates
            on_execution: Callback function for execution (trade) updates
        """
        self.symbol = symbol
        self.demo = demo
        self.on_position_update = on_position_update
        self.on_kline_update = on_kline_update
        self.on_execution = on_execution
        self.ws = None  # Private WebSocket for positions
        self.ws_public = None  # Public WebSocket for 4H klines
        self.running = False

        # Order confirmation mechanism
        self._pending_order_event = threading.Event()
        self._pending_order_side = None  # 'Buy' or 'Sell'
        self._confirmed_position = None  # Position data from WebSocket

        # Cache last known position side (Bybit returns empty side when position is closed)
        self._last_known_side = None

        # Latest ticker data
        self._latest_ticker = None

        # Get API keys from environment (different keys for demo vs live)
        if demo:
            api_key = os.getenv('BYBIT_DEMO_API_KEY')
            api_secret = os.getenv('BYBIT_DEMO_SECRET')
            if not api_key or not api_secret:
                raise ValueError(f"Missing Bybit Demo API credentials. Set BYBIT_DEMO_API_KEY and BYBIT_DEMO_SECRET in .env")
        else:
            api_key = os.getenv('BYBIT_LIVE_API_KEY')
            api_secret = os.getenv('BYBIT_LIVE_SECRET')
            if not api_key or not api_secret:
                raise ValueError(f"Missing Bybit Live API credentials. Set BYBIT_LIVE_API_KEY and BYBIT_LIVE_SECRET in .env")

        self.api_key = api_key
        self.api_secret = api_secret

        mode = 'Demo Trading (virtual money)' if demo else 'LIVE Trading (REAL MONEY!)'
        logger.info(f"WebSocket manager initialized ({mode})")

    def start(self):
        """Start WebSocket connection"""
        if self.running:
            logger.warning("WebSocket already running")
            return

        try:
            # Initialize WebSocket with private channel (always mainnet)
            self.ws = WebSocket(
                testnet=False,  # Always mainnet
                channel_type="private",
                api_key=self.api_key,
                api_secret=self.api_secret,
                demo=self.demo,
                ping_interval=20,
                ping_timeout=10,
                retries=30
            )

            # Subscribe to position updates
            self.ws.position_stream(
                callback=self._handle_position_update
            )

            # Subscribe to execution updates (for closed PnL)
            self.ws.execution_stream(
                callback=self._handle_execution_update
            )

            logger.info("Private WebSocket connected - listening for position and execution updates")

            # Initialize PUBLIC WebSocket for 4H kline updates
            if self.on_kline_update:
                self.ws_public = WebSocket(
                    testnet=False,
                    channel_type="linear",  # Public channel for klines
                    ping_interval=20,
                    ping_timeout=10,
                    retries=30
                )

                # Subscribe to 4H kline (interval: 240 = 4 hours)
                self.ws_public.kline_stream(
                    interval=240,
                    symbol=self.symbol,
                    callback=self._handle_kline_update
                )

                logger.info(f"Public WebSocket connected - listening for {self.symbol} 4H klines")

            self.running = True

        except Exception as e:
            logger.error(f"Failed to start WebSocket: {e}")
            self.running = False
            raise

    def stop(self):
        """Stop WebSocket connection"""
        if not self.running:
            return

        try:
            if self.ws:
                self.running = False
                logger.info("WebSocket disconnected")
        except Exception as e:
            logger.error(f"Error stopping WebSocket: {e}")

    def _handle_position_update(self, message: Dict):
        """
        Handle position update from WebSocket

        Args:
            message: Position update message from Bybit
        """
        if not self.running:
            return

        try:
            logger.debug(f"Position update: {message}")

            if 'data' in message:
                for position_data in message['data']:
                    self._process_position_data(position_data)

        except Exception as e:
            logger.error(f"Error handling position update: {e}")

    def _process_position_data(self, position_data: Dict):
        """
        Process individual position data

        Args:
            position_data: Position data from WebSocket message
        """
        try:
            symbol = position_data.get('symbol')
            side = position_data.get('side')

            def safe_float(value, default=0.0):
                if value == '' or value is None:
                    return default
                return float(value)

            size = safe_float(position_data.get('size'))
            position_value = safe_float(position_data.get('positionValue'))
            unrealized_pnl = safe_float(position_data.get('unrealisedPnl'))
            realised_pnl = safe_float(position_data.get('curRealisedPnl'))
            cum_realised_pnl = safe_float(position_data.get('cumRealisedPnl'))
            leverage = safe_float(position_data.get('leverage'))
            entry_price = safe_float(position_data.get('entryPrice')) or safe_float(position_data.get('avgPrice'))
            mark_price = safe_float(position_data.get('markPrice'))
            liq_price = safe_float(position_data.get('liqPrice'))
            take_profit = safe_float(position_data.get('takeProfit'))
            stop_loss = safe_float(position_data.get('stopLoss'))

            # Cache side when position is open
            if side and size > 0:
                self._last_known_side = side

            # Check if position was closed (size = 0)
            if size == 0:
                if not side and self._last_known_side:
                    side = self._last_known_side
                    logger.debug(f"Using cached side: {side}")

                final_pnl = realised_pnl if realised_pnl != 0 else unrealized_pnl

                logger.info(
                    f"Position CLOSED: {symbol} {side} | "
                    f"Realised PnL: ${realised_pnl:+.2f} | "
                    f"Entry: ${entry_price:.2f}"
                )

                if self.on_position_update:
                    self.on_position_update({
                        'event': 'closed',
                        'symbol': symbol,
                        'side': side,
                        'pnl': final_pnl,
                        'realised_pnl': realised_pnl,
                        'entry_price': entry_price,
                        'take_profit': take_profit,
                        'stop_loss': stop_loss
                    })

                self._last_known_side = None

            else:
                logger.debug(
                    f"Position UPDATE: {symbol} {side} | "
                    f"Size: {size:.6f} | "
                    f"Entry: ${entry_price:.2f} | "
                    f"Mark: ${mark_price:.2f} | "
                    f"PnL: ${unrealized_pnl:+.2f}"
                )

                position_data = {
                    'event': 'update',
                    'symbol': symbol,
                    'side': side,
                    'size': size,
                    'entry_price': entry_price,
                    'mark_price': mark_price,
                    'pnl': unrealized_pnl,
                    'leverage': leverage,
                    'liq_price': liq_price,
                    'take_profit': take_profit,
                    'stop_loss': stop_loss
                }

                self._check_pending_order(side, position_data)

                if self.on_position_update:
                    self.on_position_update(position_data)

        except Exception as e:
            logger.error(f"Error processing position data: {e}")

    def is_connected(self) -> bool:
        """Check if WebSocket is connected and running"""
        return self.running and self.ws is not None

    def prepare_for_position(self, side: str):
        """
        Prepare to receive position confirmation.
        Call this BEFORE placing the order to avoid race conditions.

        Args:
            side: 'Buy' or 'Sell'
        """
        self._pending_order_event.clear()
        self._pending_order_side = side
        self._confirmed_position = None
        logger.debug(f"Prepared to receive {side} position confirmation")

    def wait_for_position_open(self, side: str, timeout: float = 10.0) -> Optional[Dict]:
        """
        Wait for position to be opened (confirmed via WebSocket)

        Args:
            side: 'Buy' or 'Sell'
            timeout: Max seconds to wait

        Returns:
            Position data dict or None if timeout
        """
        if self._pending_order_side != side:
            logger.warning(f"prepare_for_position() was not called, setting up now")
            self._pending_order_event.clear()
            self._pending_order_side = side
            self._confirmed_position = None

        logger.info(f"Waiting for WebSocket confirmation of {side} position...")

        confirmed = self._pending_order_event.wait(timeout=timeout)

        self._pending_order_side = None

        if confirmed and self._confirmed_position:
            logger.info(f"Position confirmed via WebSocket: {self._confirmed_position}")
            return self._confirmed_position
        else:
            logger.warning(f"WebSocket confirmation timeout after {timeout}s")
            return None

    def _check_pending_order(self, side: str, position_data: Dict):
        """Check if this position update matches a pending order"""
        if self._pending_order_side and side == self._pending_order_side:
            self._confirmed_position = position_data
            self._pending_order_event.set()
            logger.info(f"Pending order confirmed: {side}")

    def _handle_execution_update(self, message: Dict):
        """
        Handle execution (trade) update from WebSocket

        Execution stream provides:
        - execPrice: Execution price
        - execQty: Execution quantity
        - closedPnl: Realized PnL when closing position
        - side: Buy or Sell
        - execType: Trade, Funding, etc.

        Args:
            message: Execution update message from Bybit
        """
        if not self.running:
            return

        try:
            logger.debug(f"Execution update: {message}")

            if 'data' not in message:
                return

            for exec_data in message['data']:
                symbol = exec_data.get('symbol')
                if symbol != self.symbol:
                    continue

                exec_type = exec_data.get('execType', '')

                # Only process Trade executions (not Funding, etc.)
                if exec_type != 'Trade':
                    continue

                def safe_float(value, default=0.0):
                    if value == '' or value is None:
                        return default
                    return float(value)

                side = exec_data.get('side', '')
                exec_price = safe_float(exec_data.get('execPrice'))
                exec_qty = safe_float(exec_data.get('execQty'))
                exec_pnl = safe_float(exec_data.get('execPnl'))  # PnL for close execution
                exec_fee = safe_float(exec_data.get('execFee'))  # Trading fee (negative = paid)
                closed_size = safe_float(exec_data.get('closedSize'))  # Closed position size
                order_type = exec_data.get('orderType', '')
                is_maker = exec_data.get('isMaker', False)

                # execPnl != 0 or closedSize > 0 means position was closed (partially or fully)
                if exec_pnl != 0 or closed_size > 0:
                    logger.info(
                        f"Execution CLOSE: {symbol} {side} | "
                        f"Price: ${exec_price:.2f} | "
                        f"Qty: {exec_qty:.6f} | "
                        f"PnL: ${exec_pnl:+.2f} | "
                        f"Fee: ${exec_fee:.4f} | "
                        f"Order: {order_type}"
                    )

                    if self.on_execution:
                        self.on_execution({
                            'event': 'close',
                            'symbol': symbol,
                            'side': side,
                            'exec_price': exec_price,
                            'exec_qty': exec_qty,
                            'closed_pnl': exec_pnl,  # Using execPnl from API
                            'exec_fee': exec_fee,
                            'order_type': order_type,
                            'is_maker': is_maker
                        })
                else:
                    logger.debug(
                        f"Execution OPEN: {symbol} {side} | "
                        f"Price: ${exec_price:.2f} | "
                        f"Qty: {exec_qty:.6f}"
                    )

        except Exception as e:
            logger.error(f"Error handling execution update: {e}")

    def _handle_kline_update(self, message: Dict):
        """Handle 4H kline update from WebSocket"""
        if not self.running:
            return

        try:
            logger.debug(f"Kline update: {message}")

            if 'data' in message:
                for kline_data in message['data']:
                    self._process_kline_data(kline_data)

        except Exception as e:
            logger.error(f"Error handling kline update: {e}")

    def _process_kline_data(self, kline_data: Dict):
        """Process individual kline (candle) data"""
        try:
            symbol = kline_data.get('symbol')
            interval = kline_data.get('interval')
            confirm = kline_data.get('confirm')

            start_time = int(kline_data.get('start'))
            end_time = int(kline_data.get('end'))
            open_price = float(kline_data.get('open'))
            high_price = float(kline_data.get('high'))
            low_price = float(kline_data.get('low'))
            close_price = float(kline_data.get('close'))
            volume = float(kline_data.get('volume'))

            # Update latest ticker
            self._latest_ticker = {
                'lastPrice': close_price
            }

            candle = {
                'symbol': symbol,
                'interval': interval,
                'timestamp': end_time,
                'open': open_price,
                'high': high_price,
                'low': low_price,
                'close': close_price,
                'volume': volume,
                'confirm': confirm
            }

            # Only call callback when candle is CLOSED
            if confirm:
                logger.info(
                    f"4H CANDLE CLOSED: {symbol} | "
                    f"Close: ${close_price:.2f} | Volume: {volume:.0f}"
                )

                if self.on_kline_update:
                    self.on_kline_update(candle)
            else:
                logger.debug(f"4H candle updating (not closed yet): ${close_price:.2f}")

        except Exception as e:
            logger.error(f"Error processing kline data: {e}")

    def get_latest_ticker(self) -> Optional[Dict]:
        """Get latest ticker data from WebSocket"""
        return self._latest_ticker


if __name__ == "__main__":
    # Test WebSocket
    logging.basicConfig(level=logging.INFO)

    def handle_update(data: Dict):
        """Test callback"""
        print(f"Callback received: {data}")

    try:
        ws_manager = BybitWebSocketManager(
            symbol='BTCUSDT',
            demo=True,
            on_position_update=handle_update
        )
        ws_manager.start()

        import time
        print("WebSocket running... Press Ctrl+C to stop")
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        print("\nStopping...")
        ws_manager.stop()
    except Exception as e:
        print(f"Error: {e}")
