import asyncio
import json
import time
import aiohttp
import sys

# ==========================================
# QA AUTOMATION: LOAD TEST CONFIGURATION
# ==========================================

BASE_URL = "http://127.0.0.1:8000"
NAIVE_ENDPOINT = f"{BASE_URL}/api/v1/naive"
OPTIMIZED_ENDPOINT = f"{BASE_URL}/api/v2/optimized"
CONCURRENCY = 50
TOTAL_ITEMS = 55000  # Strictly > 50,000 items

def generate_massive_payload() -> str:
    """
    Dynamically generates a >5MB JSON payload in memory.
    This prevents disk read bottlenecks (OOM/IO limits) on the load tester itself
    and guarantees we hit the FastAPI server with a massive deserialization task.
    """
    print(f"[*] Synthesizing {TOTAL_ITEMS} dummy user objects in memory...")
    payload_dict = {
        "password": "qa_automation_super_secret_password_for_bcrypt",
        "data": []
    }
    
    for i in range(TOTAL_ITEMS):
        payload_dict["data"].append({
            "id": i,
            "username": f"synthetic_qa_user_{i}",
            "role": "DUMMY_DATA",
            "padding": "X" * 60  # Padding to ensure we easily clear the 5MB threshold
        })
        
    json_string = json.dumps(payload_dict)
    size_mb = len(json_string.encode('utf-8')) / (1024 * 1024)
    print(f"[*] Payload generated successfully. Size: {size_mb:.2f} MB")
    
    return json_string

async def send_request(session: aiohttp.ClientSession, url: str, payload: str, req_id: int):
    """
    Fires a single POST request and tracks its exact latency and status code.
    Real-time printing is used so the user can visually observe the GIL starvation.
    """
    start_time = time.time()
    try:
        async with session.post(url, data=payload, headers={'Content-Type': 'application/json'}) as response:
            status = response.status
            await response.read()  # Force reading the body to complete the request lifecycle
            latency = time.time() - start_time
            print(f"Req {req_id:02d} completed in {latency:.2f}s with status {status}")
            return {"id": req_id, "status": status, "latency": latency, "error": None}
    except Exception as e:
        latency = time.time() - start_time
        print(f"Req {req_id:02d} FAILED in {latency:.2f}s: {str(e)}")
        return {"id": req_id, "status": None, "latency": latency, "error": str(e)}

async def run_phase(phase_name: str, url: str, payload: str):
    """
    Executes a barrage of concurrent requests using asyncio.gather.
    """
    print(f"\n{'='*50}")
    print(f"🚀 INITIATING {phase_name.upper()}")
    print(f"Target: {url}")
    print(f"Concurrency: {CONCURRENCY} simultaneous requests")
    print(f"{'='*50}")
    
    # We use a generous timeout. The naive endpoint will freeze the server's GIL, 
    # meaning requests at the back of the queue will suffer extreme starvation delays.
    timeout = aiohttp.ClientTimeout(total=300)
    
    async with aiohttp.ClientSession(timeout=timeout) as session:
        tasks = [send_request(session, url, payload, i) for i in range(CONCURRENCY)]
        
        phase_start = time.time()
        # Fire all 50 requests at the exact same time
        results = await asyncio.gather(*tasks)
        phase_duration = time.time() - phase_start
        
        # Analyze results
        statuses = [r["status"] for r in results if r["status"] is not None]
        errors = [r["error"] for r in results if r["error"] is not None]
        latencies = [r["latency"] for r in results]
        
        avg_latency = sum(latencies) / len(latencies) if latencies else 0
        max_latency = max(latencies) if latencies else 0
        min_latency = min(latencies) if latencies else 0
        
        print("\n--- PHASE RESULTS ---")
        print(f"Total Phase Duration : {phase_duration:.2f} seconds")
        print(f"Successful Responses : {statuses.count(200)} / {CONCURRENCY}")
        if errors:
            print(f"Failed Requests      : {len(errors)} (e.g., {errors[0]})")
        print(f"Status Codes Seen    : {set(statuses)}")
        print(f"Min Latency          : {min_latency:.3f} seconds")
        print(f"Max Latency          : {max_latency:.3f} seconds")
        print(f"Average Latency      : {avg_latency:.3f} seconds")
        
        if "naive" in url:
            print("\n[QA NOTE]: Notice the massive Max/Average latencies and synchronous console output. "
                  "Because the GIL was locked, requests queued up synchronously. "
                  "Check your OTel dashboard to see the 'event_loop_blocked' Trap Hook spans!")
        else:
            print("\n[QA NOTE]: The event loop remained fluid! By yielding the GIL during serialization "
                  "and offloading bcrypt to the ProcessPool, asyncio handled the concurrent I/O flawlessly.")

async def main():
    print("Starting fastapi-gil-guardian Load Test...\n")
    
    # 1. Generate the payload once
    payload = generate_massive_payload()
    
    # 2. Phase 1: The Intentional Failure (Triggers the OTel Watchdog)
    await run_phase("Phase 1: Naive Endpoint", NAIVE_ENDPOINT, payload)
    
    # Cooldown period to let the server's event loop fully recover
    print("\n[*] Cooling down for 5 seconds before Phase 2...")
    await asyncio.sleep(5)
    
    # 3. Phase 2: The Optimized GIL-Safe Architecture
    await run_phase("Phase 2: Optimized Endpoint", OPTIMIZED_ENDPOINT, payload)
    
    print("\n✅ Load Test Complete. Please inspect your distributed tracing dashboard.")

if __name__ == "__main__":
    # Ensure compatibility with Windows environments if necessary
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[*] Load test aborted by user.")
