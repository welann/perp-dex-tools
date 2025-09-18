"""
Lighter exchange client implementation.
"""

import os
import asyncio
import requests
from decimal import Decimal
from typing import Dict, Any, List, Optional,Tuple

try:
    import lighter
except ImportError:
    lighter = None

from .base import BaseExchangeClient, OrderResult, OrderInfo, query_retry
from helpers.logger import TradingLogger

def trim_exception(e: Exception) -> str:
    return str(e).strip().split("\n")[-1]

class LighterClient(BaseExchangeClient):
    """Lighter exchange client implementation."""

    def __init__(self, config: Dict[str, Any]):
        """Initialize Lighter client."""
        super().__init__(config)

        if lighter is None:
            raise ImportError("lighter SDK is not installed. Please install with: uv add git+https://github.com/elliottech/lighter-python.git")

        # Lighter credentials from environment
        self.private_key = os.getenv('LIGHTER_PRIVATE_KEY')
        self.account_index = int(os.getenv('LIGHTER_ACCOUNT_INDEX', '1'))
        self.api_key_index = int(os.getenv('LIGHTER_API_KEY_INDEX', '3'))
        self.base_url = os.getenv('LIGHTER_BASE_URL', 'https://testnet.zklighter.elliot.ai')
        self.is_testnet = self.base_url.find('testnet') != -1

        if not self.private_key:
            raise ValueError("LIGHTER_PRIVATE_KEY must be set in environment variables")

        # Initialize Lighter client using official SDK
        self.signer_client = lighter.SignerClient(
            url=self.base_url,
            private_key=self.private_key,
            account_index=self.account_index,
            api_key_index=self.api_key_index
        )


        # Initialize API client for read operations
        self.api_client = lighter.ApiClient(configuration=lighter.Configuration(host=self.base_url))
        self.order_api = lighter.OrderApi(self.api_client)
        self.account_api = lighter.AccountApi(self.api_client)

        # Initialize logger using the same format as helpers
        ticker = getattr(config, 'ticker', 'UNKNOWN')
        self.logger = TradingLogger(exchange="lighter", ticker=ticker, log_to_console=False)

        self._order_update_handler = None
        self.ws_client = None

        # Cache market index for contract
        self._market_indices = {}
        # Cache market details
        self._market_details = {}
        self._markets_loaded = False

    def check_client(self)-> bool:
        """Check if Lighter client is properly initialized."""
        err = self.signer_client.check_client()
        if err is not None:
            print(f"CheckClient error: {trim_exception(err)}")
            return False
        return True

    def _validate_config(self) -> None:
        """Validate Lighter configuration."""
        required_env_vars = ['LIGHTER_PRIVATE_KEY', 'LIGHTER_ACCOUNT_INDEX', 'LIGHTER_API_KEY_INDEX']
        missing_vars = [var for var in required_env_vars if not os.getenv(var)]
        if missing_vars:
            raise ValueError(f"Missing required environment variables: {missing_vars}")
        async def connect(self,orderbook_id:int) -> None:
        """Connect to Lighter WebSocket."""
        try:
            if self.ws_client is None and self._order_update_handler:
                self.ws_client = lighter.WsClient(
                    order_book_ids=[orderbook_id],
                    account_ids=[str(self.account_index)],
                    on_account_update=self._handle_account_update
                )
            # Start WebSocket connection asynchronously if handler is set
            if self.ws_client:
                asyncio.create_task(self.ws_client.run_async())
                await asyncio.sleep(2)  # Wait for connection to establish
        except Exception as e:
            self.logger.log(f"Error connecting to Lighter WebSocket: {e}", "ERROR")

    async def disconnect(self) -> None:
        """Disconnect from Lighter."""
        try:
            if self.ws_client:
                # Close WebSocket connection
                await self.ws_client.close()
                self.ws_client = None
        except Exception as e:
            self.logger.log(f"Error during Lighter disconnect: {e}", "ERROR")

    def get_exchange_name(self) -> str:
        """Get the exchange name."""
        return "lighter"

    def setup_order_update_handler(self, handler) -> None:
        """Setup order update handler for WebSocket."""
        self._order_update_handler = handler

    def _handle_account_update(self, account_data: dict):
        """Handle account update from WebSocket."""
        if self._order_update_handler:
            try:
                # Convert Lighter account update to standardized format
                self._order_update_handler(account_data)
            except Exception as e:
                self.logger.log(f"Error handling account update: {e}", "ERROR")


