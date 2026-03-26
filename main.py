import json
import bcrypt
from fastapi import FastAPI, Request, HTTPException

app = FastAPI(
    title="fastapi-gil-guardian",
    description="Initial naive implementation of password hashing API."
)

@app.post("/api/v1/naive")
async def naive_workload(request: Request):
    """
    INTENTIONAL FAILURE ENDPOINT.
    This route accepts a massive JSON payload and processes it using the standard
    synchronous `json` library and standard thread-bound `bcrypt`. It will aggressively 
    lock the GIL and starve the event loop.
    """
    try:
        raw_body = await request.body()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Failed to read request body.")
    
    # 1. Synchronous JSON Parsing (Will block the GIL on large payloads)
    try:
        # Standard library call: blocks the main thread
        data = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")
            
    password_str = data.get("password", "default_gil_guardian_secret")
    password_bytes = password_str.encode('utf-8')
    
    # 2. Synchronous CPU-Bound Hashing (Holds the GIL hostage)
    salt = bcrypt.gensalt()
    hashed_password = bcrypt.hashpw(password_bytes, salt)
        
    return {
        "status": "success",
        "route": "naive",
        "message": "Password hashed via synchronous main-thread logic.",
        "hash_preview": hashed_password.decode('utf-8')[:15] + "..."
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
