import asyncio
import time
import json
import orjson
import bcrypt
import logging
from contextlib import asynccontextmanager
from concurrent.futures import ProcessPoolExecutor
from fastapi import FastAPI, Request, HTTPException

# ... (OTel imports and setup)
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

# (Tracer and Watchdog remain the same...)
trace_provider = TracerProvider()
otlp_exporter = OTLPSpanExporter()
span_processor = BatchSpanProcessor(otlp_exporter)
trace_provider.add_span_processor(span_processor)
trace.set_tracer_provider(trace_provider)

tracer = trace.get_tracer("fastapi-gil-guardian.main")
logger = logging.getLogger("gil_guardian")
logger.setLevel(logging.INFO)

# Global ProcessPoolExecutor reference
process_pool = None

# ... (event_loop_watchdog logic same as before)
async def event_loop_watchdog():
    logger.info("Event Loop Watchdog initialized...")
    sleep_duration = 0.01
    while True:
        start_time_perf = time.perf_counter()
        start_time_epoch = time.time()
        await asyncio.sleep(sleep_duration)
        actual_wake_time_perf = time.perf_counter()
        delta = actual_wake_time_perf - (start_time_perf + sleep_duration)
        if delta > 0.05:
            actual_wake_time_epoch = start_time_epoch + (actual_wake_time_perf - start_time_perf)
            intended_wake_time_epoch = actual_wake_time_epoch - delta
            start_time_ns = int(intended_wake_time_epoch * 1e9)
            end_time_ns = int(actual_wake_time_epoch * 1e9)
            span = tracer.start_span("event_loop_blocked", start_time=start_time_ns)
            span.set_attribute("event_loop.lag_seconds", delta)
            logger.warning(f"GIL Freeze Detected! Event loop blocked for {delta:.3f}s")
            span.end(end_time=end_time_ns)

# ==========================================
# REFACTOR: PROCESS POOL WORKER
# ==========================================

def worker_hash(password_str: str) -> bytes:
    """
    Executes CPU-bound bcrypt hashing in an entirely separate process.
    This frees the main asyncio event loop's GIL.
    """
    # Simple version for Step 4 (OTel propagation comes in Step 5)
    password_bytes = password_str.encode('utf-8')
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password_bytes, salt)

# ==========================================
# FASTAPI APPLICATION LIFESPAN & SETUP
# ==========================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    global process_pool
    # Initialize the ProcessPoolExecutor on startup
    process_pool = ProcessPoolExecutor()
    
    # Spin up the single continuous background watchdog task
    watchdog_task = asyncio.create_task(event_loop_watchdog())
    
    yield
    
    # Graceful shutdown
    watchdog_task.cancel()
    try:
        await watchdog_task
    except asyncio.CancelledError:
        pass
    
    # Prevent zombie processes
    if process_pool:
        process_pool.shutdown(wait=True)

app = FastAPI(
    title="fastapi-gil-guardian",
    description="Proving event loop starvation and Gil-safe architecture.",
    lifespan=lifespan
)

FastAPIInstrumentor.instrument_app(app)

# --- Naive Endpoint (v1) omitted for brevity or kept ---
@app.post("/api/v1/naive")
async def naive_workload(request: Request):
    raw_body = await request.body()
    with tracer.start_as_current_span("naive_json_parse") as json_span:
        json_span.set_attribute("payload.size_bytes", len(raw_body))
        data = json.loads(raw_body)
    password_str = data.get("password", "default_gil_guardian_secret")
    with tracer.start_as_current_span("naive_bcrypt_hash"):
        salt = bcrypt.gensalt()
        hashed_password = bcrypt.hashpw(password_str.encode('utf-8'), salt)
    return {"status": "success", "hash": hashed_password.decode('utf-8')[:15] + "..."}


# ==========================================
# MODULE 3: THE "OPTIMIZED" ENDPOINT (v2)
# ==========================================

@app.post("/api/v2/optimized")
async def optimized_workload(request: Request):
    """
    GIL-SAFE ARCHITECTURE ENDPOINT.
    Utilizes Rust-based `orjson` to yield the GIL during memory-heavy serialization,
    and explicitly offloads CPU-bound cryptography to a distinct process pool.
    """
    try:
        raw_body = await request.body()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Failed to read request body.")
    
    # 1. GIL-Releasing JSON Parsing (using orjson)
    with tracer.start_as_current_span("optimized_orjson_parse") as json_span:
        json_span.set_attribute("payload.size_bytes", len(raw_body))
        try:
            data = orjson.loads(raw_body)
        except orjson.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON payload.")
            
    password_str = data.get("password", "default_gil_guardian_secret")
    
    # 2. Asynchronous Offloading to the Process Pool
    loop = asyncio.get_running_loop()
    
    # This call now yields control immediately back to the event loop.
    # The math happens in a background process, leaving the Watchdog happy (no lag).
    hashed_password = await loop.run_in_executor(
        process_pool, 
        worker_hash, 
        password_str
    )
        
    return {
        "status": "success",
        "route": "optimized",
        "message": "GIL bypassed successfully. Event loop remains completely fluid.",
        "hash_preview": hashed_password.decode('utf-8')[:15] + "..."
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
