# 🛡️ fastapi-gil-guardian

Welcome to the **fastapi-gil-guardian**, a high-performance blueprint and engineering report demonstrating how to handle mixed I/O and CPU workloads in Python. This project proves, through empirical observability, how to bypass the Global Interpreter Lock (GIL) and maintain a fluid event loop under extreme duress.

## The Bottleneck: The Cooperative Contract and the GIL

The Python `asyncio` ecosystem is built entirely on a "Cooperative Contract." Tasks must willingly and swiftly yield control back to the event loop. The event loop is a single thread; its only job is routing network I/O and scheduling. 

However, two primary "silent killers" routinely break this contract in standard FastAPI applications:
1. **Large JSON Payloads:** The standard Python `json` library is a synchronous C-extension. When deserializing a massive 5MB payload, it holds the Global Interpreter Lock (GIL) hostage.
2. **Cryptographic Hashing:** Libraries like `bcrypt` are intentionally CPU-intensive (designed with a Work Factor). Executing `bcrypt.hashpw` in the main thread refuses to yield the GIL.

When these operations occur, you experience the **0% CPU Freeze**. The CPU is 100% busy on one core crunching math or parsing strings, but the event loop cannot schedule *any* other tasks. Concurrent requests queue up indefinitely, causing massive latency spikes across your entire API, despite seemingly low overall system utilization.

## Event Loop Telemetry

In this domain, observability is not a luxury; it is the absolute proof of architecture. To visualize the exact moment the Cooperative Contract is broken, we implemented the **"Trap Hook" Watchdog**.

To expose the silent killer, we deployed a background daemon task that runs a continuous heartbeat: `await asyncio.sleep(0.01)`. 

### Visual Evidence: The "Frozen" vs. "Fluid" Event Loop

By load-testing our intentionally broken `/api/v1/naive` endpoint and our GIL-safe `/api/v2/optimized` endpoint, the telemetry reveals the exact architectural difference.

[HTTP POST /api/v2/optimized ........] 🌊 FLUID (asyncio lag < 5ms)
```

> **⚠️ Architectural Warning: Latency vs. Fluidity**
> Claiming a "<5ms lag Post-Fix fluid state" refers strictly to the *Event Loop's* responsiveness to I/O, not the duration of the request itself. Offloading does not inherently accelerate the math; it simply isolates the math to prevent it from starving *other* concurrent users. 

## Surviving the Void: ProcessPool Context Propagation

A distributed trace that breaks at a process boundary is a lie. Background threads (like OTel's `BatchSpanProcessor`) do not survive Python multiprocessing boundaries (fork/spawn). To maintain the parent-child span relationship, the trace context MUST be explicitly serialized (injected), passed over the IPC boundary, and extracted by the worker.

```python
# 1. Main Process: Inject context into a picklable carrier
carrier = {}
propagate.inject(carrier)

# 2. Yield to the ProcessPool (The GIL is safe!)
hashed_password = await loop.run_in_executor(process_pool, worker_hash, carrier, password_str)

# 3. Worker Process (Re-hydrate OTel)
def worker_hash(carrier: dict, ...):
    provider = TracerProvider() # Re-bootstrapping is required!
    trace.set_tracer_provider(provider)
    ctx = propagate.extract(carrier) # Bridges the IPC boundary
    with tracer.start_as_current_span(..., context=ctx):
        ...
```
