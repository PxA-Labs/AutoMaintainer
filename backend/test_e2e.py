import asyncio
from agents import run_agent_loop


async def main():
    print("--- Starting AutoMaintainer End-to-End Agent Loop Test ---")
    await run_agent_loop("PxA-Labs/AutoMaintainer", None, "test-run-123")
    print("--- Done ---")


if __name__ == "__main__":
    asyncio.run(main())
