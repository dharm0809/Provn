"""Send 10 questions through the gateway — mix of regular and web search, two models."""

import asyncio
import httpx

GATEWAY = "http://localhost:8002"
API_KEY = "test-key-alpha"

QUESTIONS = [
    # Regular questions
    ("qwen3:4b", "What is the speed of light?"),
    ("gemma3:1b", "Explain photosynthesis in one sentence."),
    ("qwen3:4b", "What is the Pythagorean theorem?"),
    ("gemma3:1b", "Name three planets in our solar system."),
    # Web search questions (Wikipedia-indexed topics that DDG handles well)
    ("qwen3:4b", "Search the web for information about Python programming language"),
    ("qwen3:4b", "Search the web for information about Tesla Motors"),
    ("qwen3:4b", "Search the web for information about Mars planet"),
    ("qwen3:4b", "Search the web for information about machine learning"),
    ("qwen3:4b", "Search the web for information about Linux operating system"),
    ("qwen3:4b", "Search the web for information about JavaScript"),
]

HEADERS = {"Content-Type": "application/json", "X-API-Key": API_KEY}


async def send(client: httpx.AsyncClient, idx: int, model: str, question: str):
    payload = {"model": model, "messages": [{"role": "user", "content": question}], "stream": False}
    print(f"[{idx+1:2d}/10] {model:10s} | {question[:55]}...")
    try:
        resp = await client.post(f"{GATEWAY}/v1/chat/completions", json=payload, headers=HEADERS, timeout=120.0)
        if resp.status_code == 200:
            data = resp.json()
            tokens = data.get("usage", {}).get("total_tokens", "?")
            content = data["choices"][0]["message"]["content"][:80]
            # Check if tool_interactions are in metadata (web search was used)
            meta = data["choices"][0]["message"].get("metadata", {})
            print(f"         ✓ {tokens} tokens | {content}...")

            # Check for tool events via lineage
            session_id = resp.headers.get("x-session-id", "")
            if session_id:
                lr = await client.get(
                    f"{GATEWAY}/v1/lineage/sessions/{session_id}",
                    headers=HEADERS, timeout=10.0,
                )
                if lr.status_code == 200:
                    records = lr.json().get("records", [])
                    for rec in records:
                        tool_ints = rec.get("metadata", {}).get("tool_interactions", [])
                        if tool_ints:
                            for ti in tool_ints:
                                sources = ti.get("sources", [])
                                print(f"         🔍 web_search called | {len(sources)} sources | hash: {ti.get('input_hash','')[:24]}...")
        else:
            print(f"         ✗ HTTP {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        print(f"         ✗ Error: {e}")


async def main():
    print("=" * 70)
    print("Gateway Web Search Test — 10 questions (4 regular + 6 web search)")
    print("=" * 70)
    async with httpx.AsyncClient() as client:
        for idx, (model, question) in enumerate(QUESTIONS):
            await send(client, idx, model, question)
            await asyncio.sleep(2)

    print("=" * 70)
    print("Done! Check dashboard at http://localhost:5173/lineage/")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
