# 🛡️ fastapi-gil-guardian

Welcome to the **fastapi-gil-guardian**, a high-performance blueprint and engineering report demonstrating how to handle mixed I/O and CPU workloads in Python. This project proves, through empirical observability, how to bypass the Global Interpreter Lock (GIL) and maintain a fluid event loop under extreme duress.

## The Bottleneck: The Cooperative Contract and the GIL

The Python `asyncio` ecosystem is built entirely on a "Cooperative Contract." Tasks must willingly and swiftly yield control back to the event loop. The event loop is a single thread; its only job is routing network I/O and scheduling. 

However, two primary "silent killers" routinely break this contract in standard FastAPI applications:
1. **Large JSON Payloads:** The standard Python `json` library is a synchronous C-extension. When deserializing a massive 5MB payload, it holds the Global Interpreter Lock (GIL) hostage.
2. **Cryptographic Hashing:** Libraries like `bcrypt` are intentionally CPU-intensive (designed with a Work Factor). Executing `bcrypt.hashpw` in the main thread refuses to yield the GIL.

When these operations occur, you experience the **0% CPU Freeze**. The CPU is 100% busy on one core crunching math or parsing strings, but the event loop cannot schedule *any* other tasks. Concurrent requests queue up indefinitely, causing massive latency spikes across your entire API, despite seemingly low overall system utilization.
