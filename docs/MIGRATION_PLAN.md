# Migration Plan: Incremental Improvements

This document details the incremental migration path for the Personal Cycling Agent project. Each phase is designed to be independently deployable and testable, minimizing risk and allowing rollback at any stage.

## Current State Assessment

### Critical Issues (Fixed)
- [x] `run.sh` called non-existent `--sync` flag (changed to `--ingest`)
- [x] Password persistence broken after UI save (added `GARMIN_PASSWORD_RAW`)
- [x] Missing `from datetime import date` in add-on `readiness.py`
- [x] Bare `from fitparse import FitFile` in add-on `garmin_export.py`

### Architectural Problems
1. **Code duplication**: `src/` and `personal_cycling_agent/src/` have diverged significantly
2. **Slow FIT parsing**: `fitparse` is pure Python, 5-10x slower than alternatives
3. **No background tasks**: UI blocks during sync, no progress feedback
4. **Streamlit limitations**: Full re-exec on every interaction, awkward state management
5. **Server-side chart rendering**: Plotly rendered on server, consumes memory/CPU

### Performance Bottlenecks
- FIT file parsing: 30+ seconds for large files (pure Python `fitparse`)
- Analytics computation: Blocks UI during processing
- Large datasets: All data loaded into memory before rendering
- No streaming or chunked processing

---

## Phase 1: Code Deduplication

**Goal**: Eliminate the dual-codebase problem by making the HA add-on import from the standalone source.

**Effort**: 2-3 days  
**Risk**: Medium (affects both deployments)  
**Prerequisites**: None

### Why First?
Every subsequent change would need to be made in two places. Deduplicating now prevents drift and reduces maintenance burden by 50%.

### Approach
Make `personal_cycling_agent/src/` a symlink to `src/`, or restructure so both deployments share the same code.

### Detailed Steps

#### Step 1.1: Audit Differences
```bash
# Generate a diff report
diff -r src/ personal_cycling_agent/src/ > /tmp/codebase_diff.txt
```

Document all differences:
- `main.py`: Missing `_CYCLING_REEXEC` guard in add-on
- `visualize.py`: Theme detection (`st.query_params` vs `st.get_option`), quote handling in `_update_config_env`
- `db/store.py`: Missing `check_same_thread=False` in add-on
- `analytics/readiness.py`: Missing `date` import in add-on
- `analytics/threshold.py`: Different interpolation logic
- `ingestion/garmin_connect.py`: Loop behavior, DB connection management
- `ingestion/garmin_export.py`: Bare import vs guarded import

#### Step 1.2: Choose Strategy
**Option A: Symlink** (Recommended)
- Make `personal_cycling_agent/src/` a symlink to `../src/`
- Both deployments use identical code
- Single source of truth

**Option B: Shared Package**
- Move `src/` to a Python package (e.g., `cycling_agent/`)
- Both `main.py` entry points import from the package
- More complex, requires packaging

**Recommendation**: Option A (symlink) for simplicity.

#### Step 1.3: Reconcile Differences
Before creating the symlink, merge the best parts of each version:

1. **`main.py`**: Keep standalone version (has `_CYCLING_REEXEC` guard)
2. **`visualize.py`**: Keep standalone version (correct theme detection, proper quote handling)
3. **`db/store.py`**: Keep standalone version (has `check_same_thread=False`)
4. **`analytics/readiness.py`**: Keep standalone version (has `date` import)
5. **`analytics/threshold.py`**: Keep standalone version (better interpolation)
6. **`ingestion/garmin_connect.py`**: Merge loop behavior
   - Standalone: bounded by `days` parameter
   - Add-on: unbounded `while True`
   - **Solution**: Add `unbounded: bool = False` parameter to `sync_activities()` and `sync_garmin()`
7. **`ingestion/garmin_export.py`**: Keep standalone version (guarded import)

#### Step 1.4: Create Symlink
```bash
# Remove the add-on src directory
rm -rf personal_cycling_agent/src/

# Create symlink
ln -s ../src personal_cycling_agent/src/

# Verify
ls -la personal_cycling_agent/src/
# Should show: src -> ../src
```

#### Step 1.5: Update Dockerfile
The Dockerfile already does `COPY src/ ./src/`, which will follow the symlink. No changes needed.

#### Step 1.6: Test Both Deployments
**Standalone**:
```bash
python -m src.main --ingest
python -m src.main --analyze
python -m src.main --visualize
```

**HA Add-on** (via devcontainer):
- Build add-on
- Verify all pages load
- Verify sync works

### Verification
- [ ] `diff -r src/ personal_cycling_agent/src/` shows no differences (symlink)
- [ ] All 192 tests pass
- [ ] Standalone CLI works
- [ ] HA add-on builds and runs
- [ ] No import errors in either deployment

---

## Phase 2: FIT Parsing Performance (fitparse → fitdecode)

**Goal**: Replace `fitparse` with `fitdecode` for 5-10x faster FIT file parsing.

**Effort**: 3-4 days  
**Risk**: Medium (core data ingestion)  
**Prerequisites**: Phase 1 (code deduplication)

### Why fitdecode?
- **Performance**: `fitdecode` has optional C extensions, 5-10x faster than pure Python `fitparse`
- **API compatibility**: Similar message-based iteration pattern
- **Active maintenance**: More recent updates, better FIT protocol support
- **Memory efficiency**: Streaming iteration without materializing all records

### Current fitparse Usage

**Files affected** (after Phase 1, only one copy):
1. `src/ingestion/garmin_connect.py` - `_fetch_activity_streams()` (lines 452-499)
2. `src/ingestion/garmin_export.py` - `sync_routes_from_fit()` (lines 320-349)

