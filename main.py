import argparse
import logging
import os
import sys  # For exiting with error codes
import hashlib
import hmac
import time
import requests
import keyring

class BinanceBot:
    """
    A class to encapsulate Binance API interaction and trading logic.
    """

    def __init__(self, api_key=None, api_secret=None, base_url=None):
        self.api_key = api_key or keyring.get_password("BINANCE_API_KEY", "BINANCE_API_KEY")
        self.api_secret = api_secret or keyring.get_password("BINANCE_API_SECRET", "BINANCE_API_SECRET")
        self.base_url = base_url or os.environ.get("BINANCE_BASE_URL", "https://fapi.binance.com")

        # Validate required properties
        if not self.api_key or not self.api_secret:
            raise ValueError("API Key and Secret must be provided.")
        # Configure logging
        self.logger = logging.getLogger(__name__)
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(r".\app.log"),
                logging.StreamHandler()
            ]
        )

    def _generate_signature(self, params):
        """
        Generates HMAC-SHA256 signature for Binance API.
        """
        query_string = "&".join([f"{key}={value}" for key, value in params.items()])
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature

    def _make_request(self, method, endpoint, params=None, headers=None):
        """
        A helper method for making API requests.
        """
        url = f"{self.base_url}{endpoint}"
        if headers is None:
            headers = {"X-MBX-APIKEY": self.api_key}

        try:
            if method.upper() == "GET":
                response = requests.get(url, headers=headers, params=params)
            elif method.upper() == "POST":
                response = requests.post(url, headers=headers, params=params)
            else:
                raise ValueError("Unsupported HTTP method")

            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Request failed: {e}")
            return None

    def get_positions(self):
        """
        Retrieves position risk information from Binance Futures API.
        """
        params = {"timestamp": int(time.time() * 1000)}
        params["signature"] = self._generate_signature(params)
        positions = self._make_request("GET", "/fapi/v3/positionRisk", params=params)
        if positions:
            # Filter out positions with zero amount
            return [position for position in positions if float(position.get("positionAmt", 0)) != 0]
        return None

    def get_funding_rate(self, symbol):
        """
        Retrieves funding rate information for a given symbol.
        """
        endpoint = f"/fapi/v1/premiumIndex?symbol={symbol}"
        return self._make_request("GET", endpoint)

    def hedge_positions(self, positions):
        """
        Hedge the specified positions by placing market orders.
        """
        hedged_orders = []
        for position in positions:
            params = {
                "symbol": position["symbol"],
                "side": "BUY" if position["positionSide"] == "SHORT" else "SELL",
                "type": "MARKET",
                # Flip position side to create an opposing (hedge) order
                "positionSide": "LONG" if position["positionSide"] == "SHORT" else "SHORT",
                "quantity": abs(float(position["positionAmt"])),
                "timestamp": int(time.time() * 1000),
            }
            params["signature"] = self._generate_signature(params)
            response = self._make_request("POST", "/fapi/v1/order", params=params)
            hedged_orders.append(response)
        return hedged_orders

    def unwind_positions(self, orders):
        """
        Unwind the specified orders by placing market orders to close positions.
        NOTE: This method assumes the orders dictionary contains 'origQty'.
        """
        unwind_orders = []
        for order in orders:
            # Depending on the API response, you might need to adjust the source of the quantity.
            params = {
                "symbol": order["symbol"],
                "side": "BUY" if order["positionSide"] == "SHORT" else "SELL",
                "type": "MARKET",
                "positionSide": order["positionSide"],
                "quantity": abs(float(order.get("origQty", 0))),  # Ensure this field is present
                "timestamp": int(time.time() * 1000),
            }
            params["signature"] = self._generate_signature(params)
            response = self._make_request("POST", "/fapi/v1/order", params=params)
            unwind_orders.append(response)
        return unwind_orders

    def print_json(self, json_data):
        """
        Pretty prints JSON data to the console.
        """
        if json_data:
            import json
            print(json.dumps(json_data, indent=4))
        else:
            print("No data to display.")


def main():
    """
    Main function for running the BinanceBot logic.
    """
    # Argument parsing
    parser = argparse.ArgumentParser(description="Binance trading bot.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging.")
    args = parser.parse_args()

    # Set up bot instance
    try:
        bot = BinanceBot()
    except ValueError as ve:
        print(f"Configuration error: {ve}")
        sys.exit(1)

    if args.verbose:
        bot.logger.setLevel(logging.DEBUG)

    # Example workflow: hedge and then unwind after a 60-second delay
    try:
        bot.logger.info("Starting Binance bot...")
        positions = bot.get_positions()
        if not positions:
            bot.logger.warning("No open positions found.")
            return
        current_timestamp_ms = int(time.time() * 1000)
        bot.logger.info(f"Current timestamp: {current_timestamp_ms}")
        adjust = []
        for position in positions:
            symbol = position["symbol"]
            side = position["positionSide"]
            funding = bot.get_funding_rate(symbol)
            # if (abs(funding["nextFundingTime"] - current_timestamp_ms)) > 120000: # funding not in next 2 minutes
            #     continue
            # Check funding rate conditions to decide if hedging is needed
            if side == "LONG" and float(funding.get("lastFundingRate", 0)) > 0.0005:
                adjust.append(position)
                bot.logger.info(f"Funding rate for {symbol} is high: {funding.get('lastFundingRate', 0)}")
            elif side == "SHORT" and float(funding.get("lastFundingRate", 0)) < -0.0005:
                adjust.append(position)
                bot.logger.info(f"Funding rate for {symbol} is high: {funding.get('lastFundingRate', 0)}")

        bot.logger.info(f"Positions requiring hedging: {adjust}")
        if not adjust:
            bot.logger.info("No hedging required.")
            return

        # Execute hedge orders
        hedged_orders = bot.hedge_positions(adjust)
        bot.logger.info(f"Hedged orders: {hedged_orders}")

        # # Wait for 60 seconds before unwinding the orders
        bot.logger.info("Waiting for 60 seconds before unwinding...")
        time.sleep(60)

        # # Execute unwind orders (ensure that hedged_orders contain the required fields like 'origQty')
        unwind_orders = bot.unwind_positions(hedged_orders)
        bot.logger.info(f"Unwound orders: {unwind_orders}")

    except Exception as e:
        bot.logger.exception(f"An error occurred: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
