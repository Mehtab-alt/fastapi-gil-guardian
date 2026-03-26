import asyncio
import time
import json
import bcrypt
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

# ==========================================
# OBSERVABILITY FOUNDATION: OPEN TELEMETRY
# ==========================================

# Configure the OpenTelemetry Tracer Provider
trace_provider = TracerProvider()
otlp_exporter = OTLPSpanExporter()
span_processor = BatchSpanProcessor(otlp_exporter)
trace_provider.add_span_processor(span_processor)
trace.set_tracer_provider(trace_provider)

# Acquire the tracer for our application scope
tracer = trace.get_tracer("fastapi-gil-guardian.main")
logger = logging.getLogger("gil_guardian")
logger.setLevel(logging.INFO)

# ==========================================
# THE TRAP HOOK: EVENT LOOP WATCHDOG
# ==========================================

async def event_loop_watchdog():
    """
    Background daemon task. Continually sleeps for 10ms. 
    If the event loop is blocked by synchronous processing,
    the actual wake time will be delayed. We manually emit 
    a backdated OTel span to visualize the exact freeze.
    """
    logger.info("Event Loop Watchdog initialized and watching for GIL freezes...")
    
    sleep_duration = 0.01  # 10 milliseconds
    
    while True:
        start_time_perf = time.perf_counter()
        start_time_epoch = time.time()
        
        # Yield control back to the event loop
        await asyncio.sleep(sleep_duration)
        
        actual_wake_time_perf = time.perf_counter()
        
        # Calculate lag using monotonic clock (perf_counter)
        delta = actual_wake_time_perf - (start_time_perf + sleep_duration)
        
        # Trigger watchdog if the loop was starved for > 50ms
        if delta > 0.05:
            # Backdate the span to cover the exact timeframe the event loop was "dead"
            actual_wake_time_epoch = start_time_epoch + (actual_wake_time_perf - start_time_perf)
            intended_wake_time_epoch = actual_wake_time_epoch - delta
            
            # OTel requires nanosecond timestamps
            start_time_ns = int(intended_wake_time_epoch * 1e9)
            end_time_ns = int(actual_wake_time_epoch * 1e9)
            
            # Start and end a span manually in the past
            span = tracer.start_span("event_loop_blocked", start_time=start_time_ns)
            span.set_attribute("event_loop.lag_seconds", delta)
            
            logger.warning(f"GIL Freeze Detected! Event loop blocked for {delta:.3f}s")
            span.end(end_time=end_time_ns)

# ==========================================
# FASTAPI APPLICATION LIFESPAN & SETUP
# ==========================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Spin up the background watchdog task
    watchdog_task = asyncio.create_task(event_loop_watchdog())
    yield
    # Graceful shutdown
    watchdog_task.cancel()
    try:
        await watchdog_task
    except asyncio.CancelledError:
        pass

app = FastAPI(
    title="fastapi-gil-guardian",
    description="Proving event loop starvation and Gil-safe observability.",
    lifespan=lifespan
)

# Automatically generate traces for HTTP requests
FastAPIInstrumentor.instrument_app(app)

@app.post("/api/v1/naive")
async def naive_workload(request: Request):
    try:
        raw_body = await request.body()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Failed to read request body.")
    
    # 1. Synchronous JSON Parsing (Now wrapped in a trace span)
    with tracer.start_as_current_span("naive_json_parse") as json_span:
        json_span.set_attribute("payload.size_bytes", len(raw_body))
        try:
            data = json.loads(raw_body)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON payload.")
            
    password_str = data.get("password", "default_gil_guardian_secret")
    password_bytes = password_str.encode('utf-8')
    
    # 2. Synchronous Hashing (Now wrapped in a trace span)
    with tracer.start_as_current_span("naive_bcrypt_hash") as hash_span:
        hash_span.set_attribute("bcrypt.work_factor", 12)
        salt = bcrypt.gensalt()
        hashed_password = bcrypt.hashpw(password_bytes, salt)
        
    return {
        "status": "success",
        "route": "naive",
        "message": "GIL blocked successfully. Inspect OTel traces for 'event_loop_blocked'.",
        "hash_preview": hashed_password.decode('utf-8')[:15] + "..."
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