**fitparse patterns**:
```python
# garmin_connect.py
from fitparse import FitFile
fit_file = FitFile(str(fit_path))
for msg in fit_file.get_messages("record"):
    ts = msg.get_value("timestamp")
    power = msg.get_value("power")
    heart_rate = msg.get_value("heart_rate")
    # ... etc
fit_file.close()

# garmin_export.py
fit = FitFile(str(fit_path))
records = list(fit.get_messages("record"))
fit.close()
for rec in records:
    lat = rec.get_value("position_lat")
    lon = rec.get_value("position_long")
```

### Detailed Steps

#### Step 2.1: Benchmark Current Performance
Before changing anything, establish a baseline:

```python
# benchmark_fitparse.py
import time
from pathlib import Path
from fitparse import FitFile

fit_files = list(Path("raw/fit/").glob("*.fit"))
total_time = 0
for fit_path in fit_files[:10]:  # Test first 10 files
    start = time.perf_counter()
    fit = FitFile(str(fit_path))
    records = list(fit.get_messages("record"))
    fit.close()
    elapsed = time.perf_counter() - start
    total_time += elapsed
    print(f"{fit_path.name}: {elapsed:.2f}s, {len(records)} records")

print(f"Total: {total_time:.2f}s for {min(10, len(fit_files))} files")
```

Run this on your actual data to get baseline numbers.

#### Step 2.2: Install fitdecode
```bash
pip install fitdecode
```

Add to `requirements.txt`:
```txt
fitdecode>=0.10.0  # or latest stable
```

Remove `fitparse`:
```txt
# Remove: fitparse>=1.1.0
```

#### Step 2.3: Rewrite `_fetch_activity_streams()` in `garmin_connect.py`

**fitparse version** (current):
```python
try:
    from fitparse import FitFile
except ImportError:
    FitFile = None

def _fetch_activity_streams(...):
    if FitFile is None:
        return 0
    
    fit_file = FitFile(str(fit_path))
    for msg in fit_file.get_messages("record"):
        ts = msg.get_value("timestamp")
        if ts is None:
            continue
        if hasattr(ts, "timestamp"):
            ts = ts.timestamp()
        # ... extract fields
    fit_file.close()
```

**fitdecode version** (new):
```python
try:
    import fitdecode
except ImportError:
    fitdecode = None

def _fetch_activity_streams(...):
    if fitdecode is None:
        return 0
    
    with fitdecode.FitReader(str(fit_path)) as fit:
        for frame in fit:
            if isinstance(frame, fitdecode.FitDataMessage):
                if frame.name == "record":
                    # Get timestamp
                    ts_field = frame.get_field("timestamp")
                    if ts_field is None:
                        continue
                    ts = ts_field.value
                    if hasattr(ts, "timestamp"):
                        ts = ts.timestamp()
                    
                    # Get power
                    power_field = frame.get_field("power")
                    if power_field is not None:
                        power_value = float(power_field.value)
                        # ... store
                    
                    # Get heart_rate
                    hr_field = frame.get_field("heart_rate")
                    if hr_field is not None:
                        hr_value = float(hr_field.value)
                        # ... store
                    
                    # Similar for cadence, speed, altitude
```

**Key differences**:
- `FitFile(path)` → `fitdecode.FitReader(path)` (context manager)
- `get_messages("record")` → iterate all frames, filter by `isinstance(frame, FitDataMessage)` and `frame.name == "record"`
- `msg.get_value("field")` → `frame.get_field("field")` then `.value`
- Field names are the same: `timestamp`, `power`, `heart_rate`, `cadence`, `enhanced_speed`, `speed`, `enhanced_altitude`, `altitude`, `position_lat`, `position_long`

#### Step 2.4: Rewrite `sync_routes_from_fit()` in `garmin_export.py`

**fitparse version** (current):
```python
fit = FitFile(str(fit_path))
records = list(fit.get_messages("record"))
fit.close()

for rec in records:
    lat = rec.get_value("position_lat")
    lon = rec.get_value("position_long")
    if lat is not None and lon is not None:
        points.append((lat / 1e7, lon / 1e7))
```

**fitdecode version** (new):
```python
points = []
with fitdecode.FitReader(str(fit_path)) as fit:
    for frame in fit:
        if isinstance(frame, fitdecode.FitDataMessage) and frame.name == "record":
            lat_field = frame.get_field("position_lat")
            lon_field = frame.get_field("position_long")
            if lat_field is not None and lon_field is not None:
                lat = lat_field.value / 1e7
                lon = lon_field.value / 1e7
                points.append((lat, lon))
```

**Optimization**: Don't materialize all records to a list. Process streaming.

#### Step 2.5: Handle Enhanced Fields
Garmin devices often use "enhanced" fields (`enhanced_speed`, `enhanced_altitude`) with higher precision. The current code does:
```python
speed = msg.get_value("enhanced_speed") or msg.get_value("speed")
```

In fitdecode, the same pattern works:
```python
speed_field = frame.get_field("enhanced_speed")
if speed_field is None:
    speed_field = frame.get_field("speed")
speed_value = speed_field.value if speed_field else None
```

#### Step 2.6: Update Error Handling
fitparse uses `FitParseError`. fitdecode uses `FitError` or `FitParseError`. Update exception handling:

```python
# Old
except Exception as e:
    logger.debug(f"Failed to parse: {e}")

# New
try:
    with fitdecode.FitReader(str(fit_path)) as fit:
        # ...
except (fitdecode.FitError, fitdecode.FitParseError) as e:
    logger.warning(f"FIT parse error: {e}")
except Exception as e:
    logger.debug(f"Unexpected error: {e}")
```

#### Step 2.7: Benchmark New Performance
Run the same benchmark script with fitdecode:

