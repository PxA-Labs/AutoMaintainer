import asyncio
import websockets


async def test_terminal():
    try:
        async with websockets.connect("ws://127.0.0.1:8000/api/terminal/ws") as ws:
            print("Connected to PTY!")
            # Send 'dir' and enter
            await ws.send("dir\r")

            # Read a few responses
            for _ in range(5):
                msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                print(f"Received: {repr(msg)}")
    except Exception as e:
        print(f"Test failed: {e}")


asyncio.run(test_terminal())
