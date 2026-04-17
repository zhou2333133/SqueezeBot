import time
import hmac
import hashlib
from urllib.parse import urlencode
import aiohttp
import logging
from config import BINANCE_API_KEY, BINANCE_API_SECRET

logger = logging.getLogger(__name__)

class BinanceTrader:
    def __init__(self, session: aiohttp.ClientSession):
        self.base_url = "https://fapi.binance.com"
        self.api_key = BINANCE_API_KEY
        self.api_secret = BINANCE_API_SECRET
        self.session = session

    def _sign(self, params: dict) -> str:
        """生成API签名"""
        query_string = urlencode(params)
        return hmac.new(self.api_secret.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

    async def _send_signed_request(self, method: str, endpoint: str, params: dict):
        """发送签名请求"""
        if not self.api_key or self.api_key == "YOUR_BINANCE_API_KEY":
            logger.warning("交易功能未开启：API Key 未配置。")
            return None
            
        params['timestamp'] = int(time.time() * 1000)
        params['signature'] = self._sign(params)
        headers = {'X-MBX-APIKEY': self.api_key}
        
        try:
            async with self.session.request(method, f"{self.base_url}{endpoint}", headers=headers, params=params) as resp:
                if resp.status >= 400:
                    logger.error(f"API 错误: {resp.status}, 响应: {await resp.text()}")
                    return None
                return await resp.json()
        except Exception as e:
            logger.error(f"发送签名请求时发生网络错误: {e}")
            return None

    async def place_market_order(self, symbol: str, side: str, quantity: float):
        """
        下市价单
        :param symbol: 交易对, e.g., 'BTCUSDT'
        :param side: 'BUY' or 'SELL'
        :param quantity: 数量
        """
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": f"{quantity:.8f}".rstrip('0').rstrip('.'), # 格式化数量
        }
        logger.info(f"准备下市价单: {params}")
        response = await self._send_signed_request('POST', '/fapi/v1/order', params)
        if response and response.get('orderId'):
            logger.info(f"✅ 市价单下单成功: {symbol} {side} {quantity}, OrderID: {response['orderId']}")
            return response
        else:
            logger.error(f"❌ 市价单下单失败: {response}")
            return None

    async def place_stop_loss_order(self, symbol: str, side: str, stop_price: float):
        """
        下止损单 (平仓市价止损)
        :param symbol: 交易对
        :param side: 'BUY' (用于空头止损) or 'SELL' (用于多头止损)
        :param stop_price: 触发价格
        """
        params = {
            "symbol": symbol,
            "side": side,
            "type": "STOP_MARKET",
            "stopPrice": f"{stop_price:.8f}".rstrip('0').rstrip('.'),
            "closePosition": "true", # 确保是平仓单
            "timeInForce": "GTC",
        }
        logger.info(f"准备下止损单: {params}")
        response = await self._send_signed_request('POST', '/fapi/v1/order', params)
        if response and response.get('orderId'):
            logger.info(f"✅ 止损单下单成功: {symbol} {side} 触发价 {stop_price}, OrderID: {response['orderId']}")
            return response
        else:
            logger.error(f"❌ 止损单下单失败: {response}")
            return None

    async def place_trailing_stop_order(self, symbol: str, side: str, activation_price: float, callback_rate: float, quantity: float):
        """
        下追踪止损单
        :param symbol: 交易对
        :param side: 'SELL' (用于多头止盈)
        :param activation_price: 激活价格
        :param callback_rate: 回调比例 (e.g., 1.5 for 1.5%)
        :param quantity: 数量
        """
        params = {
            "symbol": symbol,
            "side": side,
            "type": "TRAILING_STOP_MARKET",
            "activationPrice": f"{activation_price:.8f}".rstrip('0').rstrip('.'),
            "callbackRate": f"{callback_rate:.1f}",
            "quantity": f"{quantity:.8f}".rstrip('0').rstrip('.'),
        }
        logger.info(f"准备下追踪止损单: {params}")
        response = await self._send_signed_request('POST', '/fapi/v1/order', params)
        if response and response.get('orderId'):
            logger.info(f"✅ 追踪止损单下单成功: {symbol} {side} 激活价 {activation_price}, 回调率 {callback_rate}%, OrderID: {response['orderId']}")
            return response
        else:
            logger.error(f"❌ 追踪止损单下单失败: {response}")
            return None

    async def place_take_profit_order(self, symbol: str, side: str, stop_price: float):
        """
        下止盈单 (平仓市价止盈)
        :param symbol: 交易对
        :param side: 'BUY' (用于空头平仓) or 'SELL' (用于多头平仓)
        :param stop_price: 触发价格
        """
        params = {
            "symbol": symbol,
            "side": side,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": f"{stop_price:.8f}".rstrip('0').rstrip('.'),
            "closePosition": "true",
            "timeInForce": "GTC",
        }
        logger.info(f"准备下止盈单: {params}")
        response = await self._send_signed_request('POST', '/fapi/v1/order', params)
        if response and response.get('orderId'):
            logger.info(f"✅ 止盈单下单成功: {symbol} {side} 触发价 {stop_price}, OrderID: {response['orderId']}")
            return response
        else:
            logger.error(f"❌ 止盈单下单失败: {response}")
            return None