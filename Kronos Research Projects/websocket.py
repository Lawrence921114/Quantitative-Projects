import asyncio
import websockets
import json

OKX_PUBLIC_WS = "wss://ws.okx.com:8443/ws/v5/public"

async def subscribe_orderbook_20():
    async with websockets.connect(OKX_PUBLIC_WS) as ws:

        # Subscription request for top 20 levels
        sub_msg = {
            "op": "subscribe",
            "args": [
                {
                    "channel": "books20",     # <--- 20-level orderbook
                    "instId": "BTC-USDT-SWAP"
                }
            ]
        }

        await ws.send(json.dumps(sub_msg))
        print("Subscribed to BTC-USDT-SWAP (Orderbook L20).")

        while True:
            try:
                msg = await ws.recv()
                data = json.loads(msg)

                if "data" in data:
                    ob = data["data"][0]

                    best_bid = ob["bids"][0]
                    best_ask = ob["asks"][0]

                    print(f"Top Bid: {best_bid}")
                    print(f"Top Ask: {best_ask}")
                    print("-----------")

            except Exception as e:
                print("Error:", e)
                break


if __name__ == "__main__":
    asyncio.run(subscribe_orderbook_20())
    