```python
# benchmark_fitdecode.py
import time
from pathlib import Path
import fitdecode

fit_files = list(Path("raw/fit/").glob("*.fit"))
total_time = 0
for fit_path in fit_files[:10]:
    start = time.perf_counter()
    with fitdecode.FitReader(str(fit_path)) as fit:
        records = [f for f in fit if isinstance(f, fitdecode.FitDataMessage) and f.name == "record"]
    elapsed = time.perf_counter() - start
    total_time += elapsed
    print(f"{fit_path.name}: {elapsed:.2f}s, {len(records)} records")

print(f"Total: {total_time:.2f}s for {min(10, len(fit_files))} files")
```

**Expected result**: 5-10x speedup.

#### Step 2.8: Test with Real Data
1. Delete existing `activity_streams` data (or use a test DB)
2. Run sync with fitdecode: `python -m src.main --ingest`
3. Verify data integrity:
   - Same number of records as fitparse
   - Same values (within floating-point tolerance)
   - No missing fields
4. Run analytics: `python -m src.main --analyze`
5. Verify charts render correctly

### Verification
- [ ] Benchmark shows 5-10x speedup
- [ ] All 192 tests pass
- [ ] Sync produces identical data (spot-check 10 activities)
- [ ] Analytics results unchanged
- [ ] Charts render correctly
- [ ] No import errors

### Rollback Plan
If fitdecode causes issues:
1. Revert `requirements.txt` to `fitparse>=1.1.0`
2. Revert `garmin_connect.py` and `garmin_export.py` to fitparse version
3. `pip install fitparse`

---

## Phase 3: Background Task Infrastructure

**Goal**: Move long-running operations (sync, analytics) to background tasks with progress tracking.

**Effort**: 4-5 days  
**Risk**: Medium  
**Prerequisites**: Phase 1 (code deduplication)

### Current Problem
- Sync buttons in Streamlit UI block the main thread
- No progress feedback during sync
- UI freezes during analytics computation
- No way to cancel a running operation

### Solution: Background Task Queue
Use **ARQ** (Async Redis Queue) or **Celery** for background task processing.

