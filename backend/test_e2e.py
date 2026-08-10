import asyncio
from agents import run_agent_loop


class MockManager:
    async def broadcast(self, message):
        print(f"[WS BROADCAST] {message}")


async def main():
    manager = MockManager()
    print("--- Starting AutoMaintainer End-to-End Agent Loop Test ---")
    await run_agent_loop("PxA-Labs/AutoMaintainer", manager, None)
    print("--- Done ---")


if __name__ == "__main__":
    asyncio.run(main())
