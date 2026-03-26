import asyncio
import time
import json
import orjson
import bcrypt
import logging
from contextlib import asynccontextmanager
from concurrent.futures import ProcessPoolExecutor

from fastapi import FastAPI, Request, HTTPException
from opentelemetry import trace, propagate
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

# ==========================================
# OBSERVABILITY FOUNDATION: OPEN TELEMETRY
# ==========================================

# Configure the OpenTelemetry Tracer Provider for the main process
trace_provider = TracerProvider()
otlp_exporter = OTLPSpanExporter()
span_processor = BatchSpanProcessor(otlp_exporter)
trace_provider.add_span_processor(span_processor)
trace.set_tracer_provider(trace_provider)

# Acquire the tracer for our application scope
tracer = trace.get_tracer("fastapi-gil-guardian.main")
logger = logging.getLogger("gil_guardian")
logger.setLevel(logging.INFO)

# Global ProcessPoolExecutor reference
process_pool = None

# ==========================================
# THE TRAP HOOK: EVENT LOOP WATCHDOG
# ==========================================

async def event_loop_watchdog():
    """
    Background daemon task. Continually sleeps for 10ms. 
    If the event loop is blocked by synchronous C-extensions (like json or bcrypt),
    the actual wake time will be delayed. We calculate this delta using perf_counter 
    for precision and manually emit a backdated OpenTelemetry span to visualize the freeze.
    """
    logger.info("Event Loop Watchdog initialized and watching for GIL freezes...")
    
    sleep_duration = 0.01  # 10 milliseconds
    
    while True:
        start_time_perf = time.perf_counter()
        start_time_epoch = time.time()
        
        # Yield control back to the event loop
        await asyncio.sleep(sleep_duration)
        
        actual_wake_time_perf = time.perf_counter()
        
        # Calculate lag using monotonic clock for precision (avoids NTP sync issues)
        delta = actual_wake_time_perf - (start_time_perf + sleep_duration)
        
        # Strictly check if the loop was starved for > 50ms
        if delta > 0.05:
            # Backdate the span to cover the exact timeframe the event loop was "dead"
            actual_wake_time_epoch = start_time_epoch + (actual_wake_time_perf - start_time_perf)
            intended_wake_time_epoch = actual_wake_time_epoch - delta
            
            # OTel requires timestamps in nanoseconds.
            start_time_ns = int(intended_wake_time_epoch * 1e9)
            end_time_ns = int(actual_wake_time_epoch * 1e9)
            
            # Start a span explicitly in the past
            span = tracer.start_span("event_loop_blocked", start_time=start_time_ns)
            span.set_attribute("event_loop.lag_seconds", delta)
            
            logger.warning(f"GIL Freeze Detected! Event loop blocked for {delta:.3f}s")
            
            # End the span exactly when the loop finally woke up
            span.end(end_time=end_time_ns)

# ==========================================
# MODULE 3 (CONT): PROCESS POOL WORKER WITH PROPAGATION
# ==========================================

def worker_hash(carrier: dict, password_str: str) -> bytes:
    """
    Executes CPU-bound bcrypt hashing in an entirely separate process.
    Crucially, it extracts the OpenTelemetry trace context from the carrier
    to guarantee the child span bridges the IPC boundary.
    """
    # 1. Re-initialize TracerProvider for the isolated child process
    # Background threads do not survive Python multiprocessing boundaries.
    provider = TracerProvider()
    processor = BatchSpanProcessor(ConsoleSpanExporter())
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)
    
    # 2. Extract the trace context injected by the main asyncio process
    ctx = propagate.extract(carrier)
    worker_tracer = trace.get_tracer("fastapi-gil-guardian.worker")
    
    # 3. Resume the trace using the extracted context to prevent detached spans
    with worker_tracer.start_as_current_span("optimized_bcrypt_hash", context=ctx) as span:
        span.set_attribute("bcrypt.work_factor", 12)
        
        password_bytes = password_str.encode('utf-8')
        salt = bcrypt.gensalt()
        hashed_bytes = bcrypt.hashpw(password_bytes, salt)
        
        # Force a flush so the span is exported before process exit/recycling
        provider.force_flush()
        
        return hashed_bytes


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
    description="Proving event loop starvation and Gil-safe observability.",
    lifespan=lifespan
)

# Instrument FastAPI to automatically generate root HTTP spans
FastAPIInstrumentor.instrument_app(app)

# ==========================================
# MODULE 2: THE "NAIVE" ENDPOINT
# ==========================================

@app.post("/api/v1/naive")
async def naive_workload(request: Request):
    try:
        raw_body = await request.body()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Failed to read request body.")
    
    # 1. Synchronous JSON Parsing
    with tracer.start_as_current_span("naive_json_parse") as json_span:
        json_span.set_attribute("payload.size_bytes", len(raw_body))
        data = json.loads(raw_body)
            
    password_str = data.get("password", "default_gil_guardian_secret")
    
    # 2. Synchronous CPU-Bound Hashing
    with tracer.start_as_current_span("naive_bcrypt_hash"):
        salt = bcrypt.gensalt()
        hashed_password = bcrypt.hashpw(password_str.encode('utf-8'), salt)
        
    return {
        "status": "success",
        "route": "naive",
        "message": "GIL blocked successfully. Check OTel traces for 'event_loop_blocked'.",
        "hash_preview": hashed_password.decode('utf-8')[:15] + "..."
    }

# ==========================================
# MODULE 3: THE "OPTIMIZED" ENDPOINT (V2)
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
    
    # 1. GIL-Releasing JSON Parsing
    with tracer.start_as_current_span("optimized_orjson_parse") as json_span:
        json_span.set_attribute("payload.size_bytes", len(raw_body))
        try:
            data = orjson.loads(raw_body)
        except orjson.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON payload.")
            
    password_str = data.get("password", "default_gil_guardian_secret")
    
    # 2. OpenTelemetry Context Propagation (The Carrier)
    # Inject current span context into a dictionary compatible with pickling.
    carrier = {}
    propagate.inject(carrier)
    
    # 3. Asynchronous Offloading to the Process Pool
    loop = asyncio.get_running_loop()
    
    # Yield control immediately back to the event loop.
    hashed_password = await loop.run_in_executor(
        process_pool, 
        worker_hash, 
        carrier, 
        password_str
    )
        
    return {
        "status": "success",
        "route": "optimized",
        "message": "GIL bypassed successfully. Event loop remains fluid.",
        "hash_preview": hashed_password.decode('utf-8')[:15] + "..."
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