**Recommendation**: **ARQ** for simplicity (Redis is lighter than Celery's broker requirements).

### Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    FastAPI / Streamlit                    │
│  ┌──────────────────────────────────────────────────┐  │
│  │  POST /sync                                       │  │
│  │  → Enqueue task to ARQ                            │  │
│  │  → Return task_id immediately                     │  │
│  └──────────────────────────────────────────────────┘  │
│                          │                               │
└──────────────────────────┼───────────────────────────────┘
                           │
┌──────────────────────────┼───────────────────────────────┐
│                    ARQ Worker                             │
│  ┌──────────────────────────────────────────────────┐  │
│  │  Task: run_ingest()                               │  │
│  │  → Update progress in Redis                       │  │
│  │  → Call sync_garmin(), sync_activities()          │  │
│  │  → Store results in DB                            │  │
│  └──────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
                           │
┌──────────────────────────┼───────────────────────────────┐
│                    Redis                                  │
│  - Task queue                                              │
│  - Progress tracking (task_id → status, progress %)      │
│  - Results (task_id → result dict)                       │
└─────────────────────────────────────────────────────────┘
```

### Detailed Steps

#### Step 3.1: Add Redis and ARQ Dependencies
```txt
# requirements.txt
redis>=5.0.0
arq>=0.25.0
```

#### Step 3.2: Update Dockerfile
Add Redis to the container (or use external Redis):

**Option A: Redis in container** (simpler for HA add-on)
```dockerfile
RUN apt-get install -y redis-server
```

Update `run.sh` to start Redis:
```bash
redis-server --daemonize yes
```

**Option B: External Redis** (better for production)
- Use HA's Redis add-on or external Redis instance
- Configure via `REDIS_URL` env var

**Recommendation**: Option A for self-contained HA add-on.

#### Step 3.3: Create Task Worker
```python
# src/tasks/worker.py
from arq import create_pool
from arq.connections import RedisSettings

async def run_ingest_task(ctx):
    """Background task wrapper for run_ingest()."""
    from src.main import run_ingest
    
    # Update progress
    await ctx["redis"].hset(f"task:{ctx['job_id']}", mapping={
        "status": "running",
        "progress": "0",
        "stage": "Starting ingestion..."
    })
    
    # Run the actual work
    result = run_ingest()
    
    # Store result
    await ctx["redis"].hset(f"task:{ctx['job_id']}", mapping={
        "status": "completed",
        "progress": "100",
        "result": json.dumps(result)
    })
    
    return result

async def run_analyze_task(ctx):
    """Background task wrapper for run_analyze()."""
    from src.main import run_analyze
    
    await ctx["redis"].hset(f"task:{ctx['job_id']}", mapping={
        "status": "running",
        "progress": "0",
        "stage": "Starting analysis..."
    })
    
    result = run_analyze()
    
    await ctx["redis"].hset(f"task:{ctx['job_id']}", mapping={
        "status": "completed",
        "progress": "100",
        "result": json.dumps(result)
    })
    
    return result

class WorkerSettings:
    functions = [run_ingest_task, run_analyze_task]
    redis_settings = RedisSettings(host="localhost", port=6379)
```

#### Step 3.4: Add Progress Tracking to Sync Functions
Modify `sync_garmin()` and `sync_activities()` to accept a progress callback:

```python
# src/ingestion/garmin_connect.py
def sync_garmin(
    days: int = 1,
    db_path: str | None = None,
    tokenstore: str | None = None,
    progress_callback: Callable[[str, int], None] | None = None,
) -> dict[str, int]:
    """
    Args:
        progress_callback: Optional callback(stage: str, percent: int)
    """
    # ... existing code ...
    
    for day_offset in range(days):
        if progress_callback:
            progress_callback("Fetching wellness data", int(day_offset / days * 100))
        
        # ... fetch wellness for date ...
```

#### Step 3.5: Create API Endpoints for Task Management
```python
# src/web/routes/tasks.py (for FastAPI, or add to Streamlit)
from fastapi import APIRouter, BackgroundTasks
import redis

router = APIRouter()
redis_client = redis.Redis(host="localhost", port=6379, decode_responses=True)

@router.post("/tasks/sync")
async def start_sync(days: int = 7):
    """Enqueue a sync task."""
    arq_pool = await create_pool(RedisSettings())
    job = await arq_pool.enqueue_job("run_ingest_task")
    return {"task_id": job.job_id, "status": "queued"}

@router.get("/tasks/{task_id}")
async def get_task_status(task_id: str):
    """Get task status and progress."""
    task_data = redis_client.hgetall(f"task:{task_id}")
    if not task_data:
        raise HTTPException(404, "Task not found")
    return {
        "task_id": task_id,
        "status": task_data.get("status", "unknown"),
        "progress": int(task_data.get("progress", 0)),
        "stage": task_data.get("stage", ""),
        "result": json.loads(task_data.get("result", "null")),
    }
```

#### Step 3.6: Update UI to Poll Task Status
**Streamlit version** (interim solution):
```python
# In visualize.py
import time

if st.button("Sync"):
    # Start task via API call
    response = requests.post("http://localhost:8502/tasks/sync", json={"days": 7})
    task_id = response.json()["task_id"]
    
    # Poll for completion
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    while True:
        status = requests.get(f"http://localhost:8502/tasks/{task_id}").json()
        progress_bar.progress(status["progress"] / 100)
        status_text.text(status["stage"])
        
        if status["status"] == "completed":
            st.success("Sync complete!")
            st.json(status["result"])
            break
        elif status["status"] == "failed":
            st.error("Sync failed")
            break
        
        time.sleep(1)
```

**FastAPI + HTMX version** (final solution):
```html
<!-- templates/sync.html -->
<button hx-post="/tasks/sync" 
        hx-target="#sync-status"
        hx-swap="innerHTML">
    Start Sync
</button>

<div id="sync-status">
    <!-- HTMX polls for status -->
    <div hx-get="/tasks/{{ task_id }}/status"
         hx-trigger="every 1s"
         hx-swap="innerHTML">
        Loading...
    </div>
</div>
```

#### Step 3.7: Start ARQ Worker in `run.sh`
```bash
# Start Redis
redis-server --daemonize yes

# Start ARQ worker in background
arq src.tasks.worker.WorkerSettings &
ARQ_PID=$!

# Start Streamlit/FastAPI
...

# Wait for all background processes
wait ${STREAMLIT_PID} ${ARQ_PID}
```

### Verification
- [ ] Redis starts successfully in container
- [ ] ARQ worker connects to Redis
- [ ] Sync task enqueues and runs
- [ ] Progress updates visible in UI
- [ ] Task results stored and retrievable
- [ ] UI doesn't block during sync
- [ ] Multiple tasks can run concurrently

### Rollback Plan
If ARQ causes issues:
1. Remove ARQ/Redis dependencies
2. Revert to synchronous sync (current behavior)
3. Keep progress callback infrastructure for future use

---

## Phase 4: FastAPI Skeleton

**Goal**: Set up FastAPI app structure with HA Ingress compatibility, run in parallel with Streamlit.

**Effort**: 2-3 days  
**Risk**: Low (additive, doesn't replace Streamlit yet)  
**Prerequisites**: Phase 1 (code deduplication)

### Approach
Create a minimal FastAPI app that serves a "Hello World" page alongside Streamlit. This validates the infrastructure before migrating pages.

### Detailed Steps

#### Step 4.1: Add FastAPI Dependencies
```txt
# requirements.txt
fastapi>=0.104.0
uvicorn[standard]>=0.24.0
jinja2>=3.1.0
python-multipart>=0.0.6
```

#### Step 4.2: Create FastAPI App Structure
```
src/
├── web/
│   ├── __init__.py
│   ├── app.py              # FastAPI app factory
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── pages.py        # HTML page routes
│   │   └── api.py          # JSON API routes
│   ├── templates/
│   │   └── index.html      # Minimal test page
│   └── static/
│       └── css/
│           └── style.css
```

#### Step 4.3: Create App Factory
```python
# src/web/app.py
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
import os

def create_app() -> FastAPI:
    app = FastAPI(
        title="Personal Cycling Agent",
        root_path=os.environ.get("HASSIO_INGRESS", ""),
    )
    
    # Static files
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    
    # Templates
    templates_dir = Path(__file__).parent / "templates"
    templates = Jinja2Templates(directory=templates_dir)
    app.state.templates = templates
    
    # Routes
    from .routes import pages
    app.include_router(pages.router)
    
    return app
```

#### Step 4.4: Create Minimal Page Route
```python
# src/web/routes/pages.py
from fastapi import APIRouter, Request

router = APIRouter()

@router.get("/")
async def index(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse("index.html", {"request": request})
```

#### Step 4.5: Create Test Template
```html
<!-- src/web/templates/index.html -->
<!DOCTYPE html>
<html>
<head>
    <title>Cycling Agent (FastAPI)</title>
    <link rel="stylesheet" href="/static/css/style.css">
</head>
<body>
    <h1>Personal Cycling Agent</h1>
    <p>FastAPI is running!</p>
    <p><a href="/streamlit">Switch to Streamlit UI</a></p>
</body>
</html>
```

#### Step 4.6: Update `run.sh` to Run Both
```bash
# Start FastAPI on port 8502
uvicorn src.web.app:create_app \
    --factory \
    --host 0.0.0.0 \
    --port 8502 \
    &
FASTAPI_PID=$!

# Start Streamlit on port 8501 (existing)
python3 -m streamlit run src/visualize.py \
    --server.headless true \
    --server.address 0.0.0.0 \
    --server.port 8501 \
    &
STREAMLIT_PID=$!

# Wait for both
wait ${FASTAPI_PID} ${STREAMLIT_PID}
```

#### Step 4.7: Test HA Ingress
Update `config.json` temporarily to point to FastAPI:
```json
{
  "ingress_port": 8502,
  "ingress_stream": false
}
```

Verify FastAPI is accessible via HA Ingress.

### Verification
- [ ] FastAPI starts without errors
- [ ] `/` returns HTML page
- [ ] Static files load correctly
- [ ] HA Ingress proxies to FastAPI
- [ ] Streamlit still works on port 8501
- [ ] Both can run simultaneously

---

## Phase 5: API Endpoints

**Goal**: Expose all data queries as JSON endpoints to replace direct DB calls in Streamlit.

**Effort**: 3-4 days  
**Risk**: Low (additive)  
**Prerequisites**: Phase 4 (FastAPI skeleton)

### Approach
Map each Streamlit data access pattern to a REST endpoint. These endpoints will be consumed by the Jinja2 templates in Phase 6.

### Endpoint Inventory

#### Data Query Endpoints
| Streamlit Pattern | FastAPI Endpoint | DB Method |
|---|---|---|
| `db.get_activities()` | `GET /api/activities` | `get_activities()` |
| `db.get_activity_with_metrics(id)` | `GET /api/activities/{id}` | `get_activity_with_metrics()` |
| `db.get_activity_streams(id, metric)` | `GET /api/activities/{id}/streams/{metric}` | `get_activity_streams()` |
| `db.get_trend_data(...)` | `GET /api/trends/{table}` | `get_trend_data()` |
| `compute_training_load_history(...)` | `GET /api/analytics/training-load/history` | `compute_training_load_history()` |
| `get_all_routes()` | `GET /api/routes` | `get_all_routes()` |

#### Example Implementation
```python
# src/web/routes/api.py
from fastapi import APIRouter, Depends, HTTPException
from src.db.store import CyclingDB
from src import config

router = APIRouter(prefix="/api")

def get_db():
    db_path = str(config.db_path("cycling_agent.sqlite"))
    db = CyclingDB(db_path)
    try:
        yield db
    finally:
        db.close()

@router.get("/activities")
async def list_activities(
    oldest: str | None = None,
    newest: str | None = None,
    activity_type: str | None = None,
    db: CyclingDB = Depends(get_db),
):
    activities = db.get_activities(oldest, newest, activity_type)
    return [dict(row) for row in activities]

@router.get("/activities/{activity_id}")
async def get_activity(activity_id: str, db: CyclingDB = Depends(get_db)):
    activity = db.get_activity_with_metrics(activity_id)
    if not activity:
        raise HTTPException(404, "Activity not found")
    return activity

@router.get("/activities/{activity_id}/streams/{metric}")
async def get_activity_streams(
    activity_id: str,
    metric: str,
    db: CyclingDB = Depends(get_db),
):
    if metric not in {"power", "heart_rate", "cadence", "speed", "altitude"}:
        raise HTTPException(400, f"Invalid metric: {metric}")
    
    rows = db.get_activity_streams(activity_id, metric)
    
    # Downsample for performance
    elapsed = [r["elapsed"] for r in rows]
    values = [r["value"] for r in rows]
    elapsed, values = _downsample(elapsed, values, max_points=10_000)
    
    return {
        "activity_id": activity_id,
        "metric": metric,
        "elapsed": elapsed,
        "values": values,
    }

def _downsample(elapsed, values, max_points=10_000):
    """Downsample to max_points."""
    if len(values) <= max_points:
        return elapsed, values
    step = len(values) // max_points
    return elapsed[::step][:max_points], values[::step][:max_points]
```

### Verification
- [ ] All endpoints return valid JSON
- [ ] Data matches Streamlit queries
- [ ] Downsampling works correctly
- [ ] Error handling for invalid inputs
- [ ] Performance acceptable (<100ms for most queries)

---

## Phase 6: Jinja2 Templates + HTMX

**Goal**: Recreate all 5 Streamlit pages as Jinja2 templates with HTMX for interactivity.

**Effort**: 7-10 days  
**Risk**: Medium (largest phase)  
**Prerequisites**: Phase 5 (API endpoints)

### Approach
Migrate one page at a time, starting with the simplest (Profile) and ending with the most complex (Settings).

### Migration Order
1. **Profile** - Simple form, no charts
2. **Activity Detail** - Charts, but static data
3. **Trends** - Multiple charts, date filtering
4. **Map** - Geocoding, Plotly map
5. **Settings** - Complex auth flow

### Page-by-Page Migration

#### 6.1 Profile Page (Simplest)
**Streamlit features**:
- Form with text inputs, number inputs, selectbox
- Save to `user_profile.md`

**Jinja2 + HTMX**:
```html
<!-- templates/profile.html -->
{% extends "base.html" %}
{% block content %}
<h1>Athlete Profile</h1>
<form hx-post="/profile" hx-target="#profile-status">
    <label>Name: <input type="text" name="name" value="{{ profile.name }}"></label>
    <label>Weight (kg): <input type="number" name="weight" value="{{ profile.weight }}"></label>
    <label>FTP (watts): <input type="number" name="ftp" value="{{ profile.ftp }}"></label>
    <!-- ... more fields ... -->
    <button type="submit">Save Profile</button>
</form>
<div id="profile-status"></div>
{% endblock %}
```

**FastAPI route**:
```python
@router.post("/profile")
async def save_profile(request: Request, name: str = Form(), weight: float = Form(), ...):
    profile = {"name": name, "weight": weight, ...}
    save_user_profile(profile)
    return "<div class='success'>Profile saved!</div>"
```

#### 6.2 Activity Detail Page
**Streamlit features**:
- Selectbox to choose activity
- Metadata cards
- Time-series charts (power, HR, speed, cadence, altitude)

**Jinja2 + HTMX**:
```html
<!-- templates/activity.html -->
{% extends "base.html" %}
{% block content %}
<h1>Activity Detail</h1>
<select name="activity_id"
        hx-get="/activity-detail"
        hx-target="#activity-content"
        hx-trigger="change">
    {% for activity in activities %}
    <option value="{{ activity.id }}">{{ activity.start_date }}</option>
    {% endfor %}
</select>

<div id="activity-content">
    <!-- HTMX loads activity detail here -->
</div>
{% endblock %}
```

**Chart rendering** (client-side Plotly):
```html
<!-- templates/components/activity_charts.html -->
<div id="power-chart" class="chart-container"></div>
<script>
    fetch('/api/activities/{{ activity_id }}/streams/power')
        .then(r => r.json())
        .then(data => {
            Plotly.newPlot('power-chart', [{
                x: data.elapsed.map(e => e / 60),
                y: data.values,
                type: 'scatter',
                mode: 'lines'
            }], {
                title: 'Power',
                xaxis: { title: 'Minutes' },
                yaxis: { title: 'Watts' }
            });
        });
</script>
```

#### 6.3 Trends Page
**Streamlit features**:
- Date range picker
- Multiple line charts (FTP, CTL/ATL, TSB, HRV, RHR, weight, sleep, stress)

**Jinja2 + HTMX**:
```html
<!-- templates/trends.html -->
{% extends "base.html" %}
{% block content %}
<h1>Trends</h1>
<form hx-get="/trends/charts" hx-target="#trends-charts" hx-trigger="change">
    <label>Start: <input type="date" name="start" value="{{ start_date }}"></label>
    <label>End: <input type="date" name="end" value="{{ end_date }}"></label>
</form>

<div id="trends-charts">
    {% include "components/trends_charts.html" %}
</div>
{% endblock %}
```

#### 6.4 Map Page
**Streamlit features**:
- City text input
- Radius slider
- Plotly scatter_mapbox or scatter_geo

**Jinja2 + HTMX**:
```html
<!-- templates/map.html -->
{% extends "base.html" %}
{% block content %}
<h1>Route Map</h1>
<form hx-get="/map/routes" hx-target="#map-container" hx-trigger="change">
    <label>City: <input type="text" name="city" value="{{ city }}"></label>
    <label>Radius (miles): <input type="range" name="radius" min="10" max="500" value="{{ radius }}"></label>
</form>

<div id="map-container">
    {% include "components/map_chart.html" %}
</div>
{% endblock %}
```

#### 6.5 Settings Page (Most Complex)
**Streamlit features**:
- Garmin auth state machine (login, MFA, sync)
- Credential management
- Sync controls

**Jinja2 + HTMX**:
```html
<!-- templates/settings.html -->
{% extends "base.html" %}
{% block content %}
<h1>Settings</h1>

{% if auth_state == "idle" %}
    <form hx-post="/auth/garmin/login" hx-target="#auth-status">
        <label>Email: <input type="email" name="email"></label>
        <label>Password: <input type="password" name="password"></label>
        <button type="submit">Sign In</button>
    </form>
{% elif auth_state == "mfa_required" %}
    <form hx-post="/auth/garmin/mfa" hx-target="#auth-status">
        <label>MFA Code: <input type="text" name="mfa_code" maxlength="6"></label>
        <button type="submit">Verify</button>
    </form>
{% endif %}

<div id="auth-status"></div>
{% endblock %}
```

### Verification
- [ ] All 5 pages render correctly
- [ ] HTMX interactions work (no full page reloads)
- [ ] Charts render client-side with correct data
- [ ] Forms submit and update data
- [ ] Dark mode works (CSS media query)
- [ ] HA Ingress path prefix handled correctly

---

## Phase 7: Garmin Auth Migration

**Goal**: Migrate the MFA state machine from Streamlit to FastAPI sessions.

**Effort**: 2-3 days  
**Risk**: High (authentication is critical)  
**Prerequisites**: Phase 6 (Jinja2 templates)

### Challenge
Streamlit uses `st.session_state` to persist the `GarminAuth` instance between MFA phases. FastAPI needs a different approach.

### Solution: Server-Side Session Store
Use FastAPI sessions with a server-side store (Redis or in-memory dict) to hold the pending `GarminAuth` instance.

### Detailed Steps

#### Step 7.1: Add Session Middleware
```python
# src/web/app.py
from starlette.middleware.sessions import SessionMiddleware

app.add_middleware(SessionMiddleware, secret_key="your-secret-key")
```

#### Step 7.2: Create Auth Routes
```python
# src/web/routes/auth.py
from fastapi import APIRouter, Request, Form
from src.ingestion.garmin_connect import authenticate_garmin, GarminAuthResult
import redis

router = APIRouter(prefix="/auth")
redis_client = redis.Redis(host="localhost", port=6379, decode_responses=True)

@router.post("/garmin/login")
async def garmin_login(
    request: Request,
    email: str = Form(),
    password: str = Form(),
):
    # Phase 1: Initial auth
    result, auth_instance = authenticate_garmin(
        email=email,
        password=password,
        tokenstore=os.environ.get("GARMIN_TOKENSTORE", ""),
    )
    
    if result == GarminAuthResult.MFA_REQUIRED:
        # Store auth instance in Redis (keyed by session ID)
        session_id = request.session.get("session_id", str(uuid.uuid4()))
        request.session["session_id"] = session_id
        
        # Note: GarminAuth instances can't be serialized to Redis
        # Solution: Store in in-memory dict (see below)
        pending_auths[session_id] = auth_instance
        
        return {
            "auth_state": "mfa_required",
            "message": "MFA code sent to your email",
        }
    
    elif result == GarminAuthResult.SUCCESS:
        # Save credentials
        _update_config_env({"GARMIN_EMAIL": email, "GARMIN_PASSWORD": password})
        return {
            "auth_state": "authenticated",
            "message": "Login successful",
        }
    
    else:
        return {
            "auth_state": "idle",
            "message": "Login failed",
        }

# In-memory store for pending auth instances
# (Can't serialize GarminAuth to Redis)
pending_auths: dict[str, object] = {}

@router.post("/garmin/mfa")
async def garmin_mfa(
    request: Request,
    mfa_code: str = Form(),
):
    session_id = request.session.get("session_id")
    if not session_id or session_id not in pending_auths:
        return {"auth_state": "idle", "message": "No pending MFA"}
    
    auth_instance = pending_auths[session_id]
    
    # Phase 2: Complete MFA
    result, _ = authenticate_garmin(
        email=None,
        password=None,
        tokenstore=os.environ.get("GARMIN_TOKENSTORE", ""),
        mfa_code=mfa_code,
        auth_instance=auth_instance,
    )
    
    if result == GarminAuthResult.SUCCESS:
        del pending_auths[session_id]
        return {
            "auth_state": "authenticated",
            "message": "MFA verified successfully",
        }
    else:
        return {
            "auth_state": "mfa_required",
            "message": "Invalid MFA code",
        }
```

**Note**: `GarminAuth` instances can't be serialized to Redis, so we use an in-memory dict. This works for single-process deployments. For multi-process, we'd need to re-authenticate on each request using cached tokens.

### Verification
- [ ] Login flow works (email + password)
- [ ] MFA flow works (receive code, verify)
- [ ] Credentials saved to `config.env`
- [ ] Tokens cached and reused on subsequent logins
- [ ] Session persists across page reloads
- [ ] Logout clears session and tokens

---

## Phase 8: Chart Migration

**Goal**: Move Plotly rendering from server (Streamlit) to client (browser).

**Effort**: 3-4 days  
**Risk**: Medium  
**Prerequisites**: Phase 5 (API endpoints), Phase 6 (Jinja2 templates)

### Approach
Serve chart data as JSON via API endpoints. Render charts client-side using Plotly.js.

### Detailed Steps

#### Step 8.1: Add Plotly.js to Base Template
```html
<!-- templates/base.html -->
<head>
    <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
    <script src="/static/js/charts.js"></script>
</head>
```

#### Step 8.2: Create Chart Rendering Library
```javascript
// static/js/charts.js

function renderZoneChart(elementId, data, zones, options = {}) {
    const isDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    const template = isDark ? 'plotly_dark' : 'plotly_white';
    const lineColor = isDark ? '#f0f0f0' : '#222222';
    
    const traces = [{
        x: data.elapsed.map(e => e / 60),  // seconds to minutes
        y: data.values,
        type: 'scatter',
        mode: 'lines',
        line: { width: 2, color: lineColor },
        hovertemplate: '%{x:.1f} min<br>%{y:.0f} ' + options.unit + '<extra></extra>'
    }];
    
    const shapes = zones.map(z => ({
        type: 'rect',
        x0: 0,
        x1: 1,
        xref: 'paper',
        y0: z.lo,
        y1: z.hi,
        fillcolor: z.color,
        opacity: 0.12,
        line: { width: 0 },
        layer: 'below'
    }));
    
    const layout = {
        title: options.title || '',
        height: 360,
        template: template,
        shapes: shapes,
        xaxis: { title: 'Minutes' },
        yaxis: { title: options.yLabel || '' },
        margin: { l: 50, r: 20, t: 40, b: 40 },
        showlegend: false
    };
    
    Plotly.newPlot(elementId, traces, layout);
}

function renderLineChart(elementId, data, options = {}) {
    const isDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    const template = isDark ? 'plotly_dark' : 'plotly_white';
    
    const traces = [{
        x: data.x,
        y: data.y,
        type: 'scatter',
        mode: 'lines',
        line: { width: 2 }
    }];
    
    const layout = {
        title: options.title || '',
        height: options.height || 320,
        template: template,
        xaxis: { title: options.xLabel || '' },
        yaxis: { title: options.yLabel || '' },
        margin: { l: 50, r: 20, t: 40, b: 40 }
    };
    
    Plotly.newPlot(elementId, traces, layout);
}
```

#### Step 8.3: Update Templates to Use Client-Side Charts
```html
<!-- templates/components/activity_charts.html -->
<div id="power-chart" class="chart-container" data-activity-id="{{ activity_id }}"></div>

<script>
    const activityId = document.getElementById('power-chart').dataset.activityId;
    
    fetch(`/api/activities/${activityId}/streams/power`)
        .then(r => r.json())
        .then(data => {
            const zones = [
                { lo: 0, hi: {{ ftp * 0.55 }}, color: '#1f77b4', label: 'Z1' },
                { lo: {{ ftp * 0.55 }}, hi: {{ ftp * 0.75 }}, color: '#2ca02c', label: 'Z2' },
                // ... more zones
            ];
            
            renderZoneChart('power-chart', data, zones, {
                title: 'Power',
                yLabel: 'Watts',
                unit: 'W'
            });
        });
</script>
```

### Verification
- [ ] All charts render correctly
- [ ] Dark mode works (CSS media query)
- [ ] Zone charts show colored bands
- [ ] Charts are responsive (resize with window)
- [ ] Performance acceptable (<1s to render)
- [ ] No server-side Plotly rendering

---

## Phase 9: HA Add-on Integration

**Goal**: Update Dockerfile, run.sh, config.json for FastAPI.

**Effort**: 1-2 days  
**Risk**: Medium  
**Prerequisites**: Phase 6 (Jinja2 templates), Phase 7 (Garmin auth)

### Detailed Steps

#### Step 9.1: Update `run.sh`
```bash
# Start Redis (if using in-container Redis)
redis-server --daemonize yes

# Start ARQ worker (if using background tasks)
arq src.tasks.worker.WorkerSettings &
ARQ_PID=$!

# Start FastAPI (replace Streamlit)
uvicorn src.web.app:create_app \
    --factory \
    --host 0.0.0.0 \
    --port 8501 \
    --root-path "${HASSIO_INGRESS:-/}" \
    &
FASTAPI_PID=$!

# Background sync (existing logic)
...

# Wait for FastAPI (keeps container alive)
wait ${FASTAPI_PID}
```

#### Step 9.2: Update `config.json`
```json
{
  "ingress_port": 8501,
  "ingress_stream": false  // HTMX doesn't need WebSocket streaming
}
```

#### Step 9.3: Update `Dockerfile`
```dockerfile
# Add Redis if using in-container
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl redis-server && \
    rm -rf /var/lib/apt/lists/*

# Add new dependencies
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt
```

### Verification
- [ ] HA add-on builds successfully
- [ ] FastAPI accessible via HA Ingress
- [ ] All pages work through Ingress
- [ ] Background sync runs on startup
- [ ] Persistent data (`/data`) works correctly

---

## Phase 10: Testing & Cleanup

**Goal**: Comprehensive testing, remove Streamlit, update documentation.

**Effort**: 3-5 days  
**Risk**: Low  
**Prerequisites**: All previous phases

### Detailed Steps

#### Step 10.1: Remove Streamlit
```bash
# Remove from requirements.txt
# streamlit>=1.30.0

# Delete Streamlit files
rm src/visualize.py
rm personal_cycling_agent/src/visualize.py  # (if not symlinked)

# Update main.py to remove --visualize flag
```

#### Step 10.2: Update Documentation
- Update `README.md` with new architecture
- Update `docs/PLAN.md` with completed migration
- Update `personal_cycling_agent/DOCS.md` with new UI instructions

#### Step 10.3: Update Tests
- Add tests for FastAPI endpoints
- Add tests for HTMX interactions (if possible)
- Update test fixtures for new data formats

#### Step 10.4: Performance Testing
- Benchmark page load times
- Test with large datasets (1000+ activities)
- Verify chart rendering performance
- Test concurrent users

#### Step 10.5: User Acceptance Testing
- Test all workflows end-to-end
- Verify Garmin auth flow
- Test sync with progress tracking
- Verify all charts render correctly

### Verification
- [ ] All 192 existing tests pass
- [ ] New FastAPI tests added and passing
- [ ] Documentation updated
- [ ] No references to Streamlit in codebase
- [ ] Performance benchmarks acceptable
- [ ] User acceptance testing complete

---

## Timeline Summary

| Phase | Description | Effort | Dependencies |
|---|---|---|---|
| 1 | Code deduplication | 2-3 days | None |
| 2 | fitparse → fitdecode | 3-4 days | Phase 1 |
| 3 | Background tasks | 4-5 days | Phase 1 |
| 4 | FastAPI skeleton | 2-3 days | Phase 1 |
| 5 | API endpoints | 3-4 days | Phase 4 |
| 6 | Jinja2 + HTMX | 7-10 days | Phase 5 |
| 7 | Garmin auth | 2-3 days | Phase 6 |
| 8 | Chart migration | 3-4 days | Phase 5, 6 |
| 9 | HA add-on integration | 1-2 days | Phase 6, 7 |
| 10 | Testing & cleanup | 3-5 days | All |
| **Total** | | **27-39 days** | |

**Parallelization opportunities**:
- Phases 2 and 3 can run in parallel (both depend only on Phase 1)
- Phases 4 and 5 can run in parallel with Phase 3
- Phase 8 can start as soon as Phase 5 is done (doesn't need Phase 6)

**Realistic timeline with parallelization**: **20-30 days**

---

## Risk Mitigation

### Rollback Strategy
Each phase is designed to be independently deployable. If a phase causes issues:
1. Revert the specific phase's changes
2. Keep previous phases (they're stable)
3. Fix the issue and re-deploy

### Testing Strategy
- **Unit tests**: All existing tests must pass after each phase
- **Integration tests**: Add new tests for FastAPI endpoints
- **Manual testing**: Test each page in both standalone and HA add-on modes
- **Performance testing**: Benchmark before/after each phase

### Communication
- Update `CHANGELOG.md` after each phase
- Tag releases after major phases (e.g., `v0.1.0` after Phase 1)
- Document breaking changes in commit messages

---

## Success Criteria

The migration is successful when:
1. **All features work**: All 5 pages render correctly, all interactions work
2. **Performance improved**: FIT parsing 5-10x faster, UI doesn't block
3. **Code simplified**: Single codebase, no Streamlit
4. **Tests pass**: All 192 existing tests + new FastAPI tests
5. **Documentation updated**: README, PLAN, DOCS all reflect new architecture
6. **HA add-on works**: Builds, installs, runs correctly in Home Assistant

---

## Next Steps

1. **Start with Phase 1** (code deduplication) - this is the foundation
2. **Benchmark fitparse** (Phase 2, Step 2.1) to validate the performance gain
3. **Decide on background task approach** (Phase 3) - ARQ vs Celery vs simple threading
4. **Create a proof-of-concept** for one page (e.g., Profile) to validate the FastAPI + Jinja2 + HTMX approach

The plan is detailed enough to execute, but flexible enough to adapt as we learn more during implementation.