# ===== Load Lighter Orderbook Details =====
    def _load_market_details(self) -> None:
        """Load market details from Lighter API."""
        if self._markets_loaded:
            return

        try:
            # Use mainnet URL for market details as it has more complete data
            api_url = "https://mainnet.zklighter.elliot.ai/api/v1/orderBookDetails"

            response = requests.get(api_url, timeout=10)
            response.raise_for_status()

            data = response.json()

            if data.get("code") == 200 and "order_book_details" in data:
                for market in data["order_book_details"]:
                    symbol = market.get("symbol")
                    if symbol:
                        self._market_details[symbol] = {
                            "size_decimals": market.get("size_decimals"),
                            "price_decimals": market.get("price_decimals"),
                        }
                        # Also cache the market_id mapping
                        self._market_indices[symbol] = market.get("market_id")

                self._markets_loaded = True
            else:
                return
        except requests.RequestException as e:
            self.logger.log(f"Error fetching market details from Lighter API: {e}", "ERROR")
        except Exception as e:
            self.logger.log(f"Unexpected error loading market details: {e}", "ERROR")

    def symbol_to_orderbook_id(self, symbol: str) -> int:
        """Convert symbol to Lighter orderbook ID."""
        # Load market details if not already loaded
        self._load_market_details()

        # Check if symbol exists in our cache
        if symbol in self._market_indices:
            return self._market_indices[symbol]

        # If not found, try to reload market details once more
        if self._markets_loaded:
            self._markets_loaded = False
            self._load_market_details()

        if symbol in self._market_indices:
            return self._market_indices[symbol]
        else:
            self.logger.log(f"Symbol {symbol} not found in Lighter market details", "ERROR")
            raise ValueError(f"Symbol {symbol} not found in Lighter market details")
    
    def get_market_details(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get market details for a symbol."""
        self._load_market_details()
        return self._market_details.get(symbol)

    def _get_symbol_from_market_id(self, market_id: int) -> Optional[str]:
        """Get symbol from market ID."""
        self._load_market_details()
        for symbol, details in self._market_indices.items():
            if details == market_id:
                return symbol
        return None

    def _convert_from_lighter_amount_with_market_id(self, amount: str, market_id: int) -> Decimal:
        """Convert Lighter amount using market_id."""
        symbol = self._get_symbol_from_market_id(market_id)
        if symbol:
            return self._convert_from_lighter_amount(amount, symbol)
        # Fallback to default precision
        return Decimal(amount) / 10000

    def _convert_from_lighter_price_with_market_id(self, price: str, market_id: int) -> Decimal:
        """Convert Lighter price using market_id."""
        symbol = self._get_symbol_from_market_id(market_id)
        if symbol:
            return self._convert_from_lighter_price(price, symbol)
        # Fallback to default precision
        return Decimal(price) / 100
    
    def _convert_amount_to_base(self, amount: Decimal, symbol: str) -> int:
        """Convert decimal amount to base units for Lighter using market details."""
        market_details = self.get_market_details(symbol)
        if market_details and market_details.get("size_decimals") is not None:
            decimals = market_details["size_decimals"]
            return int(amount * (10 ** decimals))
        else:
            raise ValueError(f"Market details not found for symbol: {symbol}")

    def _convert_price_to_lighter(self, price: Decimal, symbol: str) -> int:
        """Convert decimal price to Lighter price units using market details."""
        market_details = self.get_market_details(symbol)
        if market_details and market_details.get("price_decimals") is not None:
            decimals = market_details["price_decimals"]
            return int(price * (10 ** decimals))

        else:
            raise ValueError(f"Market details not found for symbol: {symbol}")

    def _convert_from_lighter_amount(self, amount: str, symbol: str) -> Decimal:
        """Convert Lighter amount string to Decimal using market details."""
        market_details = self.get_market_details(symbol)
        if market_details and market_details.get("size_decimals") is not None:
            decimals = market_details["size_decimals"]
            return Decimal(amount) * (10 ** decimals)
        else:
            raise ValueError(f"Market details not found for symbol: {symbol}")
        

    def _convert_from_lighter_price(self, price: str, symbol: str) -> Decimal:
        """Convert Lighter price string to Decimal using market details."""
        market_details = self.get_market_details(symbol)
        if market_details and market_details.get("price_decimals") is not None:
            decimals = market_details["price_decimals"]
            return Decimal(price) * (10 ** decimals)

        else:
            raise ValueError(f"Market details not found for symbol: {symbol}")
    
#================== 

    @query_retry(default_return=(0, 0))
    async def fetch_bbo_prices(self, contract_id: str) -> Tuple[Decimal, Decimal]:
        depth_params = GetOrderBookDepthParams(contract_id=contract_id, limit=15)
        order_book = await self.client.quote.get_order_book_depth(depth_params)
        order_book_data = order_book['data']

        # Get the first (and should be only) order book entry
        order_book_entry = order_book_data[0]

        # Extract bids and asks from the entry
        bids = order_book_entry.get('bids', [])
        asks = order_book_entry.get('asks', [])

        # Best bid is the highest price someone is willing to buy at
        best_bid = Decimal(bids[0]['price']) if bids and len(bids) > 0 else 0
        # Best ask is the lowest price someone is willing to sell at
        best_ask = Decimal(asks[0]['price']) if asks and len(asks) > 0 else 0
        return best_bid, best_ask
    
    @query_retry(default_return=OrderResult(success=False, error_message="Query failed"))
    async def place_open_order(self, contract_id: str, quantity: Decimal, direction: str) -> OrderResult:
        """Place an open order on Lighter."""
        try:
            market_index = self.symbol_to_orderbook_id(contract_id)
            amount_base = self._convert_amount_to_base(quantity, contract_id)
            is_ask = direction.lower() == 'sell'

            # Place limit order for opening position
            _, tx_hash, err = self.signer_client.create_order(
                market_index=market_index,
                client_order_index=123,
                base_amount=amount_base,
                price=405000,
                is_ask=is_ask,
                order_type=lighter.SignerClient.ORDER_TYPE_LIMIT,
                time_in_force=lighter.SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                reduce_only=0,
                trigger_price=0,
    )
                
            if err:
                return OrderResult(success=False, error_message=str(err))

            return OrderResult(
                success=True,
                order_id=tx_hash,
                side=direction,
                size=quantity,
                status='pending'
            )

        except Exception as e:
            self.logger.log(f"Error placing open order: {e}", "ERROR")
            return OrderResult(success=False, error_message=str(e))

    @query_retry(default_return=OrderResult(success=False, error_message="Query failed"))
    async def place_close_order(self, contract_id: str, quantity: Decimal, price: Decimal, side: str) -> OrderResult:
        """Place a close order on Lighter."""
        try:
            market_index = self.symbol_to_orderbook_id(contract_id)
            amount_base = self._convert_amount_to_base(quantity, contract_id)
            lighter_price = self._convert_price_to_lighter(price, contract_id)
            is_ask = side.lower() == 'sell'

            # Place limit order for closing position
            _, tx_hash, err = await self.signer_client.create_order(
                market_index=market_index,
                amount_base=amount_base,
                price=lighter_price,
                is_ask=is_ask,
                order_type="limit"
            )

            if err:
                return OrderResult(success=False, error_message=str(err))

            return OrderResult(
                success=True,
                order_id=tx_hash,
                side=side,
                size=quantity,
                price=price,
                status='pending'
            )

        except Exception as e:
            self.logger.log(f"Error placing close order: {e}", "ERROR")
            return OrderResult(success=False, error_message=str(e))

    @query_retry(default_return=OrderResult(success=False, error_message="Query failed"))
    async def cancel_order(self, order_id: str) -> OrderResult:
        """Cancel an order on Lighter."""
        try:
            # Note: Lighter cancellation requires market_index and order_index
            # For now, this is a simplified implementation
            # In production, you'd need to track the mapping between order_id and these parameters

            # This is a placeholder - actual implementation would need order tracking
            self.logger.log(f"Cancel order not fully implemented for order_id: {order_id}", "WARNING")

            return OrderResult(
                success=False,
                error_message="Cancel order requires market_index and order_index - not implemented"
            )

        except Exception as e:
            self.logger.log(f"Error canceling order: {e}", "ERROR")
            return OrderResult(success=False, error_message=str(e))

    @query_retry(default_return=None)
    async def get_order_info(self, order_id: str) -> Optional[OrderInfo]:
        """Get order information from Lighter."""
        try:
            # Get active orders and search for the specific order
            active_orders = await self.order_api.account_active_orders(
                account_index=self.account_index
            )

            for order in active_orders:
                if str(order.order_id) == order_id:
                    # Try to get market_index from order, fallback to using default conversion
                    market_id = getattr(order, 'market_index', None) or getattr(order, 'market_id', None)
                    if market_id is not None:
                        size = self._convert_from_lighter_amount_with_market_id(order.amount_base, market_id)
                        price = self._convert_from_lighter_price_with_market_id(order.price, market_id)
                        remaining_size = self._convert_from_lighter_amount_with_market_id(order.amount_base, market_id)
                    else:
                        # Fallback to default conversion
                        size = Decimal(order.amount_base) / 10000
                        price = Decimal(order.price) / 100
                        remaining_size = Decimal(order.amount_base) / 10000

                    return OrderInfo(
                        order_id=str(order.order_id),
                        side='sell' if order.is_ask else 'buy',
                        size=size,
                        price=price,
                        status=order.status,
                        filled_size=Decimal('0'),  # Would need to calculate from order data
                        remaining_size=remaining_size
                    )

            return None

        except Exception as e:
            self.logger.log(f"Error getting order info: {e}", "ERROR")
            return None

    @query_retry(default_return=[])
    async def get_active_orders(self, contract_id: str) -> List[OrderInfo]:
        """Get active orders for a contract on Lighter."""
        try:
            market_index = self.symbol_to_orderbook_id(contract_id)

            # Get active orders for the account
            active_orders = await self.order_api.account_active_orders(
                account_index=self.account_index,
                market_index=market_index
            )

            order_list = []
            for order in active_orders:
                # Since we're filtering by market_index, we can use contract_id for conversion
                size = self._convert_from_lighter_amount(order.amount_base, contract_id)
                price = self._convert_from_lighter_price(order.price, contract_id)
                remaining_size = self._convert_from_lighter_amount(order.amount_base, contract_id)

                order_info = OrderInfo(
                    order_id=str(order.order_id),
                    side='sell' if order.is_ask else 'buy',
                    size=size,
                    price=price,
                    status=order.status,
                    filled_size=Decimal('0'),  # Would need to calculate from order data
                    remaining_size=remaining_size
                )
                order_list.append(order_info)

            return order_list

        except Exception as e:
            self.logger.log(f"Error getting active orders: {e}", "ERROR")
            return []

    @query_retry(default_return=Decimal('0'))
    async def get_account_positions(self) -> Decimal:
        """Get account positions from Lighter."""
        try:
            # Get account position information
            positions = await self.account_api.account_position(
                account_index=self.account_index
            )

            total_position_value = Decimal('0')

            if positions:
                for position in positions:
                    position_value = Decimal(position.position_value) if position.position_value else Decimal('0')
                    total_position_value += position_value

            return total_position_value

        except Exception as e:
            self.logger.log(f"Error getting account positions: {e}", "ERROR")
            return Decimal('0')
        
        
        
if __name__ == "__main__":
    pass